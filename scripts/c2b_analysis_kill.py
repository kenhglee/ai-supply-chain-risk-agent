#!/usr/bin/env python3
"""
C2b experiment driver: kill a worker inside the analyze node, then resume.

Experiment-only. It installs hooks in its own process and then calls the
unmodified lifecycle (app.workflows.risk_workflow); normal execution has no
hooks and no delays.

Windows covered (transport-level holds and in-flight termination are deferred):

  after-inference        c1. Hold inside analyze, after the risk model's
                         response has been returned to the node and before
                         analyze returns. Nothing from analyze is persisted.
                         Expected on resume: analyze runs again and the risk
                         model is invoked again.
  after-analyze-writes   c3 negative control. Hold after analyze's pending
                         writes are durably stored (put_writes returned) and
                         before the next checkpoint is written; the main
                         thread waits on that checkpoint put. LangGraph
                         reapplies the stored writes: analyze does not run
                         again and there is no new inference.

                         get_state() then reports next=() while the analyze
                         task already holds a result. The c3 control found
                         that resume_workflow treated that as completed
                         (already_completed); completion is now decided from
                         snapshot.tasks. The c3 verdict still checks the
                         lifecycle and LangGraph (attempt --direct-invoke)
                         separately.

What the holds prove: c1 shows at-least-once re-execution with duplicate
inference after the client received a complete response. It is not an
in-flight kill; the provider had finished. c3 shows that analyze's commit
boundary is its pending writes, not the following checkpoint.

COMMANDS
    start    --headline H --events FILE
             Process A: START, pausing before analyze (triage + retrieval run).
             Prints THREAD_ID=<id>.
    attempt  --thread-id T --events FILE --hold {after-inference,after-analyze-writes,none}
             Process B (with a hold) or C (none): RESUME with tripwires on
             triage, retrieval and embeddings; only risk inference is allowed.
             At the hold it prints HOLDING ... pid=<pid> and waits to be killed
             (kill -9). If the hold times out the process exits 75 and never
             continues past the hold.
             --direct-invoke (with --hold none) is a diagnostic: it continues
             the thread with the compiled graph's invoke(None), bypassing
             risk_workflow.resume_workflow, so it writes no trace and no
             supplier state. Use it only to show what LangGraph itself does.
    snapshot --thread-id T --events FILE --label L
             Records the durable checkpoint state (latest checkpoint, next node,
             whether analyze's pending writes are stored) as an event.
    verdict  --thread-id T --events FILE --expect {c1,c3,contention} [--out FILE] [--offline]
             Evaluates the event log and durable state (checkpoint, traces);
             exit 0 only if every acceptance check passed. Acceptance uses
             the normal lifecycle evidence; diagnostics (--direct-invoke) are
             reported separately and are optional. --offline reads no durable
             state: the checks that need it are reported as not evaluated and
             the verdict cannot pass.

OWNERSHIP (production dependencies are owned; docs/SAME_THREAD_OWNERSHIP_DESIGN.md)
    Every attempt acquires the thread's lease; a heartbeat keeps renewing it
    while the attempt holds, so a second attempt during a hold is
    `contended` (exit 5 from the lifecycle; recorded, nothing executed).
    After a kill the lease is only taken over once it expires: set
    LEASE_TTL_MS low for experiments and wait longer than it before the
    takeover attempt. --release-file F makes a hold continue once F exists
    (owned contention experiment: the owner completes after the contender).
    --direct-invoke needs unfenced dependencies and is refused on owned ones.
    Events record code identity (git commit / CODE_REVISION / IMAGE_DIGEST)
    and lease epochs; snapshots record the lease and the final marker.

    verdict --expect contention: an owner held (with --release-file) while a
    contender attempt ran; the contender must be `contended` with no analyze,
    no model call, no workflow write and no trace; the owner then completes
    and finalizes at the same epoch.

EVENTS
    One JSON object per line, appended, flushed and fsynced before any hold,
    so a SIGKILLed attempt keeps its record. No credentials, prompts or
    response bodies are recorded: only identifiers, timings, token usage and
    request IDs. A killed attempt has no normal completion trace; that is
    expected and the event log is the record of it.

EXIT
    0 ok; 1 verdict failed; 2 usage; 3 thread not at the expected node;
    5 hook could not be installed; 6 tripwire hit; 75 hold expired.
"""
import argparse
import inspect
import json
import os
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

HOLD_AFTER_INFERENCE = "after-inference"
HOLD_AFTER_ANALYZE_WRITES = "after-analyze-writes"
HOLD_NONE = "none"
HOLD_CHOICES = (HOLD_AFTER_INFERENCE, HOLD_AFTER_ANALYZE_WRITES, HOLD_NONE)
ANALYSIS_NODES = ["infer", "decide", "retrieve", "analyze"]

EXIT_OK = 0
EXIT_VERDICT_FAILED = 1
EXIT_PRECONDITION = 3
EXIT_HOOK = 5
EXIT_TRIPWIRE = 6
EXIT_HOLD_EXPIRED = 75

DEFAULT_HOLD_TIMEOUT_S = 900
WRITES_COMMIT_TIMEOUT_S = 30
ALLOWED_BEDROCK_OPERATIONS = frozenset({"Converse"})


class HookInstallError(RuntimeError):
    """A requested hook could not be installed; the experiment must not run."""


class TripwireHit(RuntimeError):
    """Triage, retrieval, embeddings or a non-risk model call was attempted."""


# ---- Event log ----

def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).isoformat(
        timespec="milliseconds")


