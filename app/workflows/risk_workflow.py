"""Explicit START / RESUME lifecycle for checkpointed supplier risk workflows.

thread_id identifies the durable workflow; each START or RESUME is a separate
execution attempt with its own trace_id. RESUME never accepts workflow input,
so a completed thread can't be re-run by accident (invoke(new_input, thread)
would restart it from the beginning).

RESUME continues from the saved pending node and writes nothing into the
workflow state first: the attempt's trace_id (and, when owned, its lease
token) travel in configurable, which also records them in each checkpoint's
metadata. Rewriting state on RESUME would re-run the last node's routing under
the current graph and could send a parked thread somewhere else.

Same-thread ownership (docs/SAME_THREAD_OWNERSHIP_DESIGN.md). Every attempt
built from production dependencies (checkpoint_dependencies_from_env) owns
the thread for its whole run:

  acquire    START creates the lease (epoch 1); RESUME takes it if it is
             absent, released or expired, else returns `contended` and runs
             nothing. The attempt's trace_id is the owner.
  fence      checkpoint and pending writes, action admission, replay and
             denial, and finalization all ConditionCheck the lease (owner and
             epoch); a stale owner gets LeaseLost.
  heartbeat  renews the lease for the whole attempt.
  finalize   business state and a once-only `final` marker in one transaction.
  shutdown   writers drained (invoke returned) -> settle/finalize/trace ->
             heartbeat stopped -> conditional release. If the saver recorded
             lost ownership (LangGraph can swallow a background writer's
             error while another exception propagates), the attempt is
             `lease_lost`: no finalization, no successful outcome, no release.

Recovery on RESUME of a thread with no pending tasks:
  - `final` present -> already_completed, reported from the marker; a missing
    completion trace is rewritten with the original trace_id (trace_recovered)
  - no `final`, final checkpoint written under the protocol (lease_epoch in its
    metadata) -> finalize now (recovered_finalization)
  - no `final`, legacy final checkpoint -> recognized from historical evidence
    (legacy marker, no business write) or `completion_unverified`; never
    finalized again automatically

Guarantees: durable continuation with at-least-once node execution (a node
interrupted mid-run runs again). Ownership fences admission and durable
workflow mutations; an effect admitted before a takeover may still complete
afterwards (its completion is recorded), and an IN_FLIGHT action found by a
new owner is reported as action_in_flight (reconciliation is out of scope).

Action eligibility: valid "ok" alerts go through assess (read-only) and only
new_alert / escalated reach act. eligibility_change_type (decided before the
action) is reported separately from the write-time change_type.

The unfenced lifecycle remains available only as an explicit baseline
(unfenced_baseline=True) for tests that reproduce today's races.
"""
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.governance.action_ledger import ActionInFlight, UnconfiguredActionGate, action_gate_from_env
from app.governance.authority import AuthorityDenied
from app.storage.checkpoint_store import DynamoCheckpointSaver
from app.storage.finalization import FINALIZED, LEGACY_RECOGNIZED, AlreadyFinalized, FinalizationStore
from app.storage.risk_trace_store import (
    CONFLICT,
    append_risk_trace,
    append_risk_trace_once,
    get_risk_trace_by_trace_id,
)
from app.storage.supplier_risk_state_store import get_risk_store
from app.storage.thread_lease import (
    DEFAULT_TTL_MS,
    LeaseContended,
    LeaseHeartbeat,
    LeaseLost,
    LeaseStore,
    OwnershipRequired,
    WriteConflict,
    WriteOutcomeUnknown,
)
from app.workflows.supplier_risk_agent import (
    LEGACY_UNASSESSED,
    STATE_WRITE_CHANGE_TYPES,
    build_app,
    build_trace_record,
    classify_risk_change,
    finalize_completed_run,
    get_executor_id,
    initial_risk_state,
    is_actionable_alert,
    record_ignored_alert,
    risk_state_key,
)

logger = logging.getLogger(__name__)

# Outcome statuses
PAUSED = "paused"
COMPLETED = "completed"
ALREADY_COMPLETED = "already_completed"
NOT_FOUND = "not_found"
IGNORED = "ignored"
AUTHORITY_DENIED = "authority_denied"
CONTENDED = "contended"
LEASE_LOST = "lease_lost"
ACTION_IN_FLIGHT = "action_in_flight"
COMPLETION_UNVERIFIED = "completion_unverified"
ATTEMPT_FAILED = "attempt_failed"

