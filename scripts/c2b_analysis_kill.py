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
    verdict  --thread-id T --events FILE --expect {c1,c3} [--out FILE]
             Evaluates the event log and durable state; exit 0 if it holds.

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
    def __init__(self, hold: str, hold_timeout: float, log: EventLog, thread_id: str):
        self.hold = hold
        self.hold_timeout = hold_timeout
        self.log = log
        self.thread_id = thread_id
        self.analyze_task_id = None
        self.analyze_returned = False
        self.analyze_writes_committed = threading.Event()
        self.holding = False
        self.held = False


def _hold(ctx: _Context, point: str) -> None:
    """Record the hold durably, announce it, and wait to be killed. Never continues."""
    ctx.holding = True
    ctx.log.emit("holding", point=point, thread_id=ctx.thread_id, hold_timeout_s=ctx.hold_timeout,
                 waiting_thread=threading.current_thread().name)
    print(f"HOLDING point={point} pid={os.getpid()} thread_id={ctx.thread_id} "
          f"-> run: kill -9 {os.getpid()}", file=sys.stderr, flush=True)
    threading.Event().wait(ctx.hold_timeout)
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
        ctx.log.emit("analyze_enter", trace_id=configurable.get("trace_id"), task_id=ctx.analyze_task_id)
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

def _default_deps():
    from app.workflows.risk_workflow import checkpoint_dependencies_from_env
    return checkpoint_dependencies_from_env()


def _executor_id() -> str:
    from app.workflows.supplier_risk_agent import get_executor_id
    return get_executor_id()


def _outcome_fields(outcome: dict) -> dict:
    keys = ("status", "thread_id", "trace_id", "next", "change_type", "state_written", "final_status",
            "eligibility_change_type", "action_eligible", "eligibility_basis", "action_id", "action_status")
    return {k: outcome.get(k) for k in keys}


def cmd_start(args, deps_factory) -> int:
    from app.workflows.risk_workflow import start_workflow

    log = EventLog(args.events, attempt=f"start-{os.getpid()}")
    log.emit("attempt_start", role="start", executor_id=_executor_id(), host=socket.gethostname())
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
             executor_id=_executor_id(), host=socket.gethostname())
    deps = deps_factory()
    ctx = _Context(args.hold, args.hold_timeout, log, args.thread_id)

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
    return {
        "exists": True,
        "checkpoint_id": latest.checkpoint["id"],
        "step": latest.metadata.get("step"),
        "next": list(snapshot.next),
        "tasks": _task_summary(snapshot),
        "nodes": [s["node_name"] for s in values.get("trace_steps") or []],
        "analyze_writes_stored": bool(pending_task_ids & analyze_task_ids),
        "pending_write_task_count": len(pending_task_ids),
    }


def _analyze_task_ids(events: list[dict]) -> set[str]:
    return {e["task_id"] for e in events if e["event"] == "analyze_enter" and e.get("task_id")}


def cmd_snapshot(args, deps_factory) -> int:
    log = EventLog(args.events, attempt=f"snapshot-{os.getpid()}")
    state = durable_snapshot(deps_factory(), args.thread_id, _analyze_task_ids(read_events(args.events)))
    log.emit("durable_snapshot", label=args.label, **state)
    print(json.dumps(state), flush=True)
    return EXIT_OK


def _check(ok, detail) -> dict:
    return {"ok": bool(ok), "detail": detail}