class EventLog:
    """Append-only JSONL; every event is flushed and fsynced before returning."""

    def __init__(self, path: Path, *, attempt: str):
        self.path = path
        self.attempt = attempt
        self._lock = threading.Lock()
        self._seq = 0
        self._fh = open(path, "a", encoding="utf-8")

    def emit(self, event: str, **fields) -> dict:
        with self._lock:
            self._seq += 1
            record = {"ts": _iso(), "pid": os.getpid(), "attempt": self.attempt, "seq": self._seq,
                      "event": event, **fields}
            self._fh.write(json.dumps(record, default=str) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
            return record


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---- Hooks ----

class _Context:
    def __init__(self, hold: str, hold_timeout: float, log: EventLog, thread_id: str, release_file=None):
        self.hold = hold
        self.hold_timeout = hold_timeout
        self.release_file = release_file
        self.log = log
        self.thread_id = thread_id
        self.analyze_task_id = None
        self.analyze_returned = False
        self.analyze_writes_committed = threading.Event()
        self.holding = False
        self.held = False


def _hold(ctx: _Context, point: str) -> None:
    """Record the hold durably, announce it, and wait to be killed (or released, with --release-file)."""
    ctx.holding = True
    ctx.log.emit("holding", point=point, thread_id=ctx.thread_id, hold_timeout_s=ctx.hold_timeout,
                 waiting_thread=threading.current_thread().name,
                 release_file=str(ctx.release_file) if ctx.release_file else None)
    how = f"touch {ctx.release_file}" if ctx.release_file else f"kill -9 {os.getpid()}"
    print(f"HOLDING point={point} pid={os.getpid()} thread_id={ctx.thread_id} -> run: {how}",
          file=sys.stderr, flush=True)
    deadline = time.monotonic() + ctx.hold_timeout
    while time.monotonic() < deadline:
        if ctx.release_file and Path(ctx.release_file).exists():
            ctx.holding = False
            ctx.log.emit("hold_released", point=point)
            return
        threading.Event().wait(0.1)
    ctx.log.emit("hold_expired", point=point)
    os._exit(EXIT_HOLD_EXPIRED)


def response_fields(response) -> dict:
    """Identifiers and usage only; never the response content."""
    meta = getattr(response, "response_metadata", None) or {}
    usage = getattr(response, "usage_metadata", None) or meta.get("usage") or {}
    request_id = (meta.get("ResponseMetadata") or {}).get("RequestId") or meta.get("request_id")
    return {
        "request_id": request_id,
        "input_tokens": usage.get("input_tokens", usage.get("inputTokens")),
        "output_tokens": usage.get("output_tokens", usage.get("outputTokens")),
        "total_tokens": usage.get("total_tokens", usage.get("totalTokens")),
        "stop_reason": meta.get("stopReason") or meta.get("stop_reason"),
        "provider_latency_ms": (meta.get("metrics") or {}).get("latencyMs"),
        "response_type": type(response).__name__,
    }


class _RiskInferenceProbe:
    """Wraps the real risk model: logs invocation and return, optionally holds after it."""

    def __init__(self, inner, ctx: _Context, model_name: str):
        self._inner = inner
        self._ctx = ctx
        self._model_name = model_name

    def invoke(self, prompt, *args, **kwargs):
        self._ctx.log.emit("model_invoke_start", model=self._model_name, prompt_chars=len(str(prompt)))
        t0 = time.time()
        response = self._inner.invoke(prompt, *args, **kwargs)
        self._ctx.log.emit("model_return", model=self._model_name,
                           client_latency_ms=round((time.time() - t0) * 1000, 1), **response_fields(response))
        if self._ctx.hold == HOLD_AFTER_INFERENCE:
            _hold(self._ctx, HOLD_AFTER_INFERENCE)
        return response


def _verify_hook_points(agent) -> None:
    """Fail clearly if the code no longer has the shape the hooks rely on."""
    problems = []
    if not callable(getattr(getattr(agent, "_risk_llm", None), "invoke", None)):
        problems.append("supplier_risk_agent._risk_llm has no invoke()")
    try:
        if "_risk_llm.invoke(" not in inspect.getsource(agent.analyze_risk):
            problems.append("analyze_risk no longer calls _risk_llm.invoke()")
        build_src = inspect.getsource(agent.build_app)
        for node, fn in (("analyze", "analyze_risk"), ("validate", "validate_alert")):
            if f'graph.add_node("{node}", {fn})' not in build_src:
                problems.append(f"build_app no longer binds {node} to {fn} when the graph is built")
    except (OSError, TypeError) as exc:
        problems.append(f"cannot inspect workflow source: {exc}")
    if problems:
        raise HookInstallError("; ".join(problems))


def install_hooks(ctx: _Context, checkpointer, *, require_bedrock: bool = True) -> dict:
    """Tripwires on triage/retrieval/embeddings; probes on analyze, validate and the saver."""
    from app.retrieval import retriever as retrieval
    from app.workflows import supplier_risk_agent as agent

    _verify_hook_points(agent)
    risk_model = agent._risk_runtime.runtime_model_name
    if require_bedrock and agent._risk_runtime.runtime_provider != "bedrock":
        raise HookInstallError(
            f"risk model provider is {agent._risk_runtime.runtime_provider!r}; this driver only allows "
            "Bedrock Converse for risk inference")
    for name in ("put", "put_writes"):
        if not callable(getattr(checkpointer, name, None)):
            raise HookInstallError(f"checkpointer has no {name}()")

    def trip(what: str):
        ctx.log.emit("tripwire_hit", what=what)
        raise TripwireHit(what)

    # Allowed: risk inference only.
    class _Blocked:
        def __init__(self, name):
            self._name = name

        def invoke(self, *a, **k):
            trip(f"model {self._name}.invoke")

    agent._triage_llm = _Blocked("triage")
    agent._retriever = None
    agent._get_retriever = lambda: trip("retriever _get_retriever")
    agent.get_retriever = lambda *a, **k: trip("retriever get_retriever")
    retrieval.get_retriever = lambda *a, **k: trip("retriever get_retriever")
    retrieval.build_embeddings = lambda *a, **k: trip("embeddings build_embeddings")
    agent._risk_llm = _RiskInferenceProbe(agent._risk_llm, ctx, risk_model)

    import httpx

    def no_http(client, request, *a, **k):
        trip(f"http {request.method} {request.url.host}")

    httpx.Client.send = no_http

    import botocore.client
    original_api_call = botocore.client.BaseClient._make_api_call
    aws_calls: dict[str, int] = {}

    def guarded_api_call(client, operation_name, api_params):
        service = client.meta.service_model.service_name
        key = f"{service}:{operation_name}"
        aws_calls[key] = aws_calls.get(key, 0) + 1
        if service == "bedrock-runtime":
            model_id = api_params.get("modelId")
            if operation_name not in ALLOWED_BEDROCK_OPERATIONS or model_id != risk_model:
                trip(f"aws {key} modelId={model_id}")
        elif service.startswith("bedrock"):
            trip(f"aws {key}")
        if service != "dynamodb":
            ctx.log.emit("aws_call", call=key)
        return original_api_call(client, operation_name, api_params)

    botocore.client.BaseClient._make_api_call = guarded_api_call

    # Node probes (bound when build_app() builds the graph inside resume_workflow).
    original_analyze, original_validate = agent.analyze_risk, agent.validate_alert

    def analyze_probe(state, config):
        configurable = (config or {}).get("configurable") or {}
        ctx.analyze_task_id = configurable.get("__pregel_task_id")
        if ctx.hold == HOLD_AFTER_ANALYZE_WRITES and not ctx.analyze_task_id:
            ctx.log.emit("hook_failed", reason="LangGraph did not expose the analyze task id")
            os._exit(EXIT_HOOK)
        ctx.log.emit("analyze_enter", trace_id=configurable.get("trace_id"), task_id=ctx.analyze_task_id,
                     lease_epoch=configurable.get("lease_epoch"))
        result = original_analyze(state)
        ctx.analyze_returned = True
        ctx.log.emit("analyze_return", alert_status=(result.get("alert") or {}).get("status"))
        return result

    def validate_probe(state):
        ctx.log.emit("validate_enter", during_hold=ctx.holding)
        if ctx.holding:
            ctx.log.emit("downstream_during_hold", node="validate")
        return original_validate(state)

    agent.analyze_risk = analyze_probe
    agent.validate_alert = validate_probe

    # Saver probes: pending writes (per task) and checkpoints (per step).
    original_put_writes, original_put = checkpointer.put_writes, checkpointer.put

    def put_writes_probe(config, writes, task_id, task_path=""):
        original_put_writes(config, writes, task_id, task_path)
        node = task_path.rsplit(",", 1)[-1].strip() if task_path else None
        ctx.log.emit("writes_committed", node=node, task_id=task_id,
                     checkpoint_id=config["configurable"].get("checkpoint_id"), write_count=len(writes))
        if task_id == ctx.analyze_task_id:
            ctx.analyze_writes_committed.set()

    def put_probe(config, checkpoint, metadata, new_versions):
        step = metadata.get("step")
        if ctx.hold == HOLD_AFTER_ANALYZE_WRITES and ctx.analyze_returned and not ctx.held:
            ctx.held = True
            if not ctx.analyze_writes_committed.wait(WRITES_COMMIT_TIMEOUT_S):
                ctx.log.emit("hook_failed", reason="analyze writes were not confirmed before the next checkpoint")
                os._exit(EXIT_HOOK)
            ctx.log.emit("checkpoint_put_blocked", step=step, checkpoint_id=checkpoint["id"])
            _hold(ctx, HOLD_AFTER_ANALYZE_WRITES)
        if ctx.holding:
            ctx.log.emit("downstream_during_hold", node=f"checkpoint step {step}")
        result = original_put(config, checkpoint, metadata, new_versions)
        ctx.log.emit("checkpoint_committed", step=step, checkpoint_id=checkpoint["id"])
        return result

    checkpointer.put_writes = put_writes_probe
    checkpointer.put = put_probe
    return aws_calls


# ---- Commands ----

def code_identity() -> dict:
    """What code ran: git commit (and whether the tree was dirty), else the image's labels."""
    import subprocess

    identity = {"code_revision": os.environ.get("CODE_REVISION"), "image_digest": os.environ.get("IMAGE_DIGEST")}
    try:
        identity["git_commit"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
                                                text=True, timeout=5).stdout.strip() or None
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=PROJECT_ROOT,
                                capture_output=True, text=True, timeout=5)
        identity["git_dirty"] = bool(status.stdout.strip()) if status.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        identity.update(git_commit=None, git_dirty=None)
    return identity