COMPLETED_EVENTS = ("started_completed", "resumed_completed", "recovered_completed")

# Shared by every executor adapter (Lambda handler, container worker).
START_PAUSE_BEFORE = ["analyze"]
THREAD_ID_PATTERN = re.compile(r"thread-[0-9a-f]{32}")
OUTCOME_FIELDS = (
    "status", "thread_id", "trace_id", "next", "change_type",
    "state_written", "final_status", "supplier",
    "action_id", "action_status", "authority_version",
    "eligibility_change_type", "action_eligible", "eligibility_basis",
    # Same-thread ownership and lineage.
    "lease_epoch", "lineage_checkpoint_id", "finalized_by_trace_id", "recovered_finalization",
    "trace_recovered", "legacy_recognized", "unverified_reasons", "holder", "error_type",
)


@dataclass
class Ownership:
    """Lease and finalization stores for owned attempts.

    heartbeat_interval_s: None renews every ttl/3; 0 disables the heartbeat
    (tests only: it stands in for a stalled owner).
    """
    leases: Any
    finals: Any
    heartbeat_interval_s: float | None = None
    executor: str = field(default_factory=get_executor_id)


def checkpoint_dependencies_from_env() -> dict:
    """Owned saver, stores, action gate and ownership, built fresh by each caller.

    Ownership is always enabled here: there is no fallback to unfenced writes.
    Missing configuration is an error, and missing permissions surface as AWS
    errors at the first fenced operation. A missing AUTHORITY_TABLE /
    ACTIONS_TABLE yields a gate that denies every privileged action.
    """
    table_name = os.environ.get("CHECKPOINT_TABLE")
    if not table_name:
        raise RuntimeError("CHECKPOINT_TABLE is not configured")
    for variable in ("RISK_STATE_BACKEND", "TRACE_STORE_BACKEND"):
        if os.environ.get(variable, "").lower() != "dynamodb":
            raise RuntimeError(f"the checkpointed lifecycle requires {variable}=dynamodb "
                               "(atomic finalization and write-once traces)")
    risk_store = get_risk_store()
    ttl_ms = int(os.environ.get("LEASE_TTL_MS", DEFAULT_TTL_MS))
    return {
        "checkpointer": DynamoCheckpointSaver(table_name, fenced=True),
        "risk_store": risk_store,
        "action_gate": action_gate_from_env(lease_table=table_name),
        "ownership": Ownership(LeaseStore(table_name, ttl_ms=ttl_ms),
                               FinalizationStore(table_name, risk_store.table.name)),
    }


def _gate(action_gate):
    """No gate supplied means no governance configuration: deny (fail closed)."""
    return action_gate if action_gate is not None else UnconfiguredActionGate()


def _has_pending_tasks(snapshot) -> bool:
    """Whether the thread still has work, i.e. is not complete.

    In LangGraph 1.2.x, StateSnapshot.tasks holds every task scheduled from
    the latest checkpoint, while StateSnapshot.next names only those without
    stored successful writes. Only an empty tasks tuple means the graph
    reached END.
    """
    return bool(snapshot.tasks)


def _config(thread_id: str, trace_id: str | None = None, lease=None, checkpoint_id: str | None = None) -> dict:
    configurable = {"thread_id": thread_id}
    if trace_id:
        configurable["trace_id"] = trace_id  # this attempt; copied into checkpoint metadata
    if lease is not None:
        configurable.update(lease_owner=lease.owner, lease_epoch=lease.epoch)
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
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
             change_type=None, action=None, **extra) -> dict:
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
        **extra,
    }


# ======================================================================
# Unfenced baseline (explicit opt-in; reproduces today's races in tests)
# ======================================================================

