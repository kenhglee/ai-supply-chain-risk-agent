"""Explicit START / RESUME lifecycle for checkpointed supplier risk workflows.

thread_id identifies the durable workflow; each START or RESUME is a separate
execution attempt with its own trace_id. RESUME never accepts workflow input,
so a completed thread can't be re-run by accident (invoke(new_input, thread)
would restart it from the beginning).

RESUME continues from the saved pending node and writes nothing into the
workflow state first: the attempt's trace_id travels in config
(configurable.trace_id), which also records it in each checkpoint's metadata.
Rewriting state on RESUME would re-run the last node's routing under the
current graph and could send a parked thread somewhere else.

Guarantees: durable continuation with at-least-once node execution. A node
interrupted mid-run is re-executed on resume. Completing the graph and
writing supplier state are not atomic: if the process dies after the graph
reaches END but before finalize_completed_run writes state, RESUME reports
already_completed and that state write is lost.

Action eligibility: the checkpointed graph routes valid "ok" alerts through
assess, which reads supplier state (read-only) and classifies the business
change. Only new_alert and escalated reach act; suppressed and downgraded end
without an action. The assessment (eligibility_change_type) is checkpointed
and reported separately from the write-time change_type, which
finalize_completed_run computes when it writes supplier state after END.

Privileged actions (Phase 3D): eligible alerts go through the governed act
node. If external authority refuses admission,
act raises AuthorityDenied, the thread stays at next=["act"] and the
attempt is reported as authority_denied; a later RESUME (after authority is
restored) re-runs only act. Missing governance configuration denies.
"""
import os
import re
import time
import uuid

from app.governance.action_ledger import UnconfiguredActionGate, action_gate_from_env
from app.governance.authority import AuthorityDenied
from app.storage.checkpoint_store import DynamoCheckpointSaver
from app.storage.supplier_risk_state_store import get_risk_store
from app.workflows.supplier_risk_agent import (
    LEGACY_UNASSESSED,
    STATE_WRITE_CHANGE_TYPES,
    build_app,
    build_trace_record,
    finalize_completed_run,
    get_executor_id,
    initial_risk_state,
    is_actionable_alert,
    record_ignored_alert,
)
from app.storage.risk_trace_store import append_risk_trace

# Outcome statuses
PAUSED = "paused"
COMPLETED = "completed"
ALREADY_COMPLETED = "already_completed"
NOT_FOUND = "not_found"
IGNORED = "ignored"
AUTHORITY_DENIED = "authority_denied"

# Shared by every executor adapter (Lambda handler, container worker).
START_PAUSE_BEFORE = ["analyze"]
THREAD_ID_PATTERN = re.compile(r"thread-[0-9a-f]{32}")
OUTCOME_FIELDS = (
    "status", "thread_id", "trace_id", "next", "change_type",
    "state_written", "final_status", "supplier",
    "action_id", "action_status", "authority_version",
    "eligibility_change_type", "action_eligible", "eligibility_basis",
)


def checkpoint_dependencies_from_env() -> dict:
    """Saver, risk store and action gate, built fresh by each caller.

    Continuity between executors comes only from DynamoDB + thread_id.
    A missing CHECKPOINT_TABLE is a configuration error, not bad input.
    A missing AUTHORITY_TABLE / ACTIONS_TABLE yields a gate that denies
    every privileged action (fail closed).
    """
    table_name = os.environ.get("CHECKPOINT_TABLE")
    if not table_name:
        raise RuntimeError("CHECKPOINT_TABLE is not configured")
    return {
        "checkpointer": DynamoCheckpointSaver(table_name),
        "risk_store": get_risk_store(),
        "action_gate": action_gate_from_env(),
    }


def _gate(action_gate):
    """No gate supplied means no governance configuration: deny (fail closed)."""
    return action_gate if action_gate is not None else UnconfiguredActionGate()


