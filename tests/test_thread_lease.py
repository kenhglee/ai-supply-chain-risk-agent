"""Lease and fenced DynamoCheckpointSaver against moto DynamoDB.

In-process tests. Process-level contention and takeover are in
tests/test_ownership_processes.py. moto does not implement ClientRequestToken
idempotency, so every lost-acknowledgment retry here goes through read-back
verification (real DynamoDB may instead acknowledge the retry directly).
"""
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

try:
    from moto import mock_aws
    MOTO_AVAILABLE = True
except ImportError:
    MOTO_AVAILABLE = False

pytestmark = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")

from botocore.exceptions import ClientError, ReadTimeoutError  # noqa: E402
from langgraph.checkpoint.base import empty_checkpoint  # noqa: E402

from app.storage.checkpoint_store import DynamoCheckpointSaver  # noqa: E402
from app.storage.thread_lease import (  # noqa: E402
    HELD,
    LeaseContended,
    LeaseHeartbeat,
    LeaseLost,
    LeaseStore,
    OwnershipRequired,
    WriteConflict,
    WriteOutcomeUnknown,
)

REGION = "us-east-1"
TABLE = "risk_agent_checkpoints"
THREAD = "thread-" + "a" * 32


class Clock:
    def __init__(self, start_ms=1_000_000):
        self.ms = start_ms

    def __call__(self):
        return self.ms


@pytest.fixture
def table(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        import boto3
        resource = boto3.resource("dynamodb", region_name=REGION)
        resource.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "thread_id", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "thread_id", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}])
        yield resource.Table(TABLE)


def _leases(clock, ttl_ms=10_000):
    return LeaseStore(TABLE, REGION, ttl_ms=ttl_ms, now_ms=clock)


# moto's transact_write_items deep-copies its in-memory table for rollback and
# is not safe against a concurrent transaction mutating that table; LangGraph
# writes from several background threads. Real DynamoDB serializes conflicting
# transactions itself, so this lock exists only for moto (production has none).
_MOTO_TRANSACTION_LOCK = threading.Lock()


def _saver():
    saver = DynamoCheckpointSaver(TABLE, REGION, fenced=True, sleep=lambda s: None)
    real = saver._client.transact_write_items

    def serialized(**kwargs):
        with _MOTO_TRANSACTION_LOCK:
            return real(**kwargs)

    saver._client.transact_write_items = serialized
    return saver


def _config(lease, checkpoint_id=None):
    configurable = {"thread_id": lease.thread_id, "checkpoint_ns": "", "lease_owner": lease.owner,
                    "lease_epoch": lease.epoch}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _items(table, prefix):
    return [i for i in table.scan()["Items"] if i["sk"].startswith(prefix)]


def _put_checkpoint(saver, lease, parent=None):
    checkpoint = empty_checkpoint()
    config = _config(lease, parent)
    saver.put(config, checkpoint, {"source": "loop", "step": 1, "parents": {}}, {})
    return checkpoint["id"]


def _takeover(clock, leases, owner="owner-B"):
    clock.ms += leases.ttl_ms + 1
    return leases.acquire(THREAD, owner)


# ---- Lease lifecycle ----

def test_new_thread_starts_at_epoch_1_and_a_second_acquisition_is_contended(table):
    leases = _leases(Clock())
    lease = leases.acquire(THREAD, "owner-A", new_thread=True)
    assert lease.epoch == 1
    with pytest.raises(LeaseContended) as contended:
        leases.acquire(THREAD, "owner-B")
    assert (contended.value.holder["owner"], contended.value.holder["epoch"]) == ("owner-A", 1)
    with pytest.raises(LeaseContended):
        leases.acquire(THREAD, "owner-B", new_thread=True)


