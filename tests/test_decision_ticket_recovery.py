"""Failure injection for the MCP decision-ticket action against ActionLedger's
real retry and recovery semantics (moto).

Each case breaks one boundary of ActionLedger.execute (claim -> effect ->
completion), where the effect is create_servicenow_ticket followed by
save_risk_decision, then retries the request the way an MCP client would
(a new call, so a new trace_id). Invariant under test: at most one ticket is
created and at most one ticket record is written per decision, whatever
fails and however often the caller retries.
"""
import os
import sys

import pytest
from botocore.exceptions import ClientError, ConnectionError as BotoConnectionError, HTTPClientError

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
from app.governance.action_ledger import ActionLedger  # noqa: E402
from app.governance.authority import AuthorityStore  # noqa: E402
from app.storage import risk_state_store  # noqa: E402
from app.storage.risk_state_store import DynamoDecisionStore, JsonlDecisionStore  # noqa: E402
from app.workflows import decision_ticket_action as tickets  # noqa: E402
from tests.test_decision_ticket_action import PUSH_MAIN, _evaluated  # noqa: E402
from tests.test_risk_workflow import ACTIONS, AUTHORITY, MOTO_AVAILABLE, REGION, env  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")


class Injected(RuntimeError):
    """The injected crash."""


@pytest.fixture
def store(env, tmp_path):
    decisions = JsonlDecisionStore(tmp_path / "risk_decisions.jsonl")
    env.monkeypatch.setattr(risk_state_store, "_store", decisions)
    return decisions


@pytest.fixture
def ledger(env):
    return ActionLedger(ACTIONS, AuthorityStore(AUTHORITY, REGION), REGION)


@pytest.fixture
def created(env):
    """Tickets the mock ServiceNow actually issued."""
    issued = []
    real = tickets.create_servicenow_ticket

    def counting(normalized, decision):
        ticket = real(normalized, decision)
        issued.append(ticket["ticket_id"])
        return ticket

    env.monkeypatch.setattr(tickets, "create_servicenow_ticket", counting)
    return issued


def _ticket_records(store):
    return [r for r in store._load_all() if r.get("ticket")]


def _status(env, decision_id):
    item = env.actions.get_item(Key={"action_id": tickets.decision_ticket_action_id(decision_id)}).get("Item")
    return item and item["status"]


def _retry(decision_id, ledger, times=3):
    return [tickets.open_ticket_for_decision(decision_id, gate=ledger) for _ in range(times)]


# ---- Crash inside the effect, or between effect and completion: IN_FLIGHT, never re-run ----

def test_crash_after_ticket_creation_before_save(env, store, ledger, created):
    decision_id = _evaluated(PUSH_MAIN)

    def failing_save(*a, **k):
        raise Injected("decision store unavailable")

    env.monkeypatch.setattr(tickets, "save_risk_decision", failing_save)
    with pytest.raises(Injected):
        tickets.open_ticket_for_decision(decision_id, gate=ledger)
    env.monkeypatch.setattr(tickets, "save_risk_decision", risk_state_store.save_risk_decision)

    retries = _retry(decision_id, ledger)
    assert [r["reason"] for r in retries] == ["action_in_flight"] * 3
    assert len(created) == 1                # the one ticket issued before the crash
    assert _ticket_records(store) == []     # its record was never written
    assert _status(env, decision_id) == "IN_FLIGHT"


def test_crash_after_save_before_completion(env, store, ledger, created):
    decision_id = _evaluated(PUSH_MAIN)
    real_complete = ledger._complete

    def crash_before_complete(*a, **k):
        raise Injected("executor died before recording completion")

    ledger._complete = crash_before_complete
    with pytest.raises(Injected):
        tickets.open_ticket_for_decision(decision_id, gate=ledger)
    ledger._complete = real_complete

    retries = _retry(decision_id, ledger)
    assert [r["reason"] for r in retries] == ["action_in_flight"] * 3
    assert len(created) == 1
    assert [r["ticket"]["ticket_id"] for r in _ticket_records(store)] == created
    assert _status(env, decision_id) == "IN_FLIGHT"