def _has_pending_tasks(snapshot) -> bool:
    """Whether the thread still has work, i.e. is not complete.

    In LangGraph 1.2.x, StateSnapshot.tasks holds every task scheduled from
    the latest checkpoint, while StateSnapshot.next names only those without
    stored successful writes. If a process dies after a node's writes are
    stored but before the next checkpoint, next is () while that node's task
    still holds its result; invoke(None) reapplies the writes without
    re-running the node. Only an empty tasks tuple means the graph reached END.
    """
    return bool(snapshot.tasks)


def _config(thread_id: str, trace_id: str | None = None) -> dict:
    configurable = {"thread_id": thread_id}
    if trace_id:
        configurable["trace_id"] = trace_id  # this attempt; copied into checkpoint metadata
    return {"configurable": configurable}


def _action_fields(values: dict | None) -> dict:
    result = (values or {}).get("action_result") or {}
    return {
        "action_id": result.get("action_id"),
        "action_status": result.get("status"),
        "authority_version": result.get("authority_version"),
    }


def _eligibility_fields(values: dict | None, next_nodes=()) -> dict:
    """The pre-action assessment, separate from the write-time change_type.

    A thread parked at act with no assessment predates the assess node and
    keeps legacy behavior; say so explicitly.
    """
    assessment = (values or {}).get("assessment")
    if assessment is None and "act" in next_nodes:
        assessment = {"basis": LEGACY_UNASSESSED}
    assessment = assessment or {}
    return {
        "eligibility_change_type": assessment.get("eligibility_change_type"),
        "action_eligible": assessment.get("action_eligible"),
        "eligibility_basis": assessment.get("basis"),
    }


def _policy_fields(values: dict | None) -> dict:
    """Trace-only: the materialized policy version/hash that admitted the action."""
    result = (values or {}).get("action_result") or {}
    return {"policy_version": result.get("policy_version"), "policy_sha256": result.get("policy_sha256")}


def _outcome(status: str, *, thread_id, trace_id, next_nodes=(), values=None,
             change_type=None, action=None) -> dict:
    alert = (values or {}).get("alert") or {}
    return {
        "status": status,
        "thread_id": thread_id,
        "trace_id": trace_id,
        "next": list(next_nodes),
        "change_type": change_type,
        "state_written": change_type in STATE_WRITE_CHANGE_TYPES,
        "final_status": alert.get("status"),
        "supplier": alert.get("supplier"),
        "risk_type": alert.get("risk_type"),
        "risk_level": alert.get("risk_level"),
        **(action or _action_fields(values)),
        **_eligibility_fields(values, next_nodes),
    }


def _settle(app, thread_id: str, trace_id: str, risk_store, *,
            t_run_start: float, completed_event: str) -> dict:
    """Decide paused vs completed from the checkpoint before any business write."""
    cfg = _config(thread_id)
    run_duration_ms = round((time.time() - t_run_start) * 1000, 1)
    snapshot = app.get_state(cfg)
    trace_extras = {"thread_id": thread_id, "executor_id": get_executor_id()}

    if _has_pending_tasks(snapshot):
        append_risk_trace(build_trace_record(
            snapshot.values,
            alert_id=snapshot.values["alert_id"],
            trace_id=trace_id,
            headline=snapshot.values["headline"],
            t_run_start=t_run_start,
            run_duration_ms=run_duration_ms,
            change_type=None,
            trace_extras={**trace_extras, **_eligibility_fields(snapshot.values, snapshot.next),
                          "workflow_event": PAUSED},
        ))
        return _outcome(PAUSED, thread_id=thread_id, trace_id=trace_id,
                        next_nodes=snapshot.next, values=snapshot.values)

    values = snapshot.values
    enriched = finalize_completed_run(
        {},
        values,
        risk_store,
        alert_id=values["alert_id"],
        trace_id=trace_id,
        headline=values["headline"],
        t_run_start=t_run_start,
        run_duration_ms=run_duration_ms,
        trace_extras={**trace_extras, **_action_fields(values), **_policy_fields(values),
                      **_eligibility_fields(values), "workflow_event": completed_event},
    )
    return _outcome(COMPLETED, thread_id=thread_id, trace_id=trace_id,
                    values=values, change_type=enriched["change_type"])