def test_release_keeps_the_item_and_reacquisition_never_resets_the_epoch(table):
    leases = _leases(Clock())
    epochs = []
    for owner in ("A", "B", "C"):
        lease = leases.acquire(THREAD, owner)
        epochs.append(lease.epoch)
        assert leases.release(lease) is True
        assert leases.read(THREAD)["state"] == "RELEASED"
    assert epochs == [1, 2, 3]
    stale = SimpleNamespace(thread_id=THREAD, owner="A", epoch=1)
    assert leases.release(stale) is False  # a stale owner cannot release, or reset anything
    assert leases.read(THREAD)["epoch"] == 3


def test_expired_lease_can_be_taken_over_and_the_stale_owner_cannot_renew_or_release(table):
    clock = Clock()
    leases = _leases(clock)
    a = leases.acquire(THREAD, "owner-A")
    b = _takeover(clock, leases)
    assert b.epoch == a.epoch + 1
    with pytest.raises(LeaseLost):
        leases.renew(a)
    assert leases.release(a) is False
    assert leases.read(THREAD)["owner"] == "owner-B"


def test_expired_owner_may_renew_while_nobody_has_taken_over(table):
    clock = Clock()
    leases = _leases(clock)
    a = leases.acquire(THREAD, "owner-A")
    clock.ms += leases.ttl_ms + 1  # expired, but no other acquisition
    renewed = leases.renew(a)
    assert renewed.epoch == a.epoch and renewed.expires_at_ms == clock.ms + leases.ttl_ms
    with pytest.raises(LeaseContended):
        leases.acquire(THREAD, "owner-B")  # live again after renewal


def test_acquisition_with_a_lost_acknowledgment_is_recognized(table):
    leases = _leases(Clock())
    real = leases._table.update_item

    def applied_then_timeout(**kwargs):
        real(**kwargs)
        raise ReadTimeoutError(endpoint_url="https://dynamodb")

    leases._table.update_item = applied_then_timeout
    lease = leases.acquire(THREAD, "owner-A")
    assert (lease.owner, lease.epoch) == ("owner-A", 1)


def test_heartbeat_renews_and_reports_takeover(table):
    clock = Clock()
    leases = _leases(clock, ttl_ms=1_000)
    beat = LeaseHeartbeat(leases, leases.acquire(THREAD, "owner-A"), interval_s=0.02).start()
    first_expiry = leases.read(THREAD)["expires_at_ms"]
    clock.ms += 500
    deadline = time.monotonic() + 5
    while leases.read(THREAD)["expires_at_ms"] == first_expiry and time.monotonic() < deadline:
        time.sleep(0.01)
    assert leases.read(THREAD)["expires_at_ms"] > first_expiry  # renewed
    beat.stop()
    _takeover(clock, leases)
    beat = LeaseHeartbeat(leases, beat.lease, interval_s=0.02).start()
    assert beat.lost.wait(5)
    beat.stop()
    assert isinstance(beat.error, LeaseLost)


def test_delete_thread_keeps_lease_and_final_items(table):
    leases = _leases(Clock())
    lease = leases.acquire(THREAD, "owner-A")
    table.put_item(Item={"thread_id": THREAD, "sk": "final", "trace_id": "t"})
    saver = _saver()
    _put_checkpoint(saver, lease)
    saver.delete_thread(THREAD)
    assert sorted(i["sk"] for i in table.scan()["Items"]) == ["final", "lease"]


# ---- Fenced writes ----