def _settle(app, thread_id: str, trace_id: str, risk_store, *,
            t_run_start: float, completed_event: str) -> dict:
    """Baseline: decide paused vs completed from the latest checkpoint."""
    cfg = _config(thread_id)
    run_duration_ms = round((time.time() - t_run_start) * 1000, 1)
    snapshot = app.get_state(cfg)
    trace_extras = {"thread_id": thread_id, "executor_id": get_executor_id()}

    if _has_pending_tasks(snapshot):
        append_risk_trace(build_trace_record(
            snapshot.values, alert_id=snapshot.values["alert_id"], trace_id=trace_id,
            headline=snapshot.values["headline"], t_run_start=t_run_start, run_duration_ms=run_duration_ms,
            change_type=None,
            trace_extras={**trace_extras, **_eligibility_fields(snapshot.values, snapshot.next),
                          "workflow_event": PAUSED},
        ))
        return _outcome(PAUSED, thread_id=thread_id, trace_id=trace_id,
                        next_nodes=snapshot.next, values=snapshot.values)

    values = snapshot.values
    enriched = finalize_completed_run(
        {}, values, risk_store, alert_id=values["alert_id"], trace_id=trace_id, headline=values["headline"],
        t_run_start=t_run_start, run_duration_ms=run_duration_ms,
        trace_extras={**trace_extras, **_action_fields(values), **_policy_fields(values),
                      **_eligibility_fields(values), "workflow_event": completed_event},
    )
    return _outcome(COMPLETED, thread_id=thread_id, trace_id=trace_id,
                    values=values, change_type=enriched["change_type"])


def _denied(app, thread_id: str, trace_id: str, denial: AuthorityDenied, *, t_run_start: float,
            cfg=None, lease=None) -> dict:
    """Authority refused act: no effect, no business write; the thread stays at act."""
    snapshot = app.get_state(cfg or _config(thread_id))
    action = {"action_id": denial.action_id, "action_status": "DENIED",
              "authority_version": denial.authority_version}
    ownership = _ownership_fields(lease, snapshot)
    append_risk_trace(build_trace_record(
        snapshot.values, alert_id=snapshot.values["alert_id"], trace_id=trace_id,
        headline=snapshot.values["headline"], t_run_start=t_run_start,
        run_duration_ms=round((time.time() - t_run_start) * 1000, 1), change_type=None,
        trace_extras={
            "thread_id": thread_id, "executor_id": get_executor_id(), "workflow_event": AUTHORITY_DENIED,
            "authority_status": denial.status, "denied_layer": denial.layer,
            "policy_version": denial.policy_version, "policy_sha256": denial.policy_sha256,
            **action, **_eligibility_fields(snapshot.values, snapshot.next), **ownership,
        },
    ))
    return _outcome(AUTHORITY_DENIED, thread_id=thread_id, trace_id=trace_id, next_nodes=snapshot.next,
                    values=snapshot.values, action=action, **ownership)


def _start_unfenced(row, *, checkpointer, risk_store, action_gate, pause_before) -> dict:
    headline = row["headline"]
    alert_id = row.get("alert_id") or uuid.uuid4().hex
    trace_id = uuid.uuid4().hex
    thread_id = f"thread-{uuid.uuid4().hex}"
    if checkpointer.get_tuple(_config(thread_id)) is not None:
        raise RuntimeError(f"thread_id collision: {thread_id} already has checkpoints")
    app = build_app(checkpointer, action_gate=_gate(action_gate), risk_reader=risk_store)
    t_run_start = time.time()
    try:
        app.invoke(initial_risk_state(alert_id, thread_id, trace_id, headline), _config(thread_id, trace_id),
                   interrupt_before=pause_before, durability="sync")
    except AuthorityDenied as denial:
        return _denied(app, thread_id, trace_id, denial, t_run_start=t_run_start)
    return _settle(app, thread_id, trace_id, risk_store, t_run_start=t_run_start, completed_event="started_completed")


def _resume_unfenced(thread_id, *, checkpointer, risk_store, action_gate) -> dict:
    app = build_app(checkpointer, action_gate=_gate(action_gate), risk_reader=risk_store)
    snapshot = app.get_state(_config(thread_id))
    if not _has_pending_tasks(snapshot):
        last_trace_id = (snapshot.metadata or {}).get("trace_id") or snapshot.values.get("trace_id")
        return _outcome(ALREADY_COMPLETED, thread_id=thread_id, trace_id=last_trace_id, values=snapshot.values)
    trace_id = uuid.uuid4().hex
    t_run_start = time.time()
    try:
        app.invoke(None, _config(thread_id, trace_id), durability="sync")
    except AuthorityDenied as denial:
        return _denied(app, thread_id, trace_id, denial, t_run_start=t_run_start)
    return _settle(app, thread_id, trace_id, risk_store, t_run_start=t_run_start, completed_event="resumed_completed")


