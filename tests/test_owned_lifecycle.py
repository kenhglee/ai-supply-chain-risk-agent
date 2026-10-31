"""Owned START / RESUME lifecycle (moto, real compiled graph, fake models; in-process).

Crashes are simulated in-process (a store call raises); process kills and
cross-process contention are in tests/test_ownership_processes.py.
"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from app.storage import risk_trace_store  # noqa: E402
from app.storage.thread_lease import Lease, LeaseStore, OwnershipRequired  # noqa: E402
from app.workflows import risk_workflow  # noqa: E402
from app.workflows import supplier_risk_agent as agent  # noqa: E402
from tests.test_risk_workflow import (  # noqa: E402,F401  (env is a fixture)
    CHECKPOINTS,
    HEADLINE,
    MOTO_AVAILABLE,
    REGION,
    _calls,
    _new_executor,
    _nodes,
    _seed_state,
    _start_paused,
    _state_row,
    _trace,
    _unfenced_executor,
    env,
)

pytestmark = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")


class SimulatedCrash(Exception):
    pass


def _lease(thread_id):
    return LeaseStore(CHECKPOINTS, REGION).read(thread_id)


def _final(deps, thread_id):
    return deps["ownership"].finals.read(thread_id)


def _count_ticket_effects(env):
    effects = []
    real = agent.create_servicenow_ticket
    env.monkeypatch.setattr(agent, "create_servicenow_ticket",
                            lambda event, decision: effects.append(1) or real(event, decision))
    return effects


# ---- Acquisition, contention, lineage, release ----

def test_owned_attempts_acquire_release_and_report_lineage(env):
    started = _start_paused(env)
    assert started["lease_epoch"] == 1 and _lease(started["thread_id"])["state"] == "RELEASED"
    deps = _new_executor()
    outcome = risk_workflow.resume_workflow(started["thread_id"], **deps)

    assert (outcome["status"], outcome["lease_epoch"]) == ("completed", 2)
    own_last = deps["checkpointer"].last_checkpoint[(started["thread_id"], outcome["trace_id"], 2)]
    assert outcome["lineage_checkpoint_id"] == own_last
    marker = _final(deps, started["thread_id"])
    assert (marker["trace_id"], marker["epoch"], marker["final_checkpoint_id"]) == (outcome["trace_id"], 2, own_last)
    trace = _trace(outcome["trace_id"])
    assert (trace["lease_epoch"], trace["lineage_checkpoint_id"]) == (2, own_last)
    lease = _lease(started["thread_id"])
    assert (lease["epoch"], lease["state"]) == (2, "RELEASED")


def test_resume_of_a_held_thread_is_contended_and_runs_nothing(env):
    started = _start_paused(env)
    LeaseStore(CHECKPOINTS, REGION).acquire(started["thread_id"], "someone-else", executor="other")

    outcome = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())

    assert outcome["status"] == "contended"
    assert outcome["holder"]["owner"] == "someone-else"
    assert _calls(env) == (1, 1, 0)  # no inference
    assert env.supplier_state.scan()["Items"] == []
    assert _trace(outcome["trace_id"]) is None  # contention writes no trace


def test_already_completed_reports_the_finalizer_not_the_latest_checkpoint(env):
    started = _start_paused(env)
    completed = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())
    again = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())

    assert again["status"] == "already_completed"
    assert (again["trace_id"], again["finalized_by_trace_id"]) == (completed["trace_id"], completed["trace_id"])
    assert (again["change_type"], again["state_written"], again["lease_epoch"]) == ("new_alert", False, 2)
    assert _lease(started["thread_id"])["epoch"] == 3  # every attempt acquires; release keeps the epoch


def test_unfenced_lifecycle_requires_an_explicit_baseline_opt_in(env):
    deps = _unfenced_executor()
    del deps["unfenced_baseline"]
    with pytest.raises(OwnershipRequired):
        risk_workflow.start_workflow({"headline": HEADLINE}, **deps)


# ---- Lost ownership ----

def _takeover_during(env, thread_id, when):
    """Make another worker take the thread over at a chosen moment of this attempt."""
    clock = {"ms": None}
    store = LeaseStore(CHECKPOINTS, REGION, now_ms=lambda: clock["ms"])

    def take_over():
        lease = store.read(thread_id)
        clock["ms"] = lease["expires_at_ms"] + 1
        store.acquire(thread_id, "owner-B", executor="B")
    return take_over


def test_lost_ownership_overrides_a_propagating_error_and_prevents_finalization(env):
    started = _start_paused(env)
    take_over = _takeover_during(env, started["thread_id"], "analyze")

    def analyze_fails_after_takeover(prompt):
        env.risk.calls += 1
        take_over()
        raise RuntimeError("model call failed")

    env.monkeypatch.setattr(agent, "_risk_llm", type("R", (), {"invoke": staticmethod(analyze_fails_after_takeover)}))
    deps = _new_executor()
    deps["ownership"].heartbeat_interval_s = 0  # stalled owner
    outcome = risk_workflow.resume_workflow(started["thread_id"], **deps)

    # The node's RuntimeError propagated, the error write was fenced out, and the
    # attempt reports lease_lost rather than raising or succeeding.
    assert (outcome["status"], outcome["error_type"]) == ("lease_lost", "RuntimeError")
    assert _final(deps, started["thread_id"]) is None and env.supplier_state.scan()["Items"] == []
    assert _lease(started["thread_id"])["owner"] == "owner-B"  # not released by the stale worker
    assert _trace(outcome["trace_id"])["workflow_event"] == "lease_lost"


def test_action_completion_after_ownership_loss_is_recorded_but_the_stale_worker_cannot_advance(env):
    started = _start_paused(env)
    effects = _count_ticket_effects(env)
    take_over = _takeover_during(env, started["thread_id"], "effect")
    real_ticket = agent.open_review_ticket

    def ticket_then_lose_the_thread(alert, headline, action_id):
        result = real_ticket(alert, headline, action_id)
        take_over()  # ownership is lost while the admitted effect is finishing
        return result

    env.monkeypatch.setattr(agent, "open_review_ticket", ticket_then_lose_the_thread)
    deps = _new_executor()
    deps["ownership"].heartbeat_interval_s = 0
    stale = risk_workflow.resume_workflow(started["thread_id"], **deps)

    assert stale["status"] == "lease_lost"
    [action] = env.actions.scan()["Items"]
    assert action["status"] == "COMPLETED" and len(effects) == 1  # completion was recorded
    assert _final(deps, started["thread_id"]) is None and env.supplier_state.scan()["Items"] == []
    snapshot = agent.build_app(deps["checkpointer"], action_gate=deps["action_gate"],
                               risk_reader=deps["risk_store"]).get_state({"configurable": {
                                   "thread_id": started["thread_id"]}})
    assert snapshot.next == ("act",)  # the stale worker did not advance the graph

    env.monkeypatch.setattr(agent, "open_review_ticket", real_ticket)
    held = _lease(started["thread_id"])  # worker B finishes with the thread
    LeaseStore(CHECKPOINTS, REGION).release(Lease(started["thread_id"], "owner-B", held["epoch"], held["expires_at_ms"]))
    later = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())
    assert (later["status"], later["action_status"]) == ("completed", "COMPLETED")
    assert len(effects) == 1  # replayed, not re-executed
    assert _final(_new_executor(), started["thread_id"])["trace_id"] == later["trace_id"]


def test_in_flight_action_is_a_structured_outcome(env):
    started = _start_paused(env)
    action_id = agent.review_ticket_action_id(started["thread_id"])
    env.actions.put_item(Item={"action_id": action_id, "status": "IN_FLIGHT", "claimed_by_trace_id": "dead-worker",
                               "thread_id": started["thread_id"]})

    outcome = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())

    assert (outcome["status"], outcome["next"], outcome["error_type"]) == (
        "action_in_flight", ["act"], "ActionInFlight")
    assert _lease(started["thread_id"])["state"] == "RELEASED"
    assert _trace(outcome["trace_id"])["workflow_event"] == "action_in_flight"
    assert env.supplier_state.scan()["Items"] == []


# ---- Recovery ----

def test_end_without_finalization_is_finalized_by_the_next_owner(env):
    started = _start_paused(env)
    effects = _count_ticket_effects(env)
    deps = _new_executor()
    real_commit = deps["ownership"].finals.commit

    def crash_before_finalization(*args, **kwargs):
        raise SimulatedCrash("process died after END, before finalization")

    deps["ownership"].finals.commit = crash_before_finalization
    with pytest.raises(SimulatedCrash):
        risk_workflow.resume_workflow(started["thread_id"], **deps)
    assert env.supplier_state.scan()["Items"] == [] and len(effects) == 1
    deps["ownership"].finals.commit = real_commit

    recovered = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())

    assert (recovered["status"], recovered["recovered_finalization"]) == ("completed", True)
    assert recovered["change_type"] == "new_alert" and len(effects) == 1  # act not re-run
    assert _state_row()["current_risk_level"] == "High"
    assert _trace(recovered["trace_id"])["workflow_event"] == "recovered_completed"
    assert _final(deps, started["thread_id"])["recovered"] is True


def test_finalized_without_trace_has_the_trace_recovered_with_the_original_trace_id(env):
    started = _start_paused(env)
    real_once = risk_trace_store.append_risk_trace_once

    def crash_before_completion_trace(record):
        if record.get("workflow_event") == "resumed_completed":
            raise SimulatedCrash("process died after finalization, before the trace")
        return real_once(record)

    env.monkeypatch.setattr(risk_workflow, "append_risk_trace_once", crash_before_completion_trace)
    with pytest.raises(SimulatedCrash):
        risk_workflow.resume_workflow(started["thread_id"], **_new_executor())
    env.monkeypatch.setattr(risk_workflow, "append_risk_trace_once", real_once)
    marker = _final(_new_executor(), started["thread_id"])
    assert _trace(marker["trace_id"]) is None
    [row_before] = env.supplier_state.scan()["Items"]

    again = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())

    assert (again["status"], again["trace_recovered"], again["trace_id"]) == (
        "already_completed", True, marker["trace_id"])
    trace = _trace(marker["trace_id"])
    assert (trace["workflow_event"], trace["lease_epoch"], trace["trace_recovered"]) == (
        "resumed_completed", 2, True)
    assert trace["recovered_by"] == again_trace_owner(again) and trace["recovered_by"] != marker["trace_id"]
    assert env.supplier_state.scan()["Items"] == [row_before]  # no second business write


def again_trace_owner(outcome):
    """The recovering attempt's trace_id is not reported on already_completed; read it from the lease."""
    return _lease(outcome["thread_id"])["owner"]


