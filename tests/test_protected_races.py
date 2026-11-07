"""R1-R4 under the integrated owned lifecycle, plus stale-owner and crash/takeover cases.

Separate processes (tests/race_harness.py --ownership) against one persistent
SQLite database; see tests/test_ownership_processes.py for how the stand-ins
mirror DynamoDB. The unprotected versions of R1-R4 stay in
tests/test_concurrent_resume.py as explicit unfenced baselines.

"Stalled" workers run with --no-heartbeat and a 1 ms lease, so another worker
can take the thread over while the stalled one is held at a gate; that is how
a stale owner is produced deterministically.
"""
import json
import time

import pytest

from tests.test_concurrent_resume import Race

OWN = ["--ownership"]
STALLED = ["--ownership", "--lease-ttl-ms", "1", "--no-heartbeat"]
SHORT_LEASE_MS = 300


@pytest.fixture
def race(tmp_path):
    race = Race(tmp_path)
    yield race
    race.cleanup()


def _effects(race):
    return [e for e in race.events() if e["event"] == "effect_executed"]


def _finals(race):
    return [json.loads(r["marker"]) for r in race.query("SELECT marker FROM final")]


def _state(race):
    return [json.loads(r["row"]) for r in race.query("SELECT row FROM supplier_state")]


def _forks(race, thread_id):
    rows = race.query("SELECT parent_id FROM checkpoints WHERE thread_id = ? AND parent_id IS NOT NULL", thread_id)
    parents = [r["parent_id"] for r in rows]
    return len(parents) != len(set(parents))


def _completion_traces(race, thread_id):
    return [t for t in race.traces() if t.get("thread_id") == thread_id
            and t.get("workflow_event") in ("started_completed", "resumed_completed", "recovered_completed")]


def _exit(race, worker):
    return race.first(worker, "attempt_exit") or {}


# ---- R1: simultaneous resume -> one owner, one inference, no fork, one finalization ----

def test_r1_protected_single_execution_without_fork(race):
    thread_id = race.start_paused(ownership=True)
    for w, level in (("A", "High"), ("B", "Medium")):
        race.spawn(w, "resume", thread_id=thread_id, risk_level=level, gates=["before-acquire"], extra=OWN)
    for w in "AB":
        race.wait_at(w, "before-acquire")
    for w in "AB":
        race.release(w, "before-acquire")
    codes = {w: race.wait_exit(w) for w in "AB"}

    owner = next(w for w in "AB" if race.first(w, "acquired"))
    loser = "B" if owner == "A" else "A"
    assert codes == {owner: 0, loser: 5} and _exit(race, loser)["status"] == "contended"
    assert sum(race.names(w).count("model_invoke") for w in "AB") == 1
    assert not _forks(race, thread_id)
    [marker] = _finals(race)
    assert marker["trace_id"] == _exit(race, owner)["trace_id"]
    assert [s["current_risk_level"] for s in _state(race)] == [_exit(race, owner)["risk_level"]]
    assert len(_completion_traces(race, thread_id)) == 1


# ---- R2: a second worker while the owner is inside the effect -> contended, not an exception ----

def test_r2_protected_contention_while_the_owner_is_inside_the_effect(race):
    thread_id = race.start_paused(ownership=True)
    race.spawn("A", "resume", thread_id=thread_id, gates=["inside-effect"], extra=OWN)
    race.wait_at("A", "inside-effect")
    race.spawn("B", "resume", thread_id=thread_id, gates=[], extra=OWN)
    assert race.wait_exit("B") == 5
    race.release("A", "inside-effect")
    assert race.wait_exit("A") == 0

    assert _exit(race, "B")["status"] == "contended" and race.first("B", "attempt_error") is None
    assert {"analyze_enter", "model_invoke", "invoke_enter"} & set(race.names("B")) == set()
    assert len(_effects(race)) == 1 and _exit(race, "A")["status"] == "completed"


def test_r2_takeover_of_a_dead_owner_inside_the_effect_reports_action_in_flight(race):
    thread_id = race.start_paused(ownership=True)
    race.spawn("A", "resume", thread_id=thread_id, gates=["inside-effect"],
               extra=["--ownership", "--lease-ttl-ms", str(SHORT_LEASE_MS)])
    race.wait_at("A", "inside-effect")
    assert race.kill("A") < 0  # SIGKILL inside the effect: the action stays IN_FLIGHT
    time.sleep(SHORT_LEASE_MS / 1000 + 0.2)
    race.spawn("B", "resume", thread_id=thread_id, gates=[], extra=OWN)
    assert race.wait_exit("B") == 6

    b = _exit(race, "B")
    assert (b["status"], b["next"], b["error_type"]) == ("action_in_flight", ["act"], "ActionInFlight")
    assert race.first("B", "attempt_error") is None  # structured, not an unhandled exception
    assert race.first("B", "released")["ok"] is True
    assert [a["status"] for a in race.query("SELECT status FROM actions")] == ["IN_FLIGHT"]
    assert _finals(race) == [] and _state(race) == []