def _default_deps():
    from app.workflows.risk_workflow import checkpoint_dependencies_from_env
    return checkpoint_dependencies_from_env()


def _executor_id() -> str:
    from app.workflows.supplier_risk_agent import get_executor_id
    return get_executor_id()


def _outcome_fields(outcome: dict) -> dict:
    keys = ("status", "thread_id", "trace_id", "next", "change_type", "state_written", "final_status",
            "eligibility_change_type", "action_eligible", "eligibility_basis", "action_id", "action_status",
            "lease_epoch", "lineage_checkpoint_id", "finalized_by_trace_id", "recovered_finalization",
            "trace_recovered", "holder", "error_type")
    return {k: outcome.get(k) for k in keys}


def cmd_start(args, deps_factory) -> int:
    from app.workflows.risk_workflow import start_workflow

    log = EventLog(args.events, attempt=f"start-{os.getpid()}")
    log.emit("attempt_start", role="start", executor_id=_executor_id(), host=socket.gethostname(),
             code=code_identity())
    outcome = start_workflow({"headline": args.headline}, pause_before=["analyze"], **deps_factory())
    log.emit("attempt_exit", **_outcome_fields(outcome))
    print(f"THREAD_ID={outcome.get('thread_id')}", flush=True)
    print(json.dumps(_outcome_fields(outcome)), flush=True)
    return EXIT_OK if outcome.get("status") == "paused" and outcome.get("next") == ["analyze"] else EXIT_PRECONDITION