# ---- Legacy threads (completed before ownership) ----

def _legacy_completed(env):
    """A thread finished by the pre-ownership lifecycle: no lease, no final marker."""
    return risk_workflow.start_workflow({"headline": HEADLINE, "alert_id": "alert-1"}, **_unfenced_executor())


def test_legacy_completed_thread_is_recognized_from_evidence_without_refinalizing(env):
    legacy = _legacy_completed(env)
    [row_before] = env.supplier_state.scan()["Items"]

    outcome = risk_workflow.resume_workflow(legacy["thread_id"], **_new_executor())

    assert (outcome["status"], outcome["legacy_recognized"], outcome["trace_id"]) == (
        "already_completed", True, legacy["trace_id"])
    marker = _final(_new_executor(), legacy["thread_id"])
    assert (marker["kind"], marker["trace_id"]) == ("legacy_recognized", legacy["trace_id"])
    assert env.supplier_state.scan()["Items"] == [row_before]
    assert risk_workflow.resume_workflow(legacy["thread_id"], **_new_executor())["legacy_recognized"] is True


def test_legacy_completion_without_its_trace_is_unverified_and_untouched(env):
    legacy = _legacy_completed(env)
    env.traces.delete_item(Key={"trace_id": legacy["trace_id"]})
    [row_before] = env.supplier_state.scan()["Items"]

    outcome = risk_workflow.resume_workflow(legacy["thread_id"], **_new_executor())

    assert outcome["status"] == "completion_unverified"
    assert outcome["unverified_reasons"] == ["completion_trace_missing"]
    assert _final(_new_executor(), legacy["thread_id"]) is None
    assert env.supplier_state.scan()["Items"] == [row_before]
    assert _trace(outcome["trace_id"])["workflow_event"] == "completion_unverified"