def test_fenced_saver_refuses_writes_without_an_ownership_token(table):
    saver = _saver()
    with pytest.raises(OwnershipRequired):
        saver.put({"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}}, empty_checkpoint(),
                  {"source": "loop", "step": 1, "parents": {}}, {})
    assert table.scan()["Items"] == []


def test_fenced_writes_store_owner_and_epoch_and_read_back(table):
    lease = _leases(Clock()).acquire(THREAD, "owner-A")
    saver = _saver()
    checkpoint_id = _put_checkpoint(saver, lease)
    saver.put_writes(_config(lease, checkpoint_id), [("alert", {"risk_level": "High"}), ("__error__", "boom")],
                     "task-1", "~__pregel_pull, analyze")
    tup = saver.get_tuple({"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}})
    assert tup.config["configurable"]["checkpoint_id"] == checkpoint_id
    assert sorted(w[1] for w in tup.pending_writes) == ["__error__", "alert"]
    [chk] = _items(table, "chk#")
    assert (chk["lease_owner"], int(chk["lease_epoch"])) == ("owner-A", 1)


def test_takeover_rejects_stale_checkpoint_pending_and_special_writes(table):
    clock = Clock()
    leases = _leases(clock)
    a = leases.acquire(THREAD, "owner-A")
    stale = _saver()
    checkpoint_id = _put_checkpoint(stale, a)
    _takeover(clock, leases)
    before = sorted(i["sk"] for i in table.scan()["Items"])

    with pytest.raises(LeaseLost):
        _put_checkpoint(stale, a, parent=checkpoint_id)
    calls = []
    real = stale._client.transact_write_items
    stale._client.transact_write_items = lambda **kw: calls.append(1) or real(**kw)
    with pytest.raises(LeaseLost):  # refused locally once loss is known
        stale.put_writes(_config(a, checkpoint_id), [("alert", {"risk_level": "Low"})], "task-1", "p, analyze")
    with pytest.raises(LeaseLost):
        stale.put_writes(_config(a, checkpoint_id), [("__error__", "stale error")], "task-2", "p, act")
    assert calls == []

    fresh = _saver()  # a separate stale process: rejected by the lease check itself
    with pytest.raises(LeaseLost):
        fresh.put_writes(_config(a, checkpoint_id), [("__error__", "stale error")], "task-2", "p, act")
    with pytest.raises(LeaseLost):
        fresh.put_writes(_config(a, checkpoint_id), [("alert", {"x": 1})], "task-1", "p, analyze")
    assert sorted(i["sk"] for i in table.scan()["Items"]) == before
    assert list(stale.lost) == [(THREAD, "owner-A", 1)]


def test_writes_stored_before_takeover_remain_readable_by_the_new_owner(table):
    clock = Clock()
    leases = _leases(clock)
    a = leases.acquire(THREAD, "owner-A")
    saver_a = _saver()
    checkpoint_id = _put_checkpoint(saver_a, a)
    saver_a.put_writes(_config(a, checkpoint_id), [("alert", {"risk_level": "High"})], "task-1", "p, analyze")
    b = _takeover(clock, leases)
    tup = _saver().get_tuple(_config(b))
    assert [(t, ch, v) for t, ch, v in tup.pending_writes] == [("task-1", "alert", {"risk_level": "High"})]


# ---- Retries and read-back after unknown outcomes ----

class Faults:
    """Wraps transact_write_items with a script: 'applied-timeout', 'timeout', or a ClientError."""

    def __init__(self, saver, script):
        self.real, self.script, self.calls = saver._client.transact_write_items, list(script), 0
        saver._client.transact_write_items = self

    def __call__(self, **kwargs):
        self.calls += 1
        step = self.script.pop(0) if self.script else None
        if step == "applied-timeout":
            self.real(**kwargs)
            raise ReadTimeoutError(endpoint_url="https://dynamodb")
        if step == "timeout":
            raise ReadTimeoutError(endpoint_url="https://dynamodb")
        if isinstance(step, ClientError):
            raise step
        return self.real(**kwargs)


def _cancelled(*reasons):
    return ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                        "CancellationReasons": [{"Code": r} for r in reasons]}, "TransactWriteItems")


def test_lost_acknowledgment_on_checkpoint_does_not_duplicate_or_corrupt(table):
    lease = _leases(Clock()).acquire(THREAD, "owner-A")
    saver = _saver()
    faults = Faults(saver, ["applied-timeout"])
    checkpoint_id = _put_checkpoint(saver, lease)
    assert faults.calls == 2  # retry -> data condition -> read-back -> identical: success
    [chk] = _items(table, "chk#")
    assert chk["checkpoint_id"] == checkpoint_id
    assert saver.get_tuple(_config(lease)).checkpoint["id"] == checkpoint_id


def test_lost_acknowledgment_on_pending_writes_verifies_the_complete_batch(table):
    lease = _leases(Clock()).acquire(THREAD, "owner-A")
    saver = _saver()
    checkpoint_id = _put_checkpoint(saver, lease)
    writes = [("alert", {"risk_level": "High"}), ("trace_steps", [{"node_name": "analyze"}]), ("__error__", "e")]
    Faults(saver, ["applied-timeout"])
    saver.put_writes(_config(lease, checkpoint_id), writes, "task-1", "p, analyze")
    stored = _items(table, "wr#")
    assert sorted(int(i["idx"]) for i in stored) == [-1, 0, 1]
    tup = saver.get_tuple(_config(lease))
    assert sorted((ch, json.dumps(v)) for _, ch, v in tup.pending_writes) == sorted(
        (ch, json.dumps(v)) for ch, v in writes)


def test_special_only_batch_retry_after_lost_acknowledgment(table):
    lease = _leases(Clock()).acquire(THREAD, "owner-A")
    saver = _saver()
    checkpoint_id = _put_checkpoint(saver, lease)
    faults = Faults(saver, ["applied-timeout"])
    saver.put_writes(_config(lease, checkpoint_id), [("__interrupt__", ["x"])], "task-1", "p, act")
    assert faults.calls == 2 and len(_items(table, "wr#")) == 1


def test_partial_or_different_pending_writes_are_a_data_conflict(table):
    lease = _leases(Clock()).acquire(THREAD, "owner-A")
    saver = _saver()
    checkpoint_id = _put_checkpoint(saver, lease)
    saver.put_writes(_config(lease, checkpoint_id), [("alert", {"risk_level": "High"})], "task-1", "p, analyze")
    with pytest.raises(WriteConflict, match="partial or differ"):  # same key, different batch
        saver.put_writes(_config(lease, checkpoint_id),
                         [("alert", {"risk_level": "Low"}), ("trace_steps", [])], "task-1", "p, analyze")
    assert len(_items(table, "wr#")) == 1


def test_checkpoint_with_different_content_is_a_data_conflict(table):
    lease = _leases(Clock()).acquire(THREAD, "owner-A")
    saver = _saver()
    checkpoint = empty_checkpoint()
    saver.put(_config(lease), checkpoint, {"source": "loop", "step": 1, "parents": {}}, {})
    with pytest.raises(WriteConflict, match="different content"):
        saver.put(_config(lease), checkpoint, {"source": "loop", "step": 2, "parents": {}}, {})


def test_transaction_conflicts_and_throttling_are_retried(table):
    lease = _leases(Clock()).acquire(THREAD, "owner-A")
    saver = _saver()
    throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "TransactWriteItems")
    faults = Faults(saver, [_cancelled("None", "TransactionConflict"), throttled])
    _put_checkpoint(saver, lease)
    assert faults.calls == 3 and len(_items(table, "chk#")) == 1