def cmd_attempt(args, deps_factory, *, require_bedrock: bool) -> int:
    from app.workflows.risk_workflow import resume_workflow
    from app.workflows.supplier_risk_agent import build_app

    if args.direct_invoke and args.hold != HOLD_NONE:
        print("--direct-invoke is a diagnostic continuation; use it with --hold none", file=sys.stderr)
        return 2
    role = "langgraph-direct" if args.direct_invoke else "resume"
    log = EventLog(args.events, attempt=f"{role}-{args.hold}-{os.getpid()}")
    log.emit("attempt_start", role=role, hold=args.hold, thread_id=args.thread_id,
             executor_id=_executor_id(), host=socket.gethostname(), code=code_identity(),
             release_file=str(args.release_file) if args.release_file else None)
    deps = deps_factory()
    if args.direct_invoke and (deps.get("ownership") is not None or getattr(deps["checkpointer"], "fenced", False)):
        log.emit("attempt_exit", status="refused", error="--direct-invoke needs unfenced dependencies")
        print("--direct-invoke bypasses the lifecycle and has no ownership token; owned (fenced) "
              "dependencies refuse it", file=sys.stderr)
        return 2
    ctx = _Context(args.hold, args.hold_timeout, log, args.thread_id, release_file=args.release_file)

    cfg = {"configurable": {"thread_id": args.thread_id}}
    snapshot = build_app(deps["checkpointer"], action_gate=deps["action_gate"],
                         risk_reader=deps["risk_store"]).get_state(cfg)
    log.emit("precondition", next=list(snapshot.next), tasks=_task_summary(snapshot))
    if args.hold != HOLD_NONE and tuple(snapshot.next) != ("analyze",):
        print(f"thread {args.thread_id} is at {list(snapshot.next)}, not ['analyze']", file=sys.stderr)
        return EXIT_PRECONDITION

    try:
        aws_calls = install_hooks(ctx, deps["checkpointer"], require_bedrock=require_bedrock)
    except HookInstallError as exc:
        log.emit("hook_failed", reason=str(exc))
        print(f"hook install failed: {exc}", file=sys.stderr)
        return EXIT_HOOK
    log.emit("hooks_installed", hold=args.hold)

    try:
        if args.direct_invoke:
            app = build_app(deps["checkpointer"], action_gate=deps["action_gate"], risk_reader=deps["risk_store"])
            values = app.invoke(None, {"configurable": {"thread_id": args.thread_id, "trace_id": uuid.uuid4().hex}},
                                durability="sync")
            after = app.get_state(cfg)
            result = {"status": "direct_invoke_returned", "next": list(after.next),
                      "trace_steps": [{k: st.get(k) for k in ("node_name", "started_at", "ended_at")}
                                      for st in values.get("trace_steps") or []]}
            log.emit("attempt_exit", aws_calls=aws_calls, **result)
            print(json.dumps(result), flush=True)
            return EXIT_OK
        outcome = resume_workflow(args.thread_id, **deps)
    except TripwireHit as exc:
        log.emit("attempt_exit", status="tripwire", error=str(exc))
        print(f"tripwire: {exc}", file=sys.stderr)
        return EXIT_TRIPWIRE
    log.emit("attempt_exit", aws_calls=aws_calls, **_outcome_fields(outcome))
    print(json.dumps(_outcome_fields(outcome)), flush=True)
    return EXIT_OK


def _task_summary(snapshot) -> list[dict]:
    """Pending tasks in a snapshot; a task with a result has stored writes but no checkpoint yet."""
    return [{"name": t.name, "has_result": t.result is not None, "error": bool(t.error)}
            for t in snapshot.tasks]


def durable_snapshot(deps: dict, thread_id: str, analyze_task_ids: set[str]) -> dict:
    from app.workflows.supplier_risk_agent import build_app

    cfg = {"configurable": {"thread_id": thread_id}}
    latest = deps["checkpointer"].get_tuple(cfg)
    if latest is None:
        return {"exists": False}
    snapshot = build_app(deps["checkpointer"], action_gate=deps["action_gate"],
                         risk_reader=deps["risk_store"]).get_state(cfg)
    pending_task_ids = {task_id for task_id, _, _ in latest.pending_writes or []}
    values = latest.checkpoint["channel_values"]
    # The analyze trace step held in analyze's stored writes (timings only), so
    # the final trace can be matched to the persisted original exactly.
    pending_steps = next((v for tid, ch, v in latest.pending_writes or []
                          if tid in analyze_task_ids and ch == "trace_steps"), None) or []
    pending_alert = next((v for tid, ch, v in latest.pending_writes or []
                          if tid in analyze_task_ids and ch == "alert"), None) or {}
    pending_analyze_step = next(({k: st.get(k) for k in ("node_name", "started_at", "ended_at")}
                                 for st in reversed(pending_steps) if st.get("node_name") == "analyze"), None)
    return {
        "exists": True,
        "checkpoint_id": latest.checkpoint["id"],
        "step": latest.metadata.get("step"),
        "next": list(snapshot.next),
        "tasks": _task_summary(snapshot),
        "nodes": [s["node_name"] for s in values.get("trace_steps") or []],
        "analyze_writes_stored": bool(pending_task_ids & analyze_task_ids),
        "pending_analyze_step": pending_analyze_step,
        "pending_analyze_alert_status": pending_alert.get("status"),
        "pending_write_task_count": len(pending_task_ids),
        **_ownership_snapshot(deps, thread_id),
    }


def _ownership_snapshot(deps: dict, thread_id: str) -> dict:
    ownership = deps.get("ownership")
    if ownership is None:
        return {}
    lease = ownership.leases.read(thread_id) or {}
    final = ownership.finals.read(thread_id) or {}
    return {"lease": {k: lease.get(k) for k in ("epoch", "state", "owner", "expires_at_ms")} if lease else None,
            "final": {k: final.get(k) for k in ("kind", "trace_id", "epoch", "change_type", "recovered")}
            if final else None}


