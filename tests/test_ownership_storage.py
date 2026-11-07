"""Fenced action ledger, once-only finalization and write-once traces (moto, in-process)."""
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from botocore.exceptions import ReadTimeoutError  # noqa: E402

from app.governance.action_ledger import ActionInFlight, ActionLedger  # noqa: E402
from app.governance.authority import AGENT_SCOPE, AuthorityDenied, AuthorityStore  # noqa: E402
from app.storage import risk_trace_store  # noqa: E402
from app.storage.finalization import AlreadyFinalized, FinalizationStore  # noqa: E402
from app.storage.thread_lease import Lease, LeaseLost, LeaseStore, OwnershipRequired  # noqa: E402
from tests.test_risk_workflow import (  # noqa: E402,F401  (env is a fixture)
    ACTIONS,
    AUTHORITY,
    CHECKPOINTS,
    MOTO_AVAILABLE,
    REGION,
    SUPPLIER_STATE,
    TRACES,
    env,
)

pytestmark = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")
THREAD = "thread-" + "f" * 32
ACTION = f"action#{THREAD}#open_review_ticket"


class Clock:
    def __init__(self):
        self.ms = 1_000_000

    def __call__(self):
        return self.ms


@pytest.fixture
def owned(env):
    clock = Clock()
    leases = LeaseStore(CHECKPOINTS, REGION, ttl_ms=10_000, now_ms=clock)
    ledger = ActionLedger(ACTIONS, AuthorityStore(AUTHORITY, REGION), REGION, lease_table=CHECKPOINTS)
    finals = FinalizationStore(CHECKPOINTS, SUPPLIER_STATE, REGION, sleep=lambda s: None)
    return env, clock, leases, ledger, finals


def _context(lease, trace_id="t-1"):
    return {"action_type": "open_review_ticket", "thread_id": THREAD, "trace_id": trace_id, "executor_id": "x",
            "subject": {"supplier": "TSMC"},
            "lease": {"thread_id": lease.thread_id, "owner": lease.owner, "epoch": lease.epoch}}


def _takeover(clock, leases, owner="owner-B"):
    clock.ms += leases.ttl_ms + 1
    return leases.acquire(THREAD, owner)


def _effect(calls):
    def effect():
        calls.append(1)
        return {"ticket_id": f"MOCK-{len(calls)}"}
    return effect


# ---- Fenced action ledger ----

def test_fenced_ledger_refuses_admission_without_a_lease(owned):
    env, clock, leases, ledger, _ = owned
    context = _context(leases.acquire(THREAD, "owner-A"))
    del context["lease"]
    with pytest.raises(OwnershipRequired):
        ledger.execute(ACTION, scope=AGENT_SCOPE, context=context, effect=lambda: {})
    assert env.actions.scan()["Items"] == []


def test_owner_admits_completes_and_replays(owned):
    _, _, leases, ledger, _ = owned
    a, calls = leases.acquire(THREAD, "owner-A"), []
    first = ledger.execute(ACTION, scope=AGENT_SCOPE, context=_context(a), effect=_effect(calls))
    again = ledger.execute(ACTION, scope=AGENT_SCOPE, context=_context(a, "t-2"), effect=_effect(calls))
    assert (first["status"], first["replayed"], again["replayed"], len(calls)) == ("COMPLETED", False, True, 1)
    item = ledger.get(ACTION)
    assert (int(item["claimed_epoch"]), item["last_replayed_by"], int(item["replay_epoch"])) == (1, "t-2", 1)


def test_stale_owner_cannot_claim(owned):
    env, clock, leases, ledger, _ = owned
    a = leases.acquire(THREAD, "owner-A")
    _takeover(clock, leases)
    calls = []
    with pytest.raises(LeaseLost):
        ledger.execute(ACTION, scope=AGENT_SCOPE, context=_context(a), effect=_effect(calls))
    assert calls == [] and env.actions.scan()["Items"] == []


def test_stale_owner_cannot_replay_a_completed_action(owned):
    _, clock, leases, ledger, _ = owned
    a, calls = leases.acquire(THREAD, "owner-A"), []
    ledger.execute(ACTION, scope=AGENT_SCOPE, context=_context(a), effect=_effect(calls))
    _takeover(clock, leases)
    with pytest.raises(LeaseLost):
        ledger.execute(ACTION, scope=AGENT_SCOPE, context=_context(a, "t-stale"), effect=_effect(calls))
    assert "last_replayed_by" not in ledger.get(ACTION) and len(calls) == 1


def test_stale_owner_cannot_record_a_denial(owned):
    env, clock, leases, ledger, _ = owned
    a = leases.acquire(THREAD, "owner-A")
    env.authority.revoke(reason="incident", actor="pytest")
    _takeover(clock, leases)
    with pytest.raises(LeaseLost):
        ledger.execute(ACTION, scope=AGENT_SCOPE, context=_context(a), effect=lambda: {})
    assert env.actions.scan()["Items"] == []
    b = leases.read(THREAD)
    with pytest.raises(AuthorityDenied):  # the current owner's denial is recorded
        ledger.execute(ACTION, scope=AGENT_SCOPE,
                       context=_context(Lease(THREAD, b["owner"], b["epoch"], b["expires_at_ms"])), effect=lambda: {})
    assert ledger.get(ACTION)["status"] == "DENIED"