# ---- R3: the action and business state come from one analysis; a stale owner cannot act ----

def test_r3_protected_stale_owner_before_its_analysis_is_stored(race):
    """A inferred High inside analyze but nothing of it was stored: B re-analyzes (Medium) and acts on that."""
    thread_id = race.start_paused(ownership=True)
    race.spawn("A", "resume", thread_id=thread_id, risk_level="High", gates=["after-analyze"], extra=STALLED)
    race.wait_at("A", "after-analyze")
    race.spawn("B", "resume", thread_id=thread_id, risk_level="Medium", gates=[], extra=OWN)
    assert race.wait_exit("B") == 0
    race.release("A", "after-analyze")
    assert race.wait_exit("A") == 6

    assert _exit(race, "A")["status"] == "lease_lost"
    a_owner = race.first("A", "acquired")["owner"]
    assert race.query("SELECT outcome FROM fenced_log WHERE owner = ? AND outcome = 'stored' AND "
                      "task_path LIKE '%analyze'", a_owner) == []
    [effect] = _effects(race)
    assert (effect["attempt"], effect["risk_level"]) == ("B", "Medium")
    [action] = race.query("SELECT subject FROM actions")
    assert json.loads(action["subject"])["risk_level"] == "Medium" == _state(race)[0]["current_risk_level"]
    [marker] = _finals(race)
    assert marker["trace_id"] == _exit(race, "B")["trace_id"]


def test_r3_protected_stale_owner_at_admission_is_rejected_and_its_analysis_lineage_is_continued(race):
    """A's analysis (High) is checkpointed; A's claim is rejected; B acts on that same lineage."""
    thread_id = race.start_paused(ownership=True)
    race.spawn("A", "resume", thread_id=thread_id, risk_level="High", gates=["before-claim"], extra=STALLED)
    race.wait_at("A", "before-claim")
    race.spawn("B", "resume", thread_id=thread_id, risk_level="Medium", gates=[], extra=OWN)
    assert race.wait_exit("B") == 0
    race.release("A", "before-claim")
    assert race.wait_exit("A") == 6

    assert _exit(race, "A")["status"] == "lease_lost"
    assert [c["outcome"] for c in race.query("SELECT outcome FROM claim_attempts ORDER BY at")] == [
        "claimed", "lease_rejected"]
    assert "model_invoke" not in race.names("B")  # B reused A's checkpointed analysis
    [effect] = _effects(race)
    assert (effect["attempt"], effect["risk_level"]) == ("B", "High")
    [action] = race.query("SELECT subject FROM actions")
    assert json.loads(action["subject"])["risk_level"] == "High" == _state(race)[0]["current_risk_level"]


# ---- R4: stale owner cannot finalize a second time; the new owner recovers finalization ----

def test_r4_protected_single_finalization_with_recovery_by_the_new_owner(race):
    thread_id = race.start_paused(ownership=True)
    race.spawn("A", "resume", thread_id=thread_id, gates=["before-finalize"], extra=STALLED)
    race.wait_at("A", "before-finalize")  # the graph reached END and acted; nothing finalized yet
    race.spawn("B", "resume", thread_id=thread_id, gates=[], extra=OWN)
    assert race.wait_exit("B") == 0
    race.release("A", "before-finalize")
    assert race.wait_exit("A") == 6

    b = _exit(race, "B")
    assert (b["status"], b["recovered_finalization"]) == ("completed", True)
    assert {"analyze_enter", "model_invoke", "effect_executed"} & set(race.names("B")) == set()
    assert _exit(race, "A")["status"] == "lease_lost"
    [marker] = _finals(race)
    assert marker["trace_id"] == b["trace_id"] and marker["recovered"] is True
    assert len(_state(race)) == 1 and len(_effects(race)) == 1
    assert [t["workflow_event"] for t in _completion_traces(race, thread_id)] == ["recovered_completed"]


# ---- Stale owner: action completion after ownership loss ----