def _analyze_task_ids(events: list[dict]) -> set[str]:
    return {e["task_id"] for e in events if e["event"] == "analyze_enter" and e.get("task_id")}


def cmd_snapshot(args, deps_factory) -> int:
    log = EventLog(args.events, attempt=f"snapshot-{os.getpid()}")
    state = durable_snapshot(deps_factory(), args.thread_id, _analyze_task_ids(read_events(args.events)))
    log.emit("durable_snapshot", label=args.label, **state)
    print(json.dumps(state), flush=True)
    return EXIT_OK


NOT_EVALUATED = "not_evaluated"
NOT_RUN = "not_run"


def _check(ok, detail) -> dict:
    return {"ok": bool(ok), "detail": detail}


def _not_evaluated(reason: str) -> dict:
    """Needs durable state (checkpoint / traces) that this verdict did not read."""
    return {"ok": None, "status": NOT_EVALUATED, "detail": reason}


def evaluate(events: list[dict], expect: str, final_trace: dict | None, paused_trace: dict | None,
             final_state: dict | None) -> dict:
    """Acceptance checks over one killed attempt and the lifecycle RESUME that
    followed it, plus optional diagnostics kept out of acceptance.

    final_trace / paused_trace / final_state are None when the verdict did
    not read durable state (--offline); checks that need them are then
    reported as not evaluated, never as passed.
    """
    attempts: dict[str, list[dict]] = {}
    for e in events:
        attempts.setdefault(e["attempt"], []).append(e)

    def first(role: str, hold_none: bool) -> list[dict]:
        return next((evs for evs in attempts.values()
                     if evs[0]["event"] == "attempt_start" and evs[0].get("role") == role
                     and (evs[0].get("hold") == HOLD_NONE) == hold_none), [])

    def names(evs):
        return [e["event"] for e in evs]

    def find(evs, event, **match):
        return next((e for e in evs if e["event"] == event
                     and all(e.get(k) == v for k, v in match.items())), None)

    def exit_status(evs):
        return (find(evs, "attempt_exit") or {}).get("status")

    resume_attempts = [evs for evs in attempts.values()
                       if evs[0]["event"] == "attempt_start" and evs[0].get("role") == "resume"]
    # Owned runs: attempts that found the thread held did nothing; they are checked separately.
    contended = [evs for evs in resume_attempts if exit_status(evs) == "contended"]
    killed = next((evs for evs in resume_attempts if evs[0].get("hold") != HOLD_NONE and evs not in contended), [])
    resumed = next((evs for evs in reversed(resume_attempts)
                    if evs[0].get("hold") == HOLD_NONE and evs not in contended), [])
    direct = first("langgraph-direct", True)
    k, r, d = names(killed), names(resumed), names(direct)
    snapshots = {e["label"]: e for e in events if e["event"] == "durable_snapshot"}
    before, between = snapshots.get("before-kill"), snapshots.get("after-kill")
    hold_point = HOLD_AFTER_INFERENCE if expect == "c1" else HOLD_AFTER_ANALYZE_WRITES
    k_hold = find(killed, "holding") or {}
    k_enter, k_return = find(killed, "analyze_enter") or {}, find(killed, "analyze_return") or {}
    r_start, r_exit = find(resumed, "attempt_start") or {}, find(resumed, "attempt_exit") or {}
    # The first attempt after the kill: the lifecycle RESUME, or the optional diagnostic.
    recovery_start = min((x for x in (r_start, find(direct, "attempt_start")) if x), key=lambda x: x["ts"],
                         default={})
    all_model_returns = [e for e in killed + resumed if e["event"] == "model_return"]
    durable = final_trace is not None and paused_trace is not None and final_state is not None
    final_steps = {st["node_name"]: st for st in (final_trace or {}).get("trace_steps") or []}
    paused_steps = {st["node_name"]: st for st in (paused_trace or {}).get("trace_steps") or []}
    no_durable = "verdict did not read durable state (--offline)"

    acceptance = {
        "killed_attempt_reached_hold": _check(
            k_hold.get("point") == hold_point and "attempt_exit" not in k and "hold_expired" not in k,
            {"hold": k_hold.get("point"), "events": k}),
        "killed_attempt_entered_analyze_and_got_response": _check(
            bool(k_enter) and "model_return" in k,
            {"request_ids": [e.get("request_id") for e in killed if e["event"] == "model_return"]}),
        "nothing_downstream_before_kill": _check(
            bool(k_hold) and "downstream_during_hold" not in k and "validate_enter" not in k
            and find(killed, "checkpoint_committed") is None,
            {"after_hold": k[k.index("holding") + 1:] if "holding" in k else None}),
        "no_tripwire_hits": _check(
            bool(resumed) and "tripwire_hit" not in k + r + d,
            [e.get("what") for e in killed + resumed + direct if e["event"] == "tripwire_hit"]),
        "retrieval_reused_from_start": (_check(
            final_steps.get("retrieve") is not None and paused_steps.get("retrieve") is not None
            and final_steps["retrieve"]["started_at"] == paused_steps["retrieve"]["started_at"],
            {"retrieve_started_at": (final_steps.get("retrieve") or {}).get("started_at"),
             "start_retrieve_started_at": (paused_steps.get("retrieve") or {}).get("started_at")})
            if durable else _not_evaluated(no_durable)),
    }

    if expect == "c1":
        acceptance.update({
            "durable_state_unchanged_by_killed_attempt": _check(
                bool(before) and bool(between) and before.get("checkpoint_id") == between.get("checkpoint_id")
                and between.get("next") == ["analyze"] and not between.get("analyze_writes_stored"),
                {"before": {x: (before or {}).get(x) for x in ("checkpoint_id", "step", "next")},
                 "after_kill": {x: (between or {}).get(x)
                                for x in ("checkpoint_id", "step", "next", "analyze_writes_stored")}}),
            "resume_completed": _check(r_exit.get("status") == "completed", r_exit or None),
            "resume_reran_analyze_and_inference": _check(
                "hooks_installed" in r and "analyze_enter" in r and "model_return" in r
                and len(all_model_returns) == 2,
                {"model_returns": [{"attempt": e["attempt"], "request_id": e.get("request_id"),
                                    "output_tokens": e.get("output_tokens")} for e in all_model_returns]}),
        })
        _owned_takeover_checks(acceptance, killed, resumed, contended, final_state, durable, find)
        return {"acceptance": acceptance, "diagnostics": {}}

    # ---- c3: reuse established from the normal lifecycle evidence ----
    analyze_task = k_enter.get("task_id")
    analyze_tasks = [t for t in (between or {}).get("tasks") or [] if t.get("name") == "analyze"]
    writes_event = find(killed, "writes_committed", node="analyze", task_id=analyze_task) if analyze_task else None
    acceptance["analysis_writes_durable_before_recovery"] = _check(
        writes_event is not None and bool(k_hold) and writes_event["seq"] < k_hold["seq"]
        and bool(before) and bool(between) and between.get("analyze_writes_stored") is True
        and before.get("checkpoint_id") == between.get("checkpoint_id")
        and bool(analyze_tasks) and all(t.get("has_result") for t in analyze_tasks)
        and between["ts"] < recovery_start.get("ts", ""),
        {"analyze_task_id": analyze_task, "writes_committed_seq": (writes_event or {}).get("seq"),
         "hold_seq": k_hold.get("seq"),
         "after_kill": {x: (between or {}).get(x) for x in ("checkpoint_id", "step", "next", "tasks",
                                                            "analyze_writes_stored")}})

    acceptance["resume_did_not_execute_analyze_or_invoke_model"] = _check(
        "hooks_installed" in r and "attempt_exit" in r
        and not {"analyze_enter", "model_invoke_start", "model_return"} & set(r)
        and not [e for e in resumed if e["event"] == "aws_call" and e.get("call", "").startswith("bedrock")]
        and len(all_model_returns) == 1,
        {"resume_events": r or "no resume attempt",
         "model_returns": [{"attempt": e["attempt"], "request_id": e.get("request_id")} for e in all_model_returns]})

    if not durable:
        acceptance["final_analysis_step_is_persisted_original"] = _not_evaluated(no_durable)
    else:
        step = final_steps.get("analyze") or {}
        stored = (between or {}).get("pending_analyze_step")
        if stored:
            basis = "exact match with the analyze step stored in the pending writes"
            ok = {kk: step.get(kk) for kk in ("node_name", "started_at", "ended_at")} == stored
        else:
            basis = "within the killed attempt's logged analyze window (snapshot predates step capture)"
            ok = (bool(step) and bool(k_enter) and bool(k_return)
                  and k_enter["ts"] <= step.get("started_at", "") <= step.get("ended_at", "~") <= k_return["ts"])
        ok = ok and step.get("ended_at", "~") < r_start.get("ts", "") \
            and k_return.get("alert_status") == (final_trace or {}).get("final_status")
        acceptance["final_analysis_step_is_persisted_original"] = _check(
            ok, {"basis": basis, "final_analyze_step": {kk: step.get(kk) for kk in ("started_at", "ended_at")},
                 "stored_analyze_step": stored,
                 "killed_attempt_analyze_window": [k_enter.get("ts"), k_return.get("ts")],
                 "resume_started_at": r_start.get("ts"),
                 "alert_status": [k_return.get("alert_status"), (final_trace or {}).get("final_status")]})

    downstream = ["validate", "assess"] + (["act"] if r_exit.get("action_eligible") else [])
    lifecycle_ok = ("validate_enter" in r and all(find(resumed, "writes_committed", node=n) for n in downstream)
                    and r_exit.get("status") == "completed" and r_exit.get("state_written") is True)
    if not durable:
        acceptance["downstream_and_finalization_completed"] = (
            _check(False, {"resume_exit": r_exit or None}) if not lifecycle_ok
            else _not_evaluated(f"{no_durable}; event-log evidence alone is complete"))
    else:
        final_nodes = [st["node_name"] for st in (final_trace or {}).get("trace_steps") or []]
        acceptance["downstream_and_finalization_completed"] = _check(
            lifecycle_ok and final_state.get("tasks") == [] and final_state.get("next") == []
            and final_trace.get("workflow_event") == "resumed_completed"
            and final_nodes == ANALYSIS_NODES + downstream,
            {"resume_status": r_exit.get("status"), "state_written": r_exit.get("state_written"),
             "downstream_writes": [n for n in downstream if find(resumed, "writes_committed", node=n)],
             "final_durable": {x: final_state.get(x) for x in ("step", "next", "tasks")},
             "final_trace_event": final_trace.get("workflow_event"), "final_trace_nodes": final_nodes})

    _owned_takeover_checks(acceptance, killed, resumed, contended, final_state, durable, find)

    # Optional: what LangGraph alone does (bypasses the lifecycle). Not acceptance.
    if direct:
        diagnostic = _check(
            "analyze_enter" not in d and "model_return" not in d and "validate_enter" in d,
            {"direct_invoke_events": d})
    else:
        diagnostic = {"ok": None, "status": NOT_RUN, "detail": "attempt --direct-invoke was not run (optional)"}
    return {"acceptance": acceptance, "diagnostics": {"langgraph_direct_invoke_reuses_stored_writes": diagnostic}}