# ---- Completion applied but unacknowledged: COMPLETED, retries replay ----

@pytest.mark.parametrize("ack_failure", ["connection_lost", "sdk_retry_hits_condition"])
def test_completion_applied_but_unacknowledged(env, store, ledger, created, ack_failure):
    decision_id = _evaluated(PUSH_MAIN)
    real_complete = ledger._complete

    def lost_ack(action_id, trace_id, effect):
        real_complete(action_id, trace_id, effect)
        if ack_failure == "connection_lost":
            raise BotoConnectionError(error="response lost")
        return real_complete(action_id, trace_id, effect)  # an SDK retry of the applied update

    ledger._complete = lost_ack
    with pytest.raises((BotoConnectionError, ClientError)) as raised:
        tickets.open_ticket_for_decision(decision_id, gate=ledger)
    if ack_failure == "sdk_retry_hits_condition":
        assert raised.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    ledger._complete = real_complete

    retries = _retry(decision_id, ledger)
    assert [r["status"] for r in retries] == ["already_created"] * 3
    assert {r["ticket"]["ticket_id"] for r in retries} == set(created)
    assert len(created) == 1 and len(_ticket_records(store)) == 1
    assert _status(env, decision_id) == "COMPLETED"


# ---- Claim with unknown outcome: the ledger's own recovery ----

@pytest.mark.parametrize("applied", [True, False])
def test_claim_with_unknown_outcome(env, store, ledger, created, applied):
    """ActionLedger._admit re-reads the item: admitted only if it names this attempt."""
    decision_id = _evaluated(PUSH_MAIN)
    real_transact = ledger._client.transact_write_items
    calls = []

    def unknown_outcome(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            if applied:
                real_transact(**kwargs)
            raise HTTPClientError(error="socket closed")
        return real_transact(**kwargs)

    env.monkeypatch.setattr(ledger._client, "transact_write_items", unknown_outcome)
    if applied:
        first = tickets.open_ticket_for_decision(decision_id, gate=ledger)
        assert first["status"] == "created"   # recovered in-call: the claim named this attempt
    else:
        with pytest.raises(HTTPClientError):  # nothing claimed: the caller sees the failure
            tickets.open_ticket_for_decision(decision_id, gate=ledger)
        assert _status(env, decision_id) is None
        assert created == []
    retries = _retry(decision_id, ledger)

    assert len(created) == 1 and len(_ticket_records(store)) == 1
    assert retries[0]["status"] == ("already_created" if applied else "created")
    assert {r["status"] for r in retries[1:]} == {"already_created"}


# ---- DynamoDB decision store: put applied, acknowledgment lost ----

def test_dynamo_decision_store_put_applied_but_unacknowledged(env, ledger, created):
    import boto3
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName="risk_decisions", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}])
    decisions = DynamoDecisionStore("risk_decisions", REGION)
    env.monkeypatch.setattr(risk_state_store, "_store", decisions)
    decision_id = _evaluated(PUSH_MAIN)

    real_put = decisions._table.put_item

    def put_then_lose_ack(**kwargs):
        real_put(**kwargs)
        raise BotoConnectionError(error="response lost")

    env.monkeypatch.setattr(decisions._table, "put_item", put_then_lose_ack)
    with pytest.raises(BotoConnectionError):
        tickets.open_ticket_for_decision(decision_id, gate=ledger)
    env.monkeypatch.setattr(decisions._table, "put_item", real_put)

    retries = _retry(decision_id, ledger)
    assert [r["reason"] for r in retries] == ["action_in_flight"] * 3
    ticket_records = [r for r in decisions._scan_all() if r.get("ticket")]
    assert len(created) == 1 and len(ticket_records) == 1
