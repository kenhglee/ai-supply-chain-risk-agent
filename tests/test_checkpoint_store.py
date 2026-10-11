"""Contract tests for DynamoCheckpointSaver against a moto DynamoDB table."""
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.base.id import uuid6
from langgraph.checkpoint.serde.types import ERROR

from app.storage.checkpoint_store import MAX_ITEM_BYTES, DynamoCheckpointSaver

try:
    from moto import mock_aws
    MOTO_AVAILABLE = True
except ImportError:
    MOTO_AVAILABLE = False

pytestmark = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")

TABLE = "risk_agent_checkpoints"
REGION = "us-east-1"


@pytest.fixture
def saver(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        import boto3
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "thread_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "thread_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
        )
        yield DynamoCheckpointSaver(TABLE, REGION)


def _cfg(thread_id: str, ns: str = "", checkpoint_id: str | None = None) -> dict:
    conf = {"thread_id": thread_id, "checkpoint_ns": ns}
    if checkpoint_id:
        conf["checkpoint_id"] = checkpoint_id
    return {"configurable": conf}


def _checkpoint(value) -> dict:
    c = empty_checkpoint()
    c["id"] = str(uuid6())
    c["channel_values"] = {"state": value}
    c["channel_versions"] = {"state": 1}
    return c


def _put_chain(saver, thread_id: str, n: int, ns: str = "") -> list[dict]:
    """Put n checkpoints, each parented on the previous; returns the returned configs."""
    configs, parent = [], _cfg(thread_id, ns)
    for step in range(n):
        parent = saver.put(parent, _checkpoint({"step": step}), {"source": "loop", "step": step}, {"state": 1})
        configs.append(parent)
    return configs


# ---- put / get_tuple ----

def test_round_trip(saver):
    checkpoint = _checkpoint({"headline": "TSMC quake", "trace_steps": [{"node_name": "infer"}]})
    returned = saver.put(_cfg("t1"), checkpoint, {"source": "input", "step": -1}, {"state": 1})

    assert returned == _cfg("t1", checkpoint_id=checkpoint["id"])
    tup = saver.get_tuple({"configurable": {"thread_id": "t1"}})
    assert tup.config == returned
    assert tup.checkpoint["id"] == checkpoint["id"]
    assert tup.checkpoint["channel_values"] == checkpoint["channel_values"]
    assert tup.checkpoint["channel_versions"] == {"state": 1}
    assert tup.metadata["source"] == "input"
    assert tup.metadata["step"] == -1
    assert tup.parent_config is None
    assert tup.pending_writes == []


def test_unknown_thread_returns_none(saver):
    assert saver.get_tuple({"configurable": {"thread_id": "nope"}}) is None


def test_latest_checkpoint_selected_with_parent(saver):
    configs = _put_chain(saver, "t1", 3)
    tup = saver.get_tuple({"configurable": {"thread_id": "t1"}})
    assert tup.config == configs[2]
    assert tup.checkpoint["channel_values"] == {"state": {"step": 2}}
    assert tup.parent_config == configs[1]


def test_get_tuple_by_explicit_checkpoint_id(saver):
    configs = _put_chain(saver, "t1", 3)
    tup = saver.get_tuple(configs[0])
    assert tup.checkpoint["channel_values"] == {"state": {"step": 0}}
    assert saver.get_tuple(_cfg("t1", checkpoint_id="missing")) is None


def test_fresh_saver_instance_reads_existing_checkpoints(saver):
    configs = _put_chain(saver, "t1", 2)
    other = DynamoCheckpointSaver(TABLE, REGION)
    assert other.get_tuple({"configurable": {"thread_id": "t1"}}).config == configs[1]


# ---- put_writes ----

def test_pending_writes_returned_for_checkpoint(saver):
    configs = _put_chain(saver, "t1", 2)
    saver.put_writes(configs[1], [("a", 1), ("b", {"x": 2})], task_id="task-1", task_path="p")
    saver.put_writes(configs[0], [("a", "older")], task_id="task-0")

    assert saver.get_tuple(configs[1]).pending_writes == [("task-1", "a", 1), ("task-1", "b", {"x": 2})]
    assert saver.get_tuple(configs[0]).pending_writes == [("task-0", "a", "older")]