def _owned_takeover_checks(acceptance: dict, killed, resumed, contended, final_state, durable, find) -> None:
    """Owned runs only: contention before expiry, takeover at a higher epoch, marker and lease at that epoch."""
    k_enter = find(killed, "analyze_enter") or {}
    r_exit = find(resumed, "attempt_exit") or {}
    if k_enter.get("lease_epoch") is None and r_exit.get("lease_epoch") is None:
        return  # unfenced baseline run
    killed_epoch, resumed_epoch = k_enter.get("lease_epoch"), r_exit.get("lease_epoch")
    k_hold = find(killed, "holding") or {}
    r_start = find(resumed, "attempt_start") or {}
    holders = [(find(evs, "attempt_exit") or {}).get("holder") or {} for evs in contended]
    acceptance["ownership_contended_until_expiry"] = _check(
        all(h.get("epoch") == killed_epoch for h in holders)
        and all(k_hold.get("ts", "") < evs[0]["ts"] < r_start.get("ts", "~") for evs in contended)
        and all(not {"analyze_enter", "model_return", "writes_committed", "checkpoint_committed"}
                & {e["event"] for e in evs} for evs in contended),
        {"contended_attempts": len(contended), "holder_epochs": [h.get("epoch") for h in holders],
         "killed_epoch": killed_epoch})
    acceptance["ownership_takeover_epoch_increased"] = _check(
        killed_epoch is not None and resumed_epoch is not None and resumed_epoch > killed_epoch,
        {"killed_epoch": killed_epoch, "resumed_epoch": resumed_epoch})
    if not durable:
        acceptance["ownership_final_marker_and_lease"] = _not_evaluated("verdict did not read durable state (--offline)")
        return
    final, lease = (final_state or {}).get("final") or {}, (final_state or {}).get("lease") or {}
    acceptance["ownership_final_marker_and_lease"] = _check(
        final.get("trace_id") == r_exit.get("trace_id") and final.get("epoch") == resumed_epoch
        and lease.get("state") == "RELEASED" and lease.get("epoch") == resumed_epoch,
        {"final": final, "lease": lease, "resume_trace_id": r_exit.get("trace_id")})