def test_stale_owner_records_its_admitted_effect_but_cannot_advance_or_finalize(race):
    thread_id = race.start_paused(ownership=True)
    race.spawn("A", "resume", thread_id=thread_id, gates=["inside-effect"], extra=STALLED)
    race.wait_at("A", "inside-effect")  # claimed and effect executed; completion not recorded yet
    race.spawn("B", "resume", thread_id=thread_id, gates=[], extra=OWN)
    assert race.wait_exit("B") == 6  # takes over, finds the action IN_FLIGHT
    race.release("A", "inside-effect")
    assert race.wait_exit("A") == 6

    assert _exit(race, "B")["status"] == "action_in_flight"
    assert _exit(race, "A")["status"] == "lease_lost"
    assert [a["status"] for a in race.query("SELECT status FROM actions")] == ["COMPLETED"]  # recorded
    assert _finals(race) == [] and _state(race) == []  # but A could not finalize
    a_owner = race.first("A", "acquired")["owner"]
    assert race.query("SELECT kind FROM fenced_log WHERE owner = ? AND outcome = 'stored' AND task_path LIKE "
                      "'%act'", a_owner) == []  # nor advance the graph past act

    race.spawn("C", "resume", thread_id=thread_id, gates=[], extra=OWN)
    assert race.wait_exit("C") == 0
    c = _exit(race, "C")
    assert (c["status"], c["action_status"]) == ("completed", "COMPLETED")
    assert len(_effects(race)) == 1  # replayed, not executed again
    [marker] = _finals(race)
    assert marker["trace_id"] == c["trace_id"]


# ---- Crash and takeover ----

def test_k3_kill_after_end_before_finalization_is_finalized_by_the_next_owner(race):
    thread_id = race.start_paused(ownership=True)
    race.spawn("A", "resume", thread_id=thread_id, gates=["before-finalize"],
               extra=["--ownership", "--lease-ttl-ms", str(SHORT_LEASE_MS)])
    race.wait_at("A", "before-finalize")
    race.kill("A")
    time.sleep(SHORT_LEASE_MS / 1000 + 0.2)
    race.spawn("B", "resume", thread_id=thread_id, gates=[], extra=OWN)
    assert race.wait_exit("B") == 0

    b = _exit(race, "B")
    assert (b["status"], b["recovered_finalization"], b["change_type"]) == ("completed", True, "new_alert")
    assert len(_effects(race)) == 1 and len(_state(race)) == 1 and len(_finals(race)) == 1


def test_k4_kill_after_finalization_before_the_trace_recovers_the_trace(race):
    thread_id = race.start_paused(ownership=True)
    race.spawn("A", "resume", thread_id=thread_id, gates=["before-trace"],
               extra=["--ownership", "--lease-ttl-ms", str(SHORT_LEASE_MS)])
    race.wait_at("A", "before-trace")
    a_trace = race.first("A", "trace_once") or {}
    assert a_trace == {}  # the completion trace was not written
    race.kill("A")
    [marker] = _finals(race)
    time.sleep(SHORT_LEASE_MS / 1000 + 0.2)
    race.spawn("B", "resume", thread_id=thread_id, gates=[], extra=OWN)
    assert race.wait_exit("B") == 0

    b = _exit(race, "B")
    assert (b["status"], b["trace_recovered"], b["trace_id"]) == ("already_completed", True, marker["trace_id"])
    [trace] = _completion_traces(race, thread_id)
    assert (trace["trace_id"], trace["lease_epoch"], trace["trace_recovered"]) == (
        marker["trace_id"], marker["epoch"], True)
    assert len(_state(race)) == 1  # no second business write


# ---- X1 under ownership: still unresolved (different threads) ----

def _x1_owned(race):
    for w in "AB":
        race.spawn(w, "start", gates=["after-assess"], extra=OWN)
    for w in "AB":
        race.wait_at(w, "after-assess")
    for w in "AB":
        race.release(w, "after-assess")
    return [race.wait_exit(w) for w in "AB"]


def test_x1_under_ownership_still_opens_two_tickets(race):
    assert _x1_owned(race) == [0, 0]
    assert len(_effects(race)) == 2 and len(_finals(race)) == 2  # one per thread: ownership is per thread


@pytest.mark.xfail(strict=True, reason="Unresolved (outside same-thread ownership): one business change "
                                        "opens at most one ticket across threads")
def test_x1_requirement_one_ticket_per_business_change_under_ownership(race):
    _x1_owned(race)
    assert len(_effects(race)) == 1