# ======================================================================
# Owned lifecycle
# ======================================================================

def _ownership_fields(lease, snapshot=None, lineage_checkpoint_id=None) -> dict:
    if lease is None:
        return {}
    checkpoint_id = lineage_checkpoint_id or (
        snapshot.config["configurable"].get("checkpoint_id") if snapshot is not None else None)
    return {"lease_epoch": lease.epoch, "lineage_checkpoint_id": checkpoint_id}


class _Attempt:
    """One owned execution attempt: lease, heartbeat, lineage and shutdown."""

    def __init__(self, lease, trace_id: str, *, checkpointer, risk_store, action_gate, ownership: Ownership):
        self.lease, self.trace_id = lease, trace_id
        self.thread_id = lease.thread_id
        self.checkpointer, self.risk_store, self.ownership = checkpointer, risk_store, ownership
        self.token = (lease.thread_id, lease.owner, lease.epoch)
        self.app = build_app(checkpointer, action_gate=_gate(action_gate), risk_reader=risk_store)
        self.cfg = _config(self.thread_id, trace_id, lease)
        self.t_run_start = time.time()
        self.heartbeat = None
        interval = ownership.heartbeat_interval_s
        if interval != 0:
            self.heartbeat = LeaseHeartbeat(ownership.leases, lease, interval_s=interval).start()

    # ---- lineage ----

    def lost(self) -> LeaseLost | None:
        return getattr(self.checkpointer, "lost", {}).get(self.token)

    def lineage_snapshot(self):
        """The state this attempt produced: its own last checkpoint, else the latest one."""
        own = getattr(self.checkpointer, "last_checkpoint", {}).get(self.token)
        return self.app.get_state(_config(self.thread_id, checkpoint_id=own) if own else _config(self.thread_id))

    def trace_extras(self, snapshot=None, **extra) -> dict:
        return {"thread_id": self.thread_id, "executor_id": get_executor_id(),
                **_ownership_fields(self.lease, snapshot), **extra}

    def record(self, values, event: str, *, change_type=None, snapshot=None, trace_id=None, once=False, **extra):
        record = build_trace_record(
            values, alert_id=values.get("alert_id"), trace_id=trace_id or self.trace_id,
            headline=values.get("headline"), t_run_start=self.t_run_start,
            run_duration_ms=round((time.time() - self.t_run_start) * 1000, 1), change_type=change_type,
            trace_extras=self.trace_extras(snapshot, workflow_event=event, **extra),
        )
        if once:
            return append_risk_trace_once(record)
        append_risk_trace(record)
        return None

    # ---- shutdown ----

    def stop_heartbeat(self) -> None:
        if self.heartbeat is not None:
            self.heartbeat.stop()

    def release(self) -> bool:
        try:
            return self.ownership.leases.release(self.lease)
        except Exception:  # the lease expires on its own
            logger.exception("lease release failed for %s", self.thread_id)
            return False


def _settle_owned(attempt: _Attempt, *, completed_event: str) -> dict:
    """Paused or completed, from this attempt's own lineage; finalize only if still the owner."""
    if (lost := attempt.lost()) is not None:
        raise lost
    snapshot = attempt.lineage_snapshot()
    if _has_pending_tasks(snapshot):
        attempt.record(snapshot.values, PAUSED, snapshot=snapshot, **_eligibility_fields(snapshot.values, snapshot.next))
        return _outcome(PAUSED, thread_id=attempt.thread_id, trace_id=attempt.trace_id, next_nodes=snapshot.next,
                        values=snapshot.values, **_ownership_fields(attempt.lease, snapshot))
    return _finalize_owned(attempt, snapshot, completed_event=completed_event, recovered=False)