def evaluate_contention(events: list[dict], *, final_state: dict | None, contender_traces: dict | None,
                        owner_trace: dict | None) -> dict:
    """An owner held (releasable) while a contender attempt ran; see module doc."""
    attempts: dict[str, list[dict]] = {}
    for e in events:
        attempts.setdefault(e["attempt"], []).append(e)

    def find(evs, event):
        return next((e for e in evs if e["event"] == event), None)

    resumes = [evs for evs in attempts.values() if evs[0]["event"] == "attempt_start"
               and evs[0].get("role") == "resume"]
    owner = next((evs for evs in resumes if evs[0].get("hold") != HOLD_NONE), [])
    contenders = [evs for evs in resumes if evs is not owner]
    o_enter, o_hold, o_release = find(owner, "analyze_enter") or {}, find(owner, "holding") or {}, \
        find(owner, "hold_released") or {}
    o_exit = find(owner, "attempt_exit") or {}
    owner_epoch = o_enter.get("lease_epoch")
    snapshots = {e["label"]: e for e in events if e["event"] == "durable_snapshot"}
    before, after = snapshots.get("before-contender") or {}, snapshots.get("after-contender") or {}
    durable = final_state is not None and contender_traces is not None and owner_trace is not None
    forbidden = {"analyze_enter", "model_invoke_start", "model_return", "writes_committed", "checkpoint_committed",
                 "validate_enter", "tripwire_hit"}

    acceptance = {
        "owner_held_then_released": _check(
            bool(o_hold) and bool(o_release) and o_exit.get("status") == "completed",
            {"owner_events": [e["event"] for e in owner]}),
        "contender_ran_during_the_hold": _check(
            bool(contenders) and all(o_hold.get("ts", "~") < evs[0]["ts"]
                                     and (find(evs, "attempt_exit") or {}).get("ts", "~") < o_release.get("ts", "")
                                     for evs in contenders),
            {"contenders": len(contenders), "hold": o_hold.get("ts"), "release": o_release.get("ts")}),
        "contender_was_contended_by_the_owner": _check(
            bool(contenders) and all(
                (find(evs, "attempt_exit") or {}).get("status") == "contended"
                and ((find(evs, "attempt_exit") or {}).get("holder") or {}).get("owner") == o_enter.get("trace_id")
                and ((find(evs, "attempt_exit") or {}).get("holder") or {}).get("epoch") == owner_epoch
                for evs in contenders),
            [{"status": (find(evs, "attempt_exit") or {}).get("status"),
              "holder": (find(evs, "attempt_exit") or {}).get("holder")} for evs in contenders]),
        "contender_executed_nothing": _check(
            bool(contenders) and all("hooks_installed" in {e["event"] for e in evs}
                                     and not forbidden & {e["event"] for e in evs}
                                     and not [e for e in evs if e["event"] == "aws_call"
                                              and e.get("call", "").startswith("bedrock")]
                                     for evs in contenders),
            [[e["event"] for e in evs] for evs in contenders]),
        "one_inference_total": _check(
            sum(1 for e in events if e["event"] == "model_return") == 1,
            sum(1 for e in events if e["event"] == "model_return")),
        "no_workflow_write_by_the_contender": _check(
            bool(before) and bool(after) and before.get("checkpoint_id") == after.get("checkpoint_id")
            and before.get("lease") == after.get("lease"),
            {"before": {k: before.get(k) for k in ("checkpoint_id", "lease")},
             "after": {k: after.get(k) for k in ("checkpoint_id", "lease")}}),
    }
    if not durable:
        for name in ("contender_wrote_no_trace", "owner_finalized_at_its_epoch"):
            acceptance[name] = _not_evaluated("verdict did not read durable state (--offline)")
        return {"acceptance": acceptance, "diagnostics": {}}
    final, lease = final_state.get("final") or {}, final_state.get("lease") or {}
    acceptance["contender_wrote_no_trace"] = _check(
        all(t is None for t in contender_traces.values()), {k: v is not None for k, v in contender_traces.items()})
    acceptance["owner_finalized_at_its_epoch"] = _check(
        final.get("trace_id") == o_exit.get("trace_id") and final.get("epoch") == owner_epoch == o_exit.get("lease_epoch")
        and lease.get("state") == "RELEASED" and lease.get("epoch") == owner_epoch
        and (owner_trace or {}).get("workflow_event") == "resumed_completed",
        {"final": final, "lease": lease, "owner_epoch": owner_epoch,
         "owner_trace_event": (owner_trace or {}).get("workflow_event")})
    return {"acceptance": acceptance, "diagnostics": {}}