def test_legacy_forked_history_is_unverified(env):
    legacy = _legacy_completed(env)
    checkpoints = sorted((i for i in env.checkpoints.scan()["Items"] if i["sk"].startswith("chk#")),
                         key=lambda i: i["sk"])
    sibling = dict(checkpoints[2])  # a second child of the same parent, older than the latest
    sibling["checkpoint_id"] = sibling["checkpoint_id"][:-1] + ("0" if sibling["checkpoint_id"][-1] != "0" else "1")
    sibling["sk"] = f"chk##{sibling['checkpoint_id']}"
    assert sibling["sk"] < checkpoints[-1]["sk"]
    env.checkpoints.put_item(Item=sibling)

    outcome = risk_workflow.resume_workflow(legacy["thread_id"], **_new_executor())

    assert outcome["status"] == "completion_unverified"
    assert "forked_history" in outcome["unverified_reasons"]


def test_moto_serialization_is_per_operation_not_per_attempt(env):
    """tests/conftest.py locks each moto call, never a whole attempt: other operations proceed mid-attempt."""
    import threading
    import time

    from tests.test_risk_workflow import OK_ALERT

    started = _start_paused(env)
    inside, release, result = threading.Event(), threading.Event(), {}

    def held_risk_model(prompt):
        env.risk.calls += 1
        inside.set()
        assert release.wait(20)
        return json.dumps(OK_ALERT)

    env.monkeypatch.setattr(agent, "_risk_llm", type("R", (), {"invoke": staticmethod(held_risk_model)}))
    worker = threading.Thread(target=lambda: result.update(
        outcome=risk_workflow.resume_workflow(started["thread_id"], **_new_executor())))
    worker.start()
    try:
        assert inside.wait(20)
        t0 = time.monotonic()
        lease = _lease(started["thread_id"])  # reads and writes from another thread while the attempt is running
        env.supplier_state.put_item(Item={"pk": "Other#flood", "current_risk_level": "Low"})
        env.supplier_state.delete_item(Key={"pk": "Other#flood"})
        assert time.monotonic() - t0 < 2
        assert (lease["state"], lease["epoch"]) == ("HELD", 2)
    finally:
        release.set()
        worker.join(30)
    assert result["outcome"]["status"] == "completed"