def _finalize_owned(attempt: _Attempt, snapshot, *, completed_event: str, recovered: bool) -> dict:
    """Business state + the once-only final marker in one fenced transaction, then the trace."""
    values = snapshot.values
    alert = values.get("alert") or {}
    key = risk_state_key(alert)
    prior = attempt.risk_store.get(*key) if key else None
    change_type, _, row = classify_risk_change(alert, values.get("headline"), prior)
    action = _action_fields(values)
    lineage = snapshot.config["configurable"].get("checkpoint_id")
    marker = {
        "kind": FINALIZED, "trace_id": attempt.trace_id, "change_type": change_type,
        "workflow_event": completed_event, "final_checkpoint_id": lineage,
        "eligibility_change_type": _eligibility_fields(values)["eligibility_change_type"],
        "action_id": action["action_id"], "action_status": action["action_status"],
        "risk_level": alert.get("risk_level"), "recovered": recovered,
        "finalized_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    try:
        attempt.ownership.finals.commit(attempt.lease, marker, (key[0], key[1], row) if row else None)
    except AlreadyFinalized as already:
        return _from_marker(attempt, already.marker, values)
    trace = attempt.record(
        values, completed_event, change_type=change_type, snapshot=snapshot, once=True,
        **_action_fields(values), **_policy_fields(values), **_eligibility_fields(values),
        **({"recovered_finalization": True} if recovered else {}),
    )
    if trace == CONFLICT:
        logger.warning("completion trace %s already exists with different content", attempt.trace_id)
    return _outcome(COMPLETED, thread_id=attempt.thread_id, trace_id=attempt.trace_id, values=values,
                    change_type=change_type, finalized_by_trace_id=attempt.trace_id,
                    recovered_finalization=recovered, **_ownership_fields(attempt.lease, snapshot))


def _from_marker(attempt: _Attempt, marker: dict, values: dict, **extra) -> dict:
    """already_completed, reported from the final marker's lineage (not the latest checkpoint)."""
    return _outcome(ALREADY_COMPLETED, thread_id=attempt.thread_id, trace_id=marker.get("trace_id"),
                    values=values, change_type=marker.get("change_type"),
                    state_written=False,  # this attempt wrote nothing; change_type is the finalizer's
                    finalized_by_trace_id=marker.get("trace_id"),
                    lease_epoch=marker.get("epoch"), lineage_checkpoint_id=marker.get("final_checkpoint_id"),
                    legacy_recognized=marker.get("kind") == LEGACY_RECOGNIZED, **extra)


def _complete_or_recover(attempt: _Attempt, snapshot) -> dict:
    """RESUME found no pending tasks: report, recover finalization or trace, or apply the legacy rule."""
    marker = attempt.ownership.finals.read(attempt.thread_id)
    if marker is not None:
        if marker.get("kind") == FINALIZED and get_risk_trace_by_trace_id(marker["trace_id"]) is None:
            _recover_trace(attempt, marker, snapshot)
            return _from_marker(attempt, marker, snapshot.values, trace_recovered=True)
        return _from_marker(attempt, marker, snapshot.values)
    if (snapshot.metadata or {}).get("lease_epoch") is not None:
        # Reached END under the protocol but never finalized (the owner died or lost the lease).
        return _finalize_owned(attempt, snapshot, completed_event="recovered_completed", recovered=True)
    return _legacy_completion(attempt, snapshot)


def _recover_trace(attempt: _Attempt, marker: dict, snapshot) -> None:
    """Rewrite the completion trace with the finalizing attempt's trace_id and epoch."""
    values = snapshot.values
    record = build_trace_record(
        values, alert_id=values.get("alert_id"), trace_id=marker["trace_id"], headline=values.get("headline"),
        t_run_start=attempt.t_run_start, run_duration_ms=0.0, change_type=marker.get("change_type"),
        trace_extras={
            "thread_id": attempt.thread_id, "executor_id": get_executor_id(),
            "workflow_event": marker.get("workflow_event") or "resumed_completed",
            "lease_epoch": marker.get("epoch"), "lineage_checkpoint_id": marker.get("final_checkpoint_id"),
            "recovered_by": attempt.trace_id, "trace_recovered": True,
            **_action_fields(values), **_policy_fields(values), **_eligibility_fields(values),
        },
    )
    if append_risk_trace_once(record) == CONFLICT:
        logger.warning("recovered trace %s conflicts with an existing record", marker["trace_id"])


def _legacy_completion(attempt: _Attempt, snapshot) -> dict:
    """A thread completed before ownership existed: recognize it from evidence, never refinalize."""
    reasons = []
    parents = [t.parent_config["configurable"]["checkpoint_id"]
               for t in attempt.checkpointer.list(_config(attempt.thread_id)) if t.parent_config]
    if len(parents) != len(set(parents)):
        reasons.append("forked_history")
    source_trace_id = (snapshot.metadata or {}).get("trace_id") or snapshot.values.get("trace_id")
    trace = get_risk_trace_by_trace_id(source_trace_id) if source_trace_id else None
    if not source_trace_id:
        reasons.append("no_completing_trace_id")
    elif trace is None or trace.get("trace_id") != source_trace_id:
        reasons.append("completion_trace_missing")
    else:
        if trace.get("thread_id") != attempt.thread_id:
            reasons.append("completion_trace_thread_mismatch")
        if trace.get("workflow_event") not in ("started_completed", "resumed_completed"):
            reasons.append("completion_trace_event_mismatch")
        if not trace.get("change_type"):
            reasons.append("completion_trace_change_type_missing")
    values = snapshot.values
    if reasons:
        attempt.record(values, COMPLETION_UNVERIFIED, snapshot=snapshot, unverified_reasons=",".join(reasons))
        return _outcome(COMPLETION_UNVERIFIED, thread_id=attempt.thread_id, trace_id=attempt.trace_id,
                        values=values, unverified_reasons=reasons, **_ownership_fields(attempt.lease, snapshot))
    marker = {"kind": LEGACY_RECOGNIZED, "trace_id": source_trace_id, "change_type": trace.get("change_type"),
              "workflow_event": trace.get("workflow_event"),
              "final_checkpoint_id": snapshot.config["configurable"].get("checkpoint_id"),
              "recognized_by": attempt.trace_id,
              "finalized_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
    try:
        marker = attempt.ownership.finals.commit(attempt.lease, marker, None)
    except AlreadyFinalized as already:
        marker = already.marker
    return _from_marker(attempt, marker, values)


def _run_owned(attempt: _Attempt, run, *, completed_event: str) -> dict:
    """Run one owned attempt and apply the shutdown order; see module doc."""
    outcome, error, lost_by_exception = None, None, None
    try:
        outcome = run()
        if outcome is None:  # the graph ran: invoke returned, so its writers are drained
            outcome = _settle_owned(attempt, completed_event=completed_event)
    except LeaseLost as exc:
        lost_by_exception = exc
    except AuthorityDenied as denial:
        if attempt.lost() is None:
            outcome = _denied(attempt.app, attempt.thread_id, attempt.trace_id, denial,
                              t_run_start=attempt.t_run_start, cfg=_config(attempt.thread_id), lease=attempt.lease)
    except ActionInFlight as exc:
        if attempt.lost() is None:
            snapshot = attempt.app.get_state(_config(attempt.thread_id))
            attempt.record(snapshot.values, ACTION_IN_FLIGHT, snapshot=snapshot, error_type=type(exc).__name__)
            outcome = _outcome(ACTION_IN_FLIGHT, thread_id=attempt.thread_id, trace_id=attempt.trace_id,
                               next_nodes=snapshot.next, values=snapshot.values, error_type=type(exc).__name__,
                               **_ownership_fields(attempt.lease, snapshot))
    except (WriteConflict, WriteOutcomeUnknown) as exc:
        if attempt.lost() is None:
            outcome = _attempt_failed(attempt, exc)
    except BaseException as exc:  # noqa: BLE001 - re-raised below unless ownership was lost
        error = exc
    finally:
        attempt.stop_heartbeat()

    lost = attempt.lost() or lost_by_exception
    if lost is not None:
        # Lost ownership overrides every other result, including a propagating error.
        return _lease_lost(attempt, error)
    attempt.release()
    if error is not None:
        raise error
    return outcome


def _attempt_failed(attempt: _Attempt, exc: Exception) -> dict:
    try:
        snapshot = attempt.app.get_state(_config(attempt.thread_id))
        attempt.record(snapshot.values, ATTEMPT_FAILED, snapshot=snapshot, error_type=type(exc).__name__)
        values, next_nodes = snapshot.values, snapshot.next
    except Exception:
        logger.exception("could not record attempt_failed for %s", attempt.thread_id)
        values, next_nodes = None, ()
    return _outcome(ATTEMPT_FAILED, thread_id=attempt.thread_id, trace_id=attempt.trace_id, next_nodes=next_nodes,
                    values=values, error_type=type(exc).__name__, lease_epoch=attempt.lease.epoch)


def _lease_lost(attempt: _Attempt, error: BaseException | None) -> dict:
    """No finalization, no successful outcome, no release; an unfenced audit trace only."""
    values, next_nodes = None, ()
    try:
        snapshot = attempt.app.get_state(_config(attempt.thread_id))
        values, next_nodes = snapshot.values, snapshot.next
        attempt.record(values, LEASE_LOST, snapshot=snapshot, once=True,
                       error_type=type(error).__name__ if error else None)
    except Exception:
        logger.exception("could not record lease_lost for %s", attempt.thread_id)
    return _outcome(LEASE_LOST, thread_id=attempt.thread_id, trace_id=attempt.trace_id, next_nodes=next_nodes,
                    values=values, lease_epoch=attempt.lease.epoch,
                    error_type=type(error).__name__ if error else None)


def _require_ownership(ownership, unfenced_baseline: bool) -> None:
    if ownership is None and not unfenced_baseline:
        raise OwnershipRequired("the checkpointed lifecycle requires ownership; pass unfenced_baseline=True "
                                "only to reproduce unprotected behavior in tests")


def start_workflow(row: dict, *, checkpointer, risk_store, action_gate=None, ownership: Ownership | None = None,
                   pause_before: list[str] | None = None, unfenced_baseline: bool = False) -> dict:
    """Start a new workflow for one alert row on a fresh thread_id, owning it (epoch 1)."""
    headline = row["headline"]
    alert_id = row.get("alert_id") or uuid.uuid4().hex
    _require_ownership(ownership, unfenced_baseline)

    if not is_actionable_alert(headline):
        trace_id = uuid.uuid4().hex
        record_ignored_alert(row, alert_id=alert_id, trace_id=trace_id, headline=headline,
                             trace_extras={"executor_id": get_executor_id()})
        return _outcome(IGNORED, thread_id=None, trace_id=trace_id, change_type="ignored")
    if ownership is None:
        return _start_unfenced(row, checkpointer=checkpointer, risk_store=risk_store, action_gate=action_gate,
                               pause_before=pause_before)

    trace_id = uuid.uuid4().hex
    thread_id = f"thread-{uuid.uuid4().hex}"
    if checkpointer.get_tuple(_config(thread_id)) is not None:
        raise RuntimeError(f"thread_id collision: {thread_id} already has checkpoints")
    try:
        lease = ownership.leases.acquire(thread_id, trace_id, executor=ownership.executor, new_thread=True)
    except LeaseContended:
        raise RuntimeError(f"thread_id collision: {thread_id} already has a lease") from None
    attempt = _Attempt(lease, trace_id, checkpointer=checkpointer, risk_store=risk_store,
                       action_gate=action_gate, ownership=ownership)

    def run():
        attempt.app.invoke(initial_risk_state(alert_id, thread_id, trace_id, headline), attempt.cfg,
                           interrupt_before=pause_before, durability="sync")

    return _run_owned(attempt, run, completed_event="started_completed")


def resume_workflow(thread_id: str, *, checkpointer, risk_store, action_gate=None,
                    ownership: Ownership | None = None, unfenced_baseline: bool = False) -> dict:
    """Continue an existing workflow from its latest checkpoint as a new, owned attempt."""
    _require_ownership(ownership, unfenced_baseline)
    if checkpointer.get_tuple(_config(thread_id)) is None:
        return _outcome(NOT_FOUND, thread_id=thread_id, trace_id=None)
    if ownership is None:
        return _resume_unfenced(thread_id, checkpointer=checkpointer, risk_store=risk_store, action_gate=action_gate)

    trace_id = uuid.uuid4().hex
    try:
        lease = ownership.leases.acquire(thread_id, trace_id, executor=ownership.executor)
    except LeaseContended as contended:
        holder = contended.holder
        return _outcome(CONTENDED, thread_id=thread_id, trace_id=trace_id,
                        holder={k: holder.get(k) for k in ("owner", "owner_executor", "epoch", "expires_at_ms")})
    attempt = _Attempt(lease, trace_id, checkpointer=checkpointer, risk_store=risk_store,
                       action_gate=action_gate, ownership=ownership)

    def run():
        snapshot = attempt.app.get_state(_config(thread_id))
        if not _has_pending_tasks(snapshot):
            return _complete_or_recover(attempt, snapshot)
        attempt.app.invoke(None, attempt.cfg, durability="sync")
        return None

    return _run_owned(attempt, run, completed_event="resumed_completed")
