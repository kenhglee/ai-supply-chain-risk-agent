"""Same-thread ownership across separate processes (persistent SQLite stand-ins).

Each worker is a separate process (tests/race_harness.py --ownership) running
the integrated owned lifecycle (risk_workflow.start/resume_workflow) against one
SQLite database. The stand-ins apply the same conditions as the DynamoDB
implementations: lease, fenced checkpoint and pending writes, fenced action
gate, once-only finalization and write-once traces. Retry and read-back are
tested against DynamoDB (moto) in tests/test_thread_lease.py and
tests/test_ownership_storage.py.
"""
import pytest

from tests.test_concurrent_resume import Race

OWN = ["--ownership"]
# A stalled owner: its lease expires at once and no heartbeat renews it, so the
# next worker takes over.
EXPIRES_AT_ONCE = ["--ownership", "--lease-ttl-ms", "1", "--no-heartbeat"]


@pytest.fixture
def race(tmp_path):
    race = Race(tmp_path)
    yield race
    race.cleanup()


def _lease(race, thread_id):
    [row] = race.query("SELECT epoch, state, owner FROM lease WHERE thread_id = ?", thread_id)
    return row


def _fenced(race, owner, outcome):
    return race.query("SELECT kind, task_path FROM fenced_log WHERE owner = ? AND outcome = ?", owner, outcome)


def test_two_simultaneous_acquisitions_yield_one_owner_and_the_contender_does_no_inference(race):
    thread_id = race.start_paused()
    for w in "AB":
        race.spawn(w, "resume", thread_id=thread_id, gates=["before-acquire"], extra=OWN)
    for w in "AB":
        race.wait_at(w, "before-acquire")
    for w in "AB":
        race.release(w, "before-acquire")
    codes = {w: race.wait_exit(w) for w in "AB"}

    acquired = [w for w in "AB" if race.first(w, "acquired")]
    contended = [w for w in "AB" if race.first(w, "contended")]
    assert len(acquired) == 1 and len(contended) == 1, (acquired, contended)
    owner, loser = acquired[0], contended[0]
    assert codes == {owner: 0, loser: 5}
    assert race.first(owner, "attempt_exit")["status"] == "completed"
    # The contender executed nothing.
    assert {"analyze_enter", "model_invoke", "invoke_enter"} & set(race.names(loser)) == set()
    assert race.names(owner).count("model_invoke") == 1
    holder = race.first(loser, "contended")["holder"]
    assert holder == race.first(owner, "acquired")["owner"]
    assert _fenced(race, holder, "rejected") == []


def test_takeover_rejects_the_stale_owners_pending_and_checkpoint_writes(race):
    thread_id = race.start_paused()
    race.spawn("A", "resume", thread_id=thread_id, gates=["after-analyze"], extra=EXPIRES_AT_ONCE)
    race.wait_at("A", "after-analyze")  # A has inferred; nothing from analyze is stored yet
    race.spawn("B", "resume", thread_id=thread_id, gates=[], extra=OWN)
    assert race.wait_exit("B") == 0
    race.release("A", "after-analyze")
    assert race.wait_exit("A") == 6

    a, b = race.first("A", "acquired"), race.first("B", "acquired")
    assert (a["epoch"], b["epoch"]) == (1, 2)
    assert race.first("A", "attempt_exit")["status"] == "lease_lost"
    assert race.first("A", "finalize_enter") is None and race.first("A", "released") is None
    # The pending writes were rejected by the lease check. LangGraph sends the next
    # checkpoint from another writer thread, so it is either rejected by the lease
    # check too or refused locally once the loss is recorded; never stored.
    rejected = [r["kind"] for r in _fenced(race, a["owner"], "rejected")]
    assert "writes" in rejected and set(rejected) <= {"writes", "checkpoint"}
    assert _fenced(race, a["owner"], "stored") == []  # nothing of A's reached storage
    assert race.names("B").count("model_invoke") == 1  # B had to run analyze itself
    analyze_writers = race.query("SELECT DISTINCT writer_trace_id FROM writes WHERE task_path LIKE '%analyze'")
    assert [r["writer_trace_id"] for r in analyze_writers] == [race.first("B", "attempt_exit")["trace_id"]]
    assert _lease(race, thread_id) == {"epoch": 2, "state": "RELEASED", "owner": b["owner"]}


def test_takeover_rejects_a_stale_special_error_write_even_when_its_exception_is_swallowed(race):
    thread_id = race.start_paused()
    race.spawn("A", "resume", thread_id=thread_id, gates=["after-analyze"],
               extra=EXPIRES_AT_ONCE + ["--fail-analyze"])
    race.wait_at("A", "after-analyze")
    race.spawn("B", "resume", thread_id=thread_id, gates=[], extra=OWN)
    assert race.wait_exit("B") == 0
    race.release("A", "after-analyze")
    assert race.wait_exit("A") == 6

    exit_event = race.first("A", "attempt_exit")
    # LangGraph re-raised the node's RuntimeError and swallowed the background
    # writer's LeaseLost; the fenced saver recorded the loss, so the lifecycle
    # reports lease_lost (not the RuntimeError, and not a success).
    assert (exit_event["status"], exit_event["error_type"]) == ("lease_lost", "RuntimeError")
    assert exit_event["ownership_lost"] == [[thread_id, race.first("A", "acquired")["owner"], 1]]
    assert [r["kind"] for r in _fenced(race, race.first("A", "acquired")["owner"], "rejected")] == ["writes"]
    assert race.query("SELECT * FROM writes WHERE channel = '__error__'") == []


def test_writes_stored_before_takeover_are_reused_without_repeating_inference(race):
    thread_id = race.start_paused()
    race.spawn("A", "resume", thread_id=thread_id, gates=["after-analyze-writes"], extra=EXPIRES_AT_ONCE)
    race.wait_at("A", "after-analyze-writes")  # analyze's writes stored under epoch 1; no new checkpoint
    a_owner = race.first("A", "acquired")["owner"]
    assert [r["task_path"].split(", ")[-1] for r in _fenced(race, a_owner, "stored")] == ["analyze"]
    race.spawn("B", "resume", thread_id=thread_id, gates=[], extra=OWN)
    assert race.wait_exit("B") == 0
    race.release("A", "after-analyze-writes")
    assert race.wait_exit("A") == 6

    assert {"analyze_enter", "model_invoke"} & set(race.names("B")) == set()  # reused A's stored analysis
    assert race.first("B", "attempt_exit")["status"] == "completed"
    assert [r["kind"] for r in _fenced(race, a_owner, "rejected")] == ["checkpoint"]
    assert race.names("A").count("model_invoke") == 1


def test_release_and_reacquisition_never_reset_the_epoch(race):
    thread_id = race.start_paused()
    epochs, statuses = [], []
    for w in ("W1", "W2", "W3"):
        race.spawn(w, "resume", thread_id=thread_id, gates=[], extra=OWN)
        assert race.wait_exit(w) == 0
        epochs.append(race.first(w, "acquired")["epoch"])
        statuses.append(race.first(w, "attempt_exit")["status"])
        assert race.first(w, "released")["ok"] is True
    assert epochs == [1, 2, 3]
    assert statuses == ["completed", "already_completed", "already_completed"]
    lease = _lease(race, thread_id)
    assert (lease["epoch"], lease["state"]) == (3, "RELEASED")