def test_completion_is_recorded_after_ownership_loss(owned):
    """An admitted effect may finish after takeover; recording it is not fenced."""
    _, clock, leases, ledger, _ = owned
    a, calls = leases.acquire(THREAD, "owner-A"), []

    def slow_effect():
        _takeover(clock, leases)  # ownership lost while the effect runs
        return _effect(calls)()

    result = ledger.execute(ACTION, scope=AGENT_SCOPE, context=_context(a), effect=slow_effect)
    assert result["status"] == "COMPLETED" and len(calls) == 1
    with pytest.raises(LeaseLost):  # but the stale worker can't use the ledger again
        ledger.execute(ACTION, scope=AGENT_SCOPE, context=_context(a, "t-2"), effect=_effect(calls))


def test_new_owner_meeting_in_flight_gets_action_in_flight(owned):
    _, clock, leases, ledger, _ = owned
    a = leases.acquire(THREAD, "owner-A")

    def dies_mid_effect():
        raise SystemExit("executor died inside the effect")

    with pytest.raises(SystemExit):
        ledger.execute(ACTION, scope=AGENT_SCOPE, context=_context(a), effect=dies_mid_effect)
    b = _takeover(clock, leases)
    with pytest.raises(ActionInFlight):
        ledger.execute(ACTION, scope=AGENT_SCOPE, context=_context(b, "t-b"), effect=lambda: {})


def test_claim_with_a_lost_acknowledgment_is_recognized(owned):
    _, _, leases, ledger, _ = owned
    a, calls = leases.acquire(THREAD, "owner-A"), []
    real = ledger._client.transact_write_items

    def applied_then_timeout(**kwargs):
        real(**kwargs)
        raise ReadTimeoutError(endpoint_url="https://dynamodb")

    ledger._client.transact_write_items = applied_then_timeout
    result = ledger.execute(ACTION, scope=AGENT_SCOPE, context=_context(a), effect=_effect(calls))
    assert result["status"] == "COMPLETED" and len(calls) == 1


# ---- Once-only finalization ----

ROW = ("TSMC", "earthquake", {"current_risk_level": "High", "last_headline": "h", "last_seen_at": "t"})


def _marker(trace_id="t-1"):
    return {"kind": "finalized", "trace_id": trace_id, "change_type": "new_alert", "workflow_event": "resumed_completed"}


def test_finalization_writes_marker_and_business_state_atomically_once(owned):
    env, clock, leases, _, finals = owned
    a = leases.acquire(THREAD, "owner-A")
    marker = finals.commit(a, _marker(), ROW)
    assert (marker["epoch"], finals.read(THREAD)["trace_id"]) == (1, "t-1")
    [row] = env.supplier_state.scan()["Items"]
    assert (row["pk"], row["current_risk_level"]) == ("TSMC#earthquake", "High")
    leases.release(a)
    b = leases.acquire(THREAD, "owner-B")
    with pytest.raises(AlreadyFinalized) as already:
        finals.commit(b, _marker("t-2"), ("TSMC", "earthquake", {"current_risk_level": "Low"}))
    assert already.value.marker["trace_id"] == "t-1"
    assert env.supplier_state.scan()["Items"][0]["current_risk_level"] == "High"


def test_stale_owner_cannot_finalize(owned):
    env, clock, leases, _, finals = owned
    a = leases.acquire(THREAD, "owner-A")
    _takeover(clock, leases)
    with pytest.raises(LeaseLost):
        finals.commit(a, _marker(), ROW)
    assert finals.read(THREAD) is None and env.supplier_state.scan()["Items"] == []


def test_finalization_with_a_lost_acknowledgment_is_verified_by_the_marker(owned):
    env, _, leases, _, finals = owned
    a = leases.acquire(THREAD, "owner-A")
    real = finals._client.transact_write_items

    def applied_then_timeout(**kwargs):
        real(**kwargs)
        raise ReadTimeoutError(endpoint_url="https://dynamodb")

    finals._client.transact_write_items = applied_then_timeout
    finals.commit(a, _marker(), ROW)
    assert finals.read(THREAD)["trace_id"] == "t-1" and len(env.supplier_state.scan()["Items"]) == 1


def test_legacy_marker_without_business_state(owned):
    env, _, leases, _, finals = owned
    finals.commit(leases.acquire(THREAD, "owner-A"), {"kind": "legacy_recognized", "trace_id": "old"})
    assert finals.read(THREAD)["kind"] == "legacy_recognized" and env.supplier_state.scan()["Items"] == []


# ---- Write-once traces ----

@pytest.mark.parametrize("backend", ["dynamodb", "jsonl"])
def test_trace_append_once_is_idempotent_and_detects_conflicts(env, tmp_path, backend):
    store = (risk_trace_store.DynamoTraceStore(TRACES, REGION) if backend == "dynamodb"
             else risk_trace_store.JsonlTraceStore(tmp_path / "t.jsonl"))
    record = {"trace_id": "t-1", "alert_id": "a", "created_at": "2026-01-01T00:00:00+00:00", "headline": "h",
              "thread_id": THREAD, "workflow_event": "resumed_completed", "change_type": "new_alert",
              "lease_epoch": 2, "trace_steps": []}
    assert store.append_once(record) == risk_trace_store.WRITTEN
    assert store.append_once(dict(record)) == risk_trace_store.SAME
    assert store.append_once({**record, "change_type": "suppressed"}) == risk_trace_store.CONFLICT
    assert store.get_by_identifier("t-1")["change_type"] == "new_alert"