def cmd_verdict(args, deps_factory) -> int:
    events = read_events(args.events)
    if args.expect == "contention":
        return _verdict_contention(args, deps_factory, events)
    start_exit = next((e for e in events if e["event"] == "attempt_exit" and e["attempt"].startswith("start-")), {})
    resume_exit = next((e for e in reversed(events) if e["event"] == "attempt_exit"
                        and e["attempt"].startswith("resume-none-") and e.get("status") != "contended"), {})
    final_trace = paused_trace = final_state = None
    if not args.offline:
        from app.storage.risk_trace_store import get_risk_trace_by_identifier

        final_trace = (get_risk_trace_by_identifier(resume_exit["trace_id"])
                       if resume_exit.get("status") == "completed" else None) or {}
        paused_trace = (get_risk_trace_by_identifier(start_exit["trace_id"])
                        if start_exit.get("trace_id") else None) or {}
        final_state = durable_snapshot(deps_factory(), args.thread_id, _analyze_task_ids(events))
    checks = evaluate(events, args.expect, final_trace, paused_trace, final_state)
    acceptance = checks["acceptance"].values()
    diagnostics_run = [c for c in checks["diagnostics"].values() if c["ok"] is not None]
    result = {
        "experiment": f"C2b {args.expect}",
        "thread_id": args.thread_id,
        "durable_state_read": not args.offline,
        "final_durable_state": final_state,
        "final_outcome": {k: resume_exit.get(k) for k in (
            "status", "trace_id", "change_type", "eligibility_change_type", "action_eligible", "action_status")},
        "checks": checks,
        "code": next((e.get("code") for e in events if e.get("code")), None),
        "complete": all(c["ok"] is not None for c in acceptance),
        "passed": all(c["ok"] is True for c in acceptance),
        "diagnostics_ok": all(c["ok"] for c in diagnostics_run) if diagnostics_run else None,
    }
    text = json.dumps(result, indent=2, default=str)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return EXIT_OK if result["passed"] else EXIT_VERDICT_FAILED


def _verdict_contention(args, deps_factory, events) -> int:
    final_state = owner_trace = contender_traces = None
    if not args.offline:
        from app.storage.risk_trace_store import get_risk_trace_by_trace_id

        exits = [e for e in events if e["event"] == "attempt_exit" and e["attempt"].startswith("resume-")]
        owner_exit = next((e for e in exits if e.get("status") != "contended"), {})
        owner_trace = get_risk_trace_by_trace_id(owner_exit["trace_id"]) if owner_exit.get("trace_id") else {}
        contender_traces = {e["trace_id"]: get_risk_trace_by_trace_id(e["trace_id"])
                            for e in exits if e.get("status") == "contended" and e.get("trace_id")}
        final_state = durable_snapshot(deps_factory(), args.thread_id, _analyze_task_ids(events))
    checks = evaluate_contention(events, final_state=final_state, contender_traces=contender_traces,
                                 owner_trace=owner_trace)
    acceptance = checks["acceptance"].values()
    result = {
        "experiment": "owned contention", "thread_id": args.thread_id, "durable_state_read": not args.offline,
        "final_durable_state": final_state, "checks": checks,
        "code": next((e.get("code") for e in events if e.get("code")), None),
        "complete": all(c["ok"] is not None for c in acceptance),
        "passed": all(c["ok"] is True for c in acceptance),
    }
    text = json.dumps(result, indent=2, default=str)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return EXIT_OK if result["passed"] else EXIT_VERDICT_FAILED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="c2b_analysis_kill", description=__doc__.strip().splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start")
    start.add_argument("--headline", required=True)
    start.add_argument("--events", type=Path, required=True)

    attempt = commands.add_parser("attempt")
    attempt.add_argument("--thread-id", required=True)
    attempt.add_argument("--events", type=Path, required=True)
    attempt.add_argument("--hold", choices=HOLD_CHOICES, required=True)
    attempt.add_argument("--hold-timeout", type=float, default=DEFAULT_HOLD_TIMEOUT_S)
    attempt.add_argument("--release-file", type=Path,
                         help="continue the hold once this file exists (default: wait to be killed)")
    attempt.add_argument("--direct-invoke", action="store_true",
                         help="diagnostic: continue with the graph's invoke(None), bypassing the lifecycle")

    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--thread-id", required=True)
    snapshot.add_argument("--events", type=Path, required=True)
    snapshot.add_argument("--label", required=True)

    verdict = commands.add_parser("verdict")
    verdict.add_argument("--thread-id", required=True)
    verdict.add_argument("--events", type=Path, required=True)
    verdict.add_argument("--expect", choices=("c1", "c3", "contention"), required=True)
    verdict.add_argument("--out", type=Path)
    verdict.add_argument("--offline", action="store_true",
                         help="evaluate the event log only; checks needing durable state are not evaluated")
    return parser


def main(argv: list[str] | None = None, *, deps_factory=None, require_bedrock: bool = True) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else EXIT_OK
    deps_factory = deps_factory or _default_deps
    if args.command == "start":
        return cmd_start(args, deps_factory)
    if args.command == "attempt":
        return cmd_attempt(args, deps_factory, require_bedrock=require_bedrock)
    if args.command == "snapshot":
        return cmd_snapshot(args, deps_factory)
    return cmd_verdict(args, deps_factory)


if __name__ == "__main__":
    sys.exit(main())