def test_unknown_outcome_never_applied_raises_unknown_while_still_owner(table):
    lease = _leases(Clock()).acquire(THREAD, "owner-A")
    saver = _saver()
    Faults(saver, ["timeout"] * 10)
    with pytest.raises(WriteOutcomeUnknown):
        _put_checkpoint(saver, lease)
    assert _items(table, "chk#") == []


def test_unknown_outcome_after_takeover_is_lost_ownership(table):
    clock = Clock()
    leases = _leases(clock)
    a = leases.acquire(THREAD, "owner-A")
    _takeover(clock, leases)
    saver = _saver()
    Faults(saver, ["timeout"] * 10)
    with pytest.raises(LeaseLost):
        _put_checkpoint(saver, a)


def test_lost_ownership_is_never_retried(table):
    clock = Clock()
    leases = _leases(clock)
    a = leases.acquire(THREAD, "owner-A")
    _takeover(clock, leases)
    saver = _saver()
    faults = Faults(saver, [])
    with pytest.raises(LeaseLost):
        _put_checkpoint(saver, a)
    assert faults.calls == 1


def test_request_errors_are_not_retried(table):
    lease = _leases(Clock()).acquire(THREAD, "owner-A")
    saver = _saver()
    invalid = ClientError({"Error": {"Code": "ValidationException", "Message": "bad"}}, "TransactWriteItems")
    faults = Faults(saver, [invalid])
    with pytest.raises(ClientError):
        _put_checkpoint(saver, lease)
    assert faults.calls == 1