def _denied(app, thread_id: str, trace_id: str, denial: AuthorityDenied, *, t_run_start: float) -> dict:
    """Authority refused act: no effect, no business write; the thread stays at act."""
    cfg = _config(thread_id)
    snapshot = app.get_state(cfg)
    action = {
        "action_id": denial.action_id,
        "action_status": "DENIED",
        "authority_version": denial.authority_version,
    }
    append_risk_trace(build_trace_record(
        snapshot.values,
        alert_id=snapshot.values["alert_id"],
        trace_id=trace_id,
        headline=snapshot.values["headline"],
        t_run_start=t_run_start,
        run_duration_ms=round((time.time() - t_run_start) * 1000, 1),
        change_type=None,
        trace_extras={
            "thread_id": thread_id,
            "executor_id": get_executor_id(),
            "workflow_event": AUTHORITY_DENIED,
            "authority_status": denial.status,
            "denied_layer": denial.layer,
            "policy_version": denial.policy_version,
            "policy_sha256": denial.policy_sha256,
            **action,
            **_eligibility_fields(snapshot.values, snapshot.next),
        },
    ))
    return _outcome(AUTHORITY_DENIED, thread_id=thread_id, trace_id=trace_id,
                    next_nodes=snapshot.next, values=snapshot.values, action=action)


def start_workflow(row: dict, *, checkpointer, risk_store, action_gate=None,
                   pause_before: list[str] | None = None) -> dict:
    """Start a new workflow for one alert row on a fresh thread_id."""
    headline = row["headline"]
    alert_id = row.get("alert_id") or uuid.uuid4().hex
    trace_id = uuid.uuid4().hex

    if not is_actionable_alert(headline):
        record_ignored_alert(
            row,
            alert_id=alert_id,
            trace_id=trace_id,
            headline=headline,
            trace_extras={"executor_id": get_executor_id()},
        )
        return _outcome(IGNORED, thread_id=None, trace_id=trace_id, change_type="ignored")

    thread_id = f"thread-{uuid.uuid4().hex}"
    cfg = _config(thread_id)
    if checkpointer.get_tuple(cfg) is not None:
        raise RuntimeError(f"thread_id collision: {thread_id} already has checkpoints")

    app = build_app(checkpointer, action_gate=_gate(action_gate), risk_reader=risk_store)
    t_run_start = time.time()
    try:
        app.invoke(
            initial_risk_state(alert_id, thread_id, trace_id, headline),
            _config(thread_id, trace_id),
            interrupt_before=pause_before,
            durability="sync",
        )
    except AuthorityDenied as denial:
        return _denied(app, thread_id, trace_id, denial, t_run_start=t_run_start)
    return _settle(app, thread_id, trace_id, risk_store,
                   t_run_start=t_run_start, completed_event="started_completed")


def resume_workflow(thread_id: str, *, checkpointer, risk_store, action_gate=None) -> dict:
    """Continue an existing workflow from its latest checkpoint as a new attempt."""
    cfg = _config(thread_id)
    if checkpointer.get_tuple(cfg) is None:
        return _outcome(NOT_FOUND, thread_id=thread_id, trace_id=None)

    app = build_app(checkpointer, action_gate=_gate(action_gate), risk_reader=risk_store)
    snapshot = app.get_state(cfg)
    if not _has_pending_tasks(snapshot):
        # The attempt that completed it: checkpoint metadata, else (older threads) state.
        last_trace_id = (snapshot.metadata or {}).get("trace_id") or snapshot.values.get("trace_id")
        return _outcome(ALREADY_COMPLETED, thread_id=thread_id,
                        trace_id=last_trace_id, values=snapshot.values)

    trace_id = uuid.uuid4().hex
    t_run_start = time.time()
    try:
        app.invoke(None, _config(thread_id, trace_id), durability="sync")
    except AuthorityDenied as denial:
        return _denied(app, thread_id, trace_id, denial, t_run_start=t_run_start)
    return _settle(app, thread_id, trace_id, risk_store,
                   t_run_start=t_run_start, completed_event="resumed_completed")