def evaluate(events: list[dict], expect: str, final_trace: dict | None, paused_trace: dict | None) -> dict:
    """Checks over the event log: one killed attempt, then a lifecycle RESUME
    (and, for c3, optionally a --direct-invoke diagnostic)."""
    attempts: dict[str, list[dict]] = {}
    for e in events:
        attempts.setdefault(e["attempt"], []).append(e)
    first = lambda role, hold_none: next(  # noqa: E731
        (evs for evs in attempts.values()
         if evs[0]["event"] == "attempt_start" and evs[0].get("role") == role
         and (evs[0].get("hold") == HOLD_NONE) == hold_none), [])
    killed, resumed, direct = first("resume", False), first("resume", True), first("langgraph-direct", True)
    names = lambda evs: [e["event"] for e in evs]  # noqa: E731
    exit_of = lambda evs: next((e for e in evs if e["event"] == "attempt_exit"), {})  # noqa: E731
    snapshots = {e["label"]: e for e in events if e["event"] == "durable_snapshot"}
    before, between = snapshots.get("before-kill", {}), snapshots.get("after-kill", {})
    hold_point = HOLD_AFTER_INFERENCE if expect == "c1" else HOLD_AFTER_ANALYZE_WRITES
    k, r, d = names(killed), names(resumed), names(direct)
    k_hold = next((e for e in killed if e["event"] == "holding"), {})
    paused_steps = {st["node_name"]: st for st in (paused_trace or {}).get("trace_steps") or []}
    final_steps = {st["node_name"]: st for st in
                   ((final_trace or {}).get("trace_steps") or exit_of(direct).get("trace_steps") or [])}
    retrieve_reused = (final_steps.get("retrieve") is not None and paused_steps.get("retrieve") is not None
                       and final_steps["retrieve"]["started_at"] == paused_steps["retrieve"]["started_at"])
    analyze_tasks = [t for t in between.get("tasks") or [] if t.get("name") == "analyze"]

    checks = {
        "killed_attempt_reached_hold": _check(
            k_hold.get("point") == hold_point and "attempt_exit" not in k and "hold_expired" not in k,
            {"hold": k_hold.get("point"), "events": k}),
        "killed_attempt_entered_analyze_and_got_response": _check(
            "analyze_enter" in k and "model_return" in k,
            {"request_ids": [e.get("request_id") for e in killed if e["event"] == "model_return"]}),
        "nothing_downstream_before_kill": _check(
            "downstream_during_hold" not in k and "validate_enter" not in k
            and not [e for e in killed if e["event"] == "checkpoint_committed"],
            {"after_hold": k[k.index("holding") + 1:] if "holding" in k else None}),
        "no_tripwire_hits": _check(
            "tripwire_hit" not in k + r + d,
            [e.get("what") for e in killed + resumed + direct if e["event"] == "tripwire_hit"]),
        "retrieval_reused_from_start": _check(
            retrieve_reused, {"retrieve_started_at": (final_steps.get("retrieve") or {}).get("started_at"),
                              "start_retrieve_started_at": (paused_steps.get("retrieve") or {}).get("started_at")}),
    }
    if expect == "c1":
        model_returns = [e for e in killed + resumed if e["event"] == "model_return"]
        checks.update({
            "durable_state_unchanged_by_killed_attempt": _check(
                before.get("checkpoint_id") and before.get("checkpoint_id") == between.get("checkpoint_id")
                and between.get("next") == ["analyze"] and not between.get("analyze_writes_stored"),
                {"before": {x: before.get(x) for x in ("checkpoint_id", "step", "next")},
                 "after_kill": {x: between.get(x) for x in ("checkpoint_id", "step", "next", "analyze_writes_stored")}}),
            "resume_completed": _check(exit_of(resumed).get("status") == "completed", exit_of(resumed) or None),
            "resume_reran_analyze_and_inference": _check(
                "analyze_enter" in r and "model_return" in r and len(model_returns) == 2,
                {"model_returns": [{"attempt": e["attempt"], "request_id": e.get("request_id"),
                                    "output_tokens": e.get("output_tokens")} for e in model_returns]}),
        })
    else:
        killed_analyze_return = next((e for e in killed if e["event"] == "analyze_return"), {})
        checks.update({
            "analyze_writes_stored_without_new_checkpoint": _check(
                "writes_committed" in k and between.get("analyze_writes_stored") is True
                and before.get("checkpoint_id") == between.get("checkpoint_id")
                and analyze_tasks and all(t["has_result"] for t in analyze_tasks),
                {"after_kill": {x: between.get(x) for x in ("checkpoint_id", "step", "next", "tasks")}}),
            "langgraph_reuses_stored_writes": _check(
                bool(direct) and "analyze_enter" not in d and "model_return" not in d and "validate_enter" in d
                and final_steps.get("analyze", {}).get("ended_at", "~") <= killed_analyze_return.get("ts", ""),
                {"direct_invoke_events": d or "not run",
                 "analyze_ended_at": (final_steps.get("analyze") or {}).get("ended_at"),
                 "killed_attempt_analyze_return_logged_at": killed_analyze_return.get("ts")}),
            "lifecycle_resume_continues_from_stored_writes": _check(
                exit_of(resumed).get("status") == "completed" and "analyze_enter" not in r
                and "model_return" not in r and "validate_enter" in r,
                {"resume_status": exit_of(resumed).get("status"), "resume_events": r}),
        })
    return checks


def cmd_verdict(args, deps_factory) -> int:
    from app.storage.risk_trace_store import get_risk_trace_by_identifier

    events = read_events(args.events)
    start_exit = next((e for e in events if e["event"] == "attempt_exit" and e["attempt"].startswith("start-")), {})
    resume_exit = next((e for e in reversed(events) if e["event"] == "attempt_exit"
                        and e["attempt"].startswith("resume-none-")), {})
    final_trace = (get_risk_trace_by_identifier(resume_exit["trace_id"])
                   if resume_exit.get("status") == "completed" else None)
    paused_trace = get_risk_trace_by_identifier(start_exit["trace_id"]) if start_exit.get("trace_id") else None
    checks = evaluate(events, args.expect, final_trace, paused_trace)
    final_state = durable_snapshot(deps_factory(), args.thread_id, _analyze_task_ids(events))
    result = {
        "experiment": f"C2b {args.expect}",
        "thread_id": args.thread_id,
        "final_durable_state": final_state,
        "final_outcome": {k: resume_exit.get(k) for k in (
            "status", "trace_id", "change_type", "eligibility_change_type", "action_eligible", "action_status")},
        "checks": checks,
        "passed": all(c["ok"] for c in checks.values()),
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
    attempt.add_argument("--direct-invoke", action="store_true",
                         help="diagnostic: continue with the graph's invoke(None), bypassing the lifecycle")

    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--thread-id", required=True)
    snapshot.add_argument("--events", type=Path, required=True)
    snapshot.add_argument("--label", required=True)

    verdict = commands.add_parser("verdict")
    verdict.add_argument("--thread-id", required=True)
    verdict.add_argument("--events", type=Path, required=True)
    verdict.add_argument("--expect", choices=("c1", "c3"), required=True)
    verdict.add_argument("--out", type=Path)
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