# ---- With the compiled graph (in-process; lifecycle integration is not done yet) ----

def test_graph_reuses_writes_stored_before_takeover_without_repeating_inference(table, monkeypatch):
    from app.governance.action_ledger import UnconfiguredActionGate
    from app.storage import risk_trace_store
    from app.workflows import supplier_risk_agent as agent
    from tests.race_harness import RISK_ALERT

    calls = SimpleNamespace(risk=0)

    def risk(prompt):
        calls.risk += 1
        return json.dumps(RISK_ALERT)

    monkeypatch.setattr(agent, "_triage_llm", SimpleNamespace(invoke=lambda p: "retrieve"))
    monkeypatch.setattr(agent, "_risk_llm", SimpleNamespace(invoke=risk))
    monkeypatch.setattr(agent, "_get_retriever", lambda: SimpleNamespace(
        retriever_id="f", retriever_version="v", embedding_provider="f", top_k=1,
        retrieve=lambda q, s: SimpleNamespace(context="ctx")))
    monkeypatch.setattr(risk_trace_store, "_store", SimpleNamespace(append=lambda r: None))
    reader = SimpleNamespace(get=lambda *a: {"current_risk_level": "High"})  # suppressed: no act

    def app(saver):
        return agent.build_app(saver, action_gate=UnconfiguredActionGate(), risk_reader=reader)

    def run_config(lease, trace_id):
        return {"configurable": {"thread_id": THREAD, "trace_id": trace_id, "lease_owner": lease.owner,
                                 "lease_epoch": lease.epoch}}

    clock = Clock()
    leases = _leases(clock)
    start = leases.acquire(THREAD, "owner-S", new_thread=True)
    app(_saver()).invoke(agent.initial_risk_state("a", THREAD, "t0", RISK_ALERT["supplier"] + " earthquake"),
                         run_config(start, "t0"), interrupt_before=["analyze"], durability="sync")
    leases.release(start)

    # Owner A runs analyze; its writes are stored, then the next checkpoint put "crashes".
    a = leases.acquire(THREAD, "owner-A")
    saver_a, stored = _saver(), threading.Event()
    real_put_writes, real_put = saver_a.put_writes, saver_a.put

    def put_writes(config, writes, task_id, task_path=""):
        real_put_writes(config, writes, task_id, task_path)
        if task_path.endswith("analyze"):
            stored.set()

    def put(config, checkpoint, metadata, versions):
        if (checkpoint["channel_values"].get("trace_steps") or [{}])[-1].get("node_name") == "analyze":
            assert stored.wait(5)
            raise RuntimeError("process died before the checkpoint after analyze")
        return real_put(config, checkpoint, metadata, versions)

    saver_a.put_writes, saver_a.put = put_writes, put
    with pytest.raises(RuntimeError, match="process died"):
        app(saver_a).invoke(None, run_config(a, "tA"), durability="sync")
    assert calls.risk == 1

    b = _takeover(clock, leases)
    out = app(_saver()).invoke(None, run_config(b, "tB"), durability="sync")
    assert calls.risk == 1  # analyze's stored result reused, no second inference
    assert [s["node_name"] for s in out["trace_steps"]][-3:] == ["analyze", "validate", "assess"]
    assert {int(i["lease_epoch"]) for i in _items(table, "chk#")} == {1, 3}  # START, then B; A died first
    assert {int(i["lease_epoch"]) for i in _items(table, "wr#") if i["task_path"].endswith("analyze")} == {2}
    assert leases.read(THREAD)["state"] == HELD and leases.read(THREAD)["epoch"] == 3
