#!/usr/bin/env python3
"""
Two-process durable-resume proof: pause after analysis, resume elsewhere.

Process A runs the real worker START with --pause-before validate. infer,
decide (triage model), retrieve (embeddings) and analyze (risk model) run,
their result is checkpointed in DynamoDB, and A exits. Process B is a new
interpreter: it installs tripwires on every retrieval, embedding and model
entry point, then runs the real worker RESUME on the same thread_id.

Two groups of acceptance checks, both required:

  resume  B reaches END (validate -> assess [-> act]) and writes supplier
          state without touching a tripwire: durable completion without
          repeated retrieval or inference.
  action  B's assessment matches the supplier state B resumed against, and
          the action happened if and only if that change is action-eligible
          (new_alert, escalated). A suppressed or downgraded run passes only
          if it created no ledger item.

  ownership  (owned lifecycle) A created the thread's lease at epoch 1, B
          resumed at epoch 2, and B's trace, the final marker and the
          released lease are all at epoch 2.

--expect-action required|skipped additionally fails the run unless the
assessed change is (or is not) action-eligible, e.g. to insist on covering
action execution; auto (default) accepts either.

USAGE
    AUTHORITY_TABLE=risk_agent_authority ACTIONS_TABLE=risk_agent_actions \\
        python scripts/durable_resume_proof.py run [--headline "..."]
            [--expect-action auto|required|skipped] [--out evidence.json]

    python scripts/durable_resume_proof.py guarded-resume --thread-id thread-<32 hex>
        Process B on its own: tripwires + worker RESUME. Prints the worker
        outcome line, then one {"tripwire_report": ...} line.

ENV
    --env-file (default docker/worker.env) supplies the worker configuration;
    variables already set in the caller's environment win, and .env is
    loaded without override as the worker itself does. Required:
    CHECKPOINT_TABLE, RISK_STATE_BACKEND=dynamodb, TRACE_STORE_BACKEND=dynamodb,
    AUTHORITY_TABLE, ACTIONS_TABLE. Governance is not bypassed: if authority
    or policy refuse open_review_ticket, B ends authority_denied and the
    proof fails.

EXIT
    0 every acceptance check passed; 1 a check failed; 2 preflight failed.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_HEADLINE = "TSMC halts production after earthquake in Taiwan"
DEFAULT_ENV_FILE = PROJECT_ROOT / "docker" / "worker.env"
REQUIRED_ENV = ("CHECKPOINT_TABLE", "AUTHORITY_TABLE", "ACTIONS_TABLE", "RISK_STATE_TABLE", "RISK_TRACES_TABLE")
ANALYSIS_NODES = ["infer", "decide", "retrieve", "analyze"]
RESUME_NODES = ["validate", "assess"]  # then "act" only when action-eligible
EXPECT_ACTION_CHOICES = ("auto", "required", "skipped")

# Model inference and managed retrieval. Any call to these from process B fails it.
BLOCKED_AWS_SERVICES = frozenset({"bedrock-runtime", "bedrock-agent-runtime"})
# Besides DynamoDB, process B may only resolve its own credentials (SSO profile).
CREDENTIAL_RESOLUTION_CALLS = frozenset({"sso:GetRoleCredentials"})


# ---- Tripwires (process B) ----

class TripwireError(RuntimeError):
    """A retrieval, embedding or model entry point was reached during resume."""


class _ModelTripwire:
    def __init__(self, name: str, trip):
        self._name = name
        self._trip = trip

    def invoke(self, *args, **kwargs):
        self._trip(f"model {self._name}.invoke")

    async def ainvoke(self, *args, **kwargs):
        self._trip(f"model {self._name}.ainvoke")

    def stream(self, *args, **kwargs):
        self._trip(f"model {self._name}.stream")

    def batch(self, *args, **kwargs):
        self._trip(f"model {self._name}.batch")


def install_resume_tripwires(setattr_=setattr) -> SimpleNamespace:
    """Make every retrieval, embedding and model path fail loudly and be recorded.

    Layers: the graph's LLM objects and retriever accessors, the embeddings
    factory, every httpx request (OpenAI / Bifrost transports), and botocore
    calls to Bedrock runtime services. All AWS calls are logged as
    service:operation. Pass monkeypatch.setattr to make it undoable in tests.
    """
    report = SimpleNamespace(hits=[], aws_calls=[])

    def trip(what: str):
        report.hits.append(what)
        raise TripwireError(what)

    import botocore.client
    original_api_call = botocore.client.BaseClient._make_api_call

    def guarded_api_call(client, operation_name, api_params):
        service = client.meta.service_model.service_name
        report.aws_calls.append(f"{service}:{operation_name}")
        if service in BLOCKED_AWS_SERVICES:
            trip(f"aws {service}:{operation_name}")
        return original_api_call(client, operation_name, api_params)

    setattr_(botocore.client.BaseClient, "_make_api_call", guarded_api_call)

    import httpx

    def no_http(client, request, *args, **kwargs):
        trip(f"http {request.method} {request.url.host}")

    async def no_async_http(client, request, *args, **kwargs):
        trip(f"http {request.method} {request.url.host}")

    setattr_(httpx.Client, "send", no_http)
    setattr_(httpx.AsyncClient, "send", no_async_http)

    from app.retrieval import retriever as retrieval
    from app.workflows import supplier_risk_agent as agent

    setattr_(agent, "_triage_llm", _ModelTripwire("triage", trip))
    setattr_(agent, "_risk_llm", _ModelTripwire("risk_analysis", trip))
    setattr_(agent, "_retriever", None)
    setattr_(agent, "_get_retriever", lambda: trip("retriever _get_retriever"))
    setattr_(agent, "get_retriever", lambda *a, **k: trip("retriever get_retriever"))
    setattr_(retrieval, "get_retriever", lambda *a, **k: trip("retriever get_retriever"))
    setattr_(retrieval, "build_embeddings", lambda *a, **k: trip("embeddings build_embeddings"))
    return report


def guarded_resume(thread_id: str) -> int:
    """Process B: tripwires first, then the unmodified worker RESUME."""
    process_started_at = time.time()
    report = install_resume_tripwires()
    from app.workers import risk_worker

    error = None
    try:
        exit_code = risk_worker.main(["resume", "--thread-id", thread_id])
    except Exception as exc:  # a tripwire or any other failure: still report
        exit_code, error = 1, f"{type(exc).__name__}: {exc}"
    print(json.dumps({"tripwire_report": {
        "pid": os.getpid(),
        "process_started_at": _iso(process_started_at),
        "worker_exit_code": exit_code,
        "error": error,
        "hits": report.hits,
        "aws_calls": report.aws_calls,
        "aws_services": sorted({call.split(":")[0] for call in report.aws_calls}),
    }}), flush=True)
    return exit_code if not report.hits else 1


# ---- Orchestrator ----

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


def _parse_ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _load_env_file(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _configure_env(env_file: Path) -> list[str]:
    """Caller env > env file > .env (no override). Returns preflight problems."""
    for key, value in _load_env_file(env_file).items():
        os.environ.setdefault(key, value)
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))

    problems = [f"{key} is not set" for key in REQUIRED_ENV if not os.environ.get(key)]
    for key in ("RISK_STATE_BACKEND", "TRACE_STORE_BACKEND"):
        if os.environ.get(key, "").lower() != "dynamodb":
            problems.append(f"{key} must be dynamodb (is {os.environ.get(key)!r})")
    return problems


def _run_child(args: list[str]) -> dict:
    started = time.time()
    proc = subprocess.Popen(args, cwd=PROJECT_ROOT, env=dict(os.environ),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = proc.communicate()
    ended = time.time()
    lines = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    try:
        os.kill(proc.pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    return {
        "pid": proc.pid,
        "returncode": proc.returncode,
        "started_at": _iso(started),
        "ended_at": _iso(ended),
        "started_ts": started,
        "ended_ts": ended,
        "alive_after_exit": alive,
        "outcome": next((line for line in lines if "status" in line), None),
        "tripwire_report": next((line["tripwire_report"] for line in lines if "tripwire_report" in line), None),
        "stderr_tail": stderr.strip().splitlines()[-15:],
    }


def _checkpoint_snapshot(saver, thread_id: str) -> dict:
    cfg = {"configurable": {"thread_id": thread_id}}
    latest = saver.get_tuple(cfg)
    if latest is None:
        return {"exists": False}
    values = latest.checkpoint["channel_values"]
    alert = values.get("alert") or {}
    return {
        "exists": True,
        "checkpoint_id": latest.checkpoint["id"],
        "checkpoint_count": sum(1 for _ in saver.list(cfg)),
        "metadata_step": latest.metadata.get("step"),
        "pending_branches": sorted(k for k in values if k.startswith("branch:to:")),
        "nodes": [s["node_name"] for s in values.get("trace_steps") or []],
        "trace_steps": values.get("trace_steps") or [],
        "tool_decision": values.get("tool_decision"),
        "context_chars": len(values.get("context") or ""),
        "retriever_metadata": values.get("retriever_metadata"),
        "alert": {k: alert.get(k) for k in ("status", "supplier", "risk_type", "risk_level", "impact")} if alert else None,
        "is_valid": values.get("is_valid"),
        "action_result": values.get("action_result"),
    }


def _state_rows(store) -> dict:
    return {f"{r['supplier']}#{r['risk_type']}": dict(r) for r in store.list_all()}


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def redact_evidence_text(text: str, hostname: str | None = None) -> str:
    """Evidence is committed: drop the host name (inside executor_id) and STS session names."""
    hostname = hostname or socket.gethostname()
    text = text.replace(hostname, "<host>")
    return re.sub(r"(arn:aws:sts::\d+:assumed-role/[^/\"]+/)[^\"]+", r"\1<session>", text)


def run(headline: str, env_file: Path, out: Path | None, expect_action: str = "auto") -> int:
    problems = _configure_env(env_file)
    if problems:
        print(json.dumps({"preflight": "failed", "problems": problems}, indent=2))
        return 2

    import boto3
    from app.governance.action_ledger import ActionLedger
    from app.governance.authority import AuthorityStore
    from app.storage.checkpoint_store import DynamoCheckpointSaver
    from app.storage.risk_trace_store import DynamoTraceStore
    from app.storage.supplier_risk_state_store import DynamoRiskStateStore

    region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
    identity = boto3.client("sts", region_name=region).get_caller_identity()
    saver = DynamoCheckpointSaver(os.environ["CHECKPOINT_TABLE"], region)
    state_store = DynamoRiskStateStore(os.environ["RISK_STATE_TABLE"], region)
    traces = DynamoTraceStore(os.environ["RISK_TRACES_TABLE"], region)
    ledger = ActionLedger(os.environ["ACTIONS_TABLE"], AuthorityStore(os.environ["AUTHORITY_TABLE"], region), region)

    state_before_a = _state_rows(state_store)
    from boto3.dynamodb.conditions import Key

    checkpoint_table = boto3.resource("dynamodb", region_name=region).Table(os.environ["CHECKPOINT_TABLE"])

    def ownership_items(thread):
        items = {i["sk"]: i for i in checkpoint_table.query(
            KeyConditionExpression=Key("thread_id").eq(thread) & Key("sk").between("final", "lease"),
            ConsistentRead=True)["Items"]}
        return items.get("lease"), items.get("final")

    # ---- Process A: START, pause before validate, exit ----
    a = _run_child([sys.executable, "-m", "app.workers.risk_worker", "start",
                    "--headline", headline, "--pause-before", "validate"])
    a_out = a["outcome"] or {}
    thread_id = a_out.get("thread_id")
    if not thread_id:
        print(json.dumps({"process_a": _jsonable(a), "error": "process A produced no thread"}, indent=2))
        return 1

    # ---- Between processes: only DynamoDB holds the workflow ----
    action_id = f"action#{thread_id}#open_review_ticket"
    between = {
        "checkpoint": _checkpoint_snapshot(saver, thread_id),
        "state_rows": _state_rows(state_store),
        "action_item": ledger.get(action_id),
        "trace_a": traces.get_by_identifier(a_out["trace_id"]),
    }

    # ---- Process B: tripwires + RESUME ----
    b = _run_child([sys.executable, str(Path(__file__).resolve()), "guarded-resume", "--thread-id", thread_id])
    b_out = b["outcome"] or {}

    after = {
        "checkpoint": _checkpoint_snapshot(saver, thread_id),
        "state_rows": _state_rows(state_store),
        "action_item": ledger.get(action_id),
        "trace_b": traces.get_by_identifier(b_out["trace_id"]) if b_out.get("trace_id") else None,
    }

    alert = between["checkpoint"].get("alert") or {}
    lease_after, final_after = ownership_items(thread_id)
    checks = {
        "resume": _resume_checks(headline, a, b, between, after, state_before_a),
        "ownership": ownership_checks(a_out, b_out, lease_after, final_after, after["trace_b"]),
        "action": action_checks(
            expect_action,
            alert=alert,
            headline=headline,
            prior_row=between["state_rows"].get(f"{alert.get('supplier')}#{alert.get('risk_type')}"),
            b_outcome=b_out,
            b_steps=(after["trace_b"] or {}).get("trace_steps") or [],
            action_item=after["action_item"],
            action_id=action_id,
        ),
    }
    from scripts.c2b_analysis_kill import code_identity

    evidence = _jsonable({
        "headline": headline,
        "code": code_identity(),
        "caller_arn": identity["Arn"],
        "region": region,
        "config": {k: os.environ.get(k) for k in (
            "LLM_PROVIDER", "BEDROCK_MODEL_ID", "EMBEDDING_PROVIDER", "RETRIEVER_PROVIDER",
            "RISK_STATE_BACKEND", "TRACE_STORE_BACKEND", *REQUIRED_ENV)},
        "thread_id": thread_id,
        "action_id": action_id,
        "process_a": {k: v for k, v in a.items() if k not in ("started_ts", "ended_ts")},
        "process_b": {k: v for k, v in b.items() if k not in ("started_ts", "ended_ts")},
        "between_processes": {
            "checkpoint": {k: v for k, v in between["checkpoint"].items() if k != "trace_steps"},
            "action_item": between["action_item"],
            "trace_a_workflow_event": (between["trace_a"] or {}).get("workflow_event"),
        },
        "after_b": {
            "checkpoint": {k: v for k, v in after["checkpoint"].items() if k != "trace_steps"},
            "action_item": after["action_item"],
            "trace_b_workflow_event": (after["trace_b"] or {}).get("workflow_event"),
            "trace_b_steps": (after["trace_b"] or {}).get("trace_steps"),
        },
        "expect_action": expect_action,
        "assessment": {k: b_out.get(k) for k in (
            "eligibility_change_type", "action_eligible", "eligibility_basis", "change_type")},
        "checks": checks,
        "passed": all(c["ok"] for group in checks.values() for c in group.values()),
    })
    text = redact_evidence_text(json.dumps(evidence, indent=2))
    print(text)
    if out:
        out.write_text(text + "\n")
    return 0 if evidence["passed"] else 1


def _check(ok: bool, detail) -> dict:
    return {"ok": bool(ok), "detail": detail}


def ownership_checks(a_out: dict, b_out: dict, lease: dict | None, final: dict | None,
                     trace_b: dict | None) -> dict:
    """Owned lifecycle: START at epoch 1, RESUME at epoch 2, everything B left behind at epoch 2."""
    lease, final, trace_b = lease or {}, final or {}, trace_b or {}
    epoch = lambda v: int(v) if v is not None else None  # noqa: E731  (DynamoDB returns Decimal)
    a_epoch, b_epoch = epoch(a_out.get("lease_epoch")), epoch(b_out.get("lease_epoch"))
    return {
        "a_started_at_epoch_1": _check(a_epoch == 1, {"a_epoch": a_epoch}),
        "b_resumed_at_the_next_epoch": _check(a_epoch is not None and b_epoch == a_epoch + 1,
                                              {"a_epoch": a_epoch, "b_epoch": b_epoch}),
        "b_trace_and_final_marker_at_b_epoch": _check(
            epoch(trace_b.get("lease_epoch")) == b_epoch and final.get("trace_id") == b_out.get("trace_id")
            and epoch(final.get("epoch")) == b_epoch,
            {"trace_epoch": trace_b.get("lease_epoch"), "final_trace_id": final.get("trace_id"),
             "final_epoch": final.get("epoch"), "b_trace_id": b_out.get("trace_id")}),
        "lease_released_at_b_epoch": _check(
            lease.get("state") == "RELEASED" and epoch(lease.get("epoch")) == b_epoch,
            {"state": lease.get("state"), "epoch": lease.get("epoch")}),
    }


def action_checks(expect: str, *, alert: dict, headline: str, prior_row: dict | None, b_outcome: dict,
                  b_steps: list[dict], action_item: dict | None, action_id: str) -> dict:
    """Did B act exactly when the assessed business change requires an action?

    The expected change is recomputed from the supplier-state row B resumed
    against (read between the processes), with the workflow's own classifier.
    """
    from app.workflows.supplier_risk_agent import ACTION_ELIGIBLE_CHANGE_TYPES, classify_risk_change

    expected_change, _, _ = classify_risk_change(alert, headline, prior_row)
    eligible = expected_change in ACTION_ELIGIBLE_CHANGE_TYPES
    action = action_item or {}
    nodes = [s["node_name"] for s in b_steps]
    assess_step = next((s for s in b_steps if s["node_name"] == "assess"), {})

    if eligible:
        action_ok = (
            action.get("status") == "COMPLETED"
            and action.get("claimed_by_trace_id") == b_outcome.get("trace_id")
            and (action.get("effect") or {}).get("correlation_id") == action_id
            and b_outcome.get("action_status") == "COMPLETED"
            and nodes[-2:] == ["assess", "act"]
        )
    else:
        action_ok = (
            action_item is None
            and b_outcome.get("action_id") is None
            and "act" not in nodes
            and assess_step.get("decision") == "no_action"
        )

    return {
        "eligibility_assessed_by_b": _check(
            b_outcome.get("eligibility_basis") == "assessed"
            and b_outcome.get("eligibility_change_type") == expected_change
            and b_outcome.get("action_eligible") is eligible,
            {"expected_change": expected_change, "expected_eligible": eligible,
             "assessed_change": b_outcome.get("eligibility_change_type"),
             "assessed_eligible": b_outcome.get("action_eligible"),
             "basis": b_outcome.get("eligibility_basis"),
             "prior_risk_level": (prior_row or {}).get("current_risk_level")}),
        "action_matches_eligibility": _check(
            action_ok,
            {"expected": "action completed by B" if eligible else "no action",
             "ledger_status": action.get("status"),
             "claimed_by_trace_id": action.get("claimed_by_trace_id"),
             "authority_version": action.get("authority_version"),
             "policy_version": action.get("policy_version"),
             "ticket_id": (action.get("effect") or {}).get("ticket_id"),
             "b_nodes_after_validate": nodes[nodes.index("validate") + 1:] if "validate" in nodes else None}),
        "expected_action_mode": _check(
            expect == "auto" or (expect == "required") == eligible,
            {"expect": expect, "eligible": eligible}),
    }


def _resume_checks(headline, a, b, between, after, state_before_a) -> dict:
    from app.storage.risk_trace_store import trace_display_status

    a_out, b_out = a["outcome"] or {}, b["outcome"] or {}
    tripwires = b["tripwire_report"] or {}
    chk_between, chk_after = between["checkpoint"], after["checkpoint"]
    alert = chk_between.get("alert") or {}
    key = f"{alert.get('supplier')}#{alert.get('risk_type')}"
    row_after = after["state_rows"].get(key) or {}
    b_steps = (after["trace_b"] or {}).get("trace_steps") or []
    b_nodes = [s["node_name"] for s in b_steps]
    resumed_nodes = b_nodes[len(ANALYSIS_NODES):]
    a_steps_by_node = {s["node_name"]: s for s in chk_between.get("trace_steps") or []}
    non_dynamodb_calls = {c for c in tripwires.get("aws_calls") or [] if not c.startswith("dynamodb:")}

    return {
        "same_thread_id": _check(
            a_out.get("thread_id") == b_out.get("thread_id"),
            [a_out.get("thread_id"), b_out.get("thread_id")]),
        "different_trace_id": _check(
            a_out.get("trace_id") and b_out.get("trace_id") and a_out["trace_id"] != b_out["trace_id"],
            [a_out.get("trace_id"), b_out.get("trace_id")]),
        "different_executor_and_pid": _check(
            a_out.get("executor_id") != b_out.get("executor_id") and a["pid"] != b["pid"],
            {"a": [a_out.get("executor_id"), a["pid"]], "b": [b_out.get("executor_id"), b["pid"]]}),
        "process_a_exited_before_b_started": _check(
            a["returncode"] == 0 and not a["alive_after_exit"] and a["ended_ts"] < b["started_ts"],
            {"a_exit": a["returncode"], "a_ended_at": a["ended_at"], "b_started_at": b["started_at"]}),
        "a_paused_after_analysis": _check(
            a_out.get("status") == "paused" and a_out.get("next") == ["validate"],
            {"status": a_out.get("status"), "next": a_out.get("next")}),
        "analysis_saved_before_b": _check(
            chk_between.get("nodes") == ANALYSIS_NODES
            and chk_between.get("tool_decision") == "retrieve"
            and chk_between.get("context_chars", 0) > 0
            and chk_between.get("retriever_metadata") is not None
            and alert.get("status") == "ok" and chk_between.get("is_valid") is None,
            {"nodes": chk_between.get("nodes"), "alert": alert,
             "context_chars": chk_between.get("context_chars"),
             "pending_branches": chk_between.get("pending_branches")}),
        "no_business_or_action_write_by_a": _check(
            between["state_rows"].get(key) == state_before_a.get(key) and between["action_item"] is None,
            {"row_before_a": state_before_a.get(key), "row_after_a": between["state_rows"].get(key),
             "action_item_after_a": between["action_item"]}),
        "no_retrieval_embedding_or_model_call_in_b": _check(
            tripwires.get("hits") == [] and tripwires.get("error") is None
            and not (set(tripwires.get("aws_services") or []) & BLOCKED_AWS_SERVICES)
            and not non_dynamodb_calls - CREDENTIAL_RESOLUTION_CALLS,
            {"hits": tripwires.get("hits"), "aws_services": tripwires.get("aws_services"),
             "non_dynamodb_calls": sorted(non_dynamodb_calls),
             "aws_call_count": len(tripwires.get("aws_calls") or [])}),
        "b_reused_checkpointed_steps": _check(
            b_nodes[:len(ANALYSIS_NODES)] == ANALYSIS_NODES
            and resumed_nodes in (RESUME_NODES, RESUME_NODES + ["act"])
            and all(s == a_steps_by_node.get(s["node_name"]) for s in b_steps[:len(ANALYSIS_NODES)])
            and all(_parse_ts(s["started_at"]) < b["started_ts"] for s in b_steps[:len(ANALYSIS_NODES)])
            and all(_parse_ts(s["started_at"]) >= b["started_ts"] for s in b_steps[len(ANALYSIS_NODES):]),
            {"b_trace_nodes": b_nodes,
             "analyze_started_at": a_steps_by_node.get("analyze", {}).get("started_at"),
             "validate_started_at": next((s["started_at"] for s in b_steps if s["node_name"] == "validate"), None),
             "b_process_started_at": b["started_at"]}),
        "workflow_completed": _check(
            b["returncode"] == 0 and b_out.get("status") == "completed" and b_out.get("next") == []
            and chk_after.get("nodes") == b_nodes,
            {"status": b_out.get("status"), "exit": b["returncode"], "next": b_out.get("next"),
             "final_status": b_out.get("final_status")}),
        "business_result_persisted": _check(
            b_out.get("state_written") is True
            and row_after.get("last_headline") == headline
            and row_after.get("current_risk_level") == alert.get("risk_level")
            and _parse_ts(row_after.get("last_seen_at", "1970-01-01T00:00:00+00:00")) >= int(b["started_ts"]),
            {"change_type": b_out.get("change_type"), "row": row_after}),
        "paused_trace_not_displayed_as_completed": _check(
            (between["trace_a"] or {}).get("final_status") == "ok"
            and trace_display_status(between["trace_a"] or {}) == "paused (workflow not completed)",
            {"final_status": (between["trace_a"] or {}).get("final_status"),
             "displayed_status": trace_display_status(between["trace_a"] or {})}),
        "traces_recorded": _check(
            (between["trace_a"] or {}).get("workflow_event") == "paused"
            and (after["trace_b"] or {}).get("workflow_event") == "resumed_completed",
            {"a": (between["trace_a"] or {}).get("workflow_event"),
             "b": (after["trace_b"] or {}).get("workflow_event")}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run_cmd = commands.add_parser("run", help="orchestrate process A (start) and process B (guarded resume)")
    run_cmd.add_argument("--headline", default=DEFAULT_HEADLINE)
    run_cmd.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    run_cmd.add_argument("--expect-action", choices=EXPECT_ACTION_CHOICES, default="auto")
    run_cmd.add_argument("--out", type=Path)
    resume_cmd = commands.add_parser("guarded-resume", help="process B: tripwires + worker resume")
    resume_cmd.add_argument("--thread-id", required=True)
    args = parser.parse_args(argv)

    if args.command == "guarded-resume":
        return guarded_resume(args.thread_id)
    return run(args.headline, args.env_file, args.out, args.expect_action)


if __name__ == "__main__":
    sys.exit(main())