def test_regular_writes_first_wins_special_writes_overwrite(saver):
    [config] = _put_chain(saver, "t1", 1)
    saver.put_writes(config, [("a", 1)], task_id="task-1")
    saver.put_writes(config, [("a", 99)], task_id="task-1")
    saver.put_writes(config, [(ERROR, "first")], task_id="task-1")
    saver.put_writes(config, [(ERROR, "second")], task_id="task-1")

    writes = saver.get_tuple(config).pending_writes
    assert ("task-1", "a", 1) in writes
    assert ("task-1", "a", 99) not in writes
    assert ("task-1", ERROR, "second") in writes
    assert len(writes) == 2


# ---- isolation ----

def test_namespace_isolation(saver):
    root = _put_chain(saver, "t1", 1, ns="")
    sub = _put_chain(saver, "t1", 1, ns="sub")
    assert saver.get_tuple(_cfg("t1", "")).config == root[0]
    assert saver.get_tuple(_cfg("t1", "sub")).config == sub[0]


def test_namespace_with_separator_rejected(saver):
    with pytest.raises(ValueError):
        saver.get_tuple(_cfg("t1", "bad#ns"))


def test_thread_isolation(saver):
    a = _put_chain(saver, "thread-A", 2)
    b = _put_chain(saver, "thread-B", 1)
    assert saver.get_tuple({"configurable": {"thread_id": "thread-A"}}).config == a[1]
    assert saver.get_tuple({"configurable": {"thread_id": "thread-B"}}).config == b[0]


# ---- list ----

def test_list_descending_limit_before_filter(saver):
    configs = _put_chain(saver, "t1", 4)
    ids = [c["configurable"]["checkpoint_id"] for c in configs]
    listed = lambda **kw: [t.config["configurable"]["checkpoint_id"] for t in saver.list(_cfg("t1"), **kw)]

    assert listed() == ids[::-1]
    assert listed(limit=2) == [ids[3], ids[2]]
    assert listed(before=configs[2]) == [ids[1], ids[0]]
    assert listed(filter={"step": 1}) == [ids[1]]
    assert [t.config for t in saver.list(configs[0])] == [configs[0]]


def test_list_paginates(saver, monkeypatch):
    configs = _put_chain(saver, "t1", 5)
    real_query = saver._table.query
    calls = []

    def one_item_query(**kwargs):
        calls.append(kwargs)
        return real_query(Limit=1, **kwargs)

    monkeypatch.setattr(saver._table, "query", one_item_query)
    listed = list(saver.list(_cfg("t1")))
    assert [t.config for t in listed] == configs[::-1]
    assert any("ExclusiveStartKey" in c for c in calls)


def test_list_without_thread_not_supported(saver):
    with pytest.raises(NotImplementedError):
        list(saver.list(None))


# ---- delete_thread / size ----

def test_delete_thread_removes_checkpoints_and_writes(saver):
    configs = _put_chain(saver, "t1", 2)
    saver.put_writes(configs[1], [("a", 1)], task_id="task-1")
    keep = _put_chain(saver, "t2", 1)

    saver.delete_thread("t1")
    saver.delete_thread("t1")  # idempotent

    assert saver.get_tuple({"configurable": {"thread_id": "t1"}}) is None
    assert saver._table.query(
        KeyConditionExpression="thread_id = :t", ExpressionAttributeValues={":t": "t1"}
    )["Items"] == []
    assert saver.get_tuple({"configurable": {"thread_id": "t2"}}).config == keep[0]


def test_oversize_checkpoint_rejected(saver):
    big = _checkpoint({"context": "x" * (MAX_ITEM_BYTES + 1)})
    with pytest.raises(ValueError, match="limit"):
        saver.put(_cfg("t1"), big, {"source": "loop", "step": 0}, {"state": 1})
    assert saver.get_tuple({"configurable": {"thread_id": "t1"}}) is None
