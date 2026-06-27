import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.storage.risk_trace_store import (
    JsonlTraceStore,
    DynamoTraceStore,
    build_trace_explanation,
    get_trace_store,
)


def _make_trace(
    trace_id: str = "trace1",
    alert_id: str = "alert1",
    created_at: str = "2024-01-01T00:00:00+00:00",
) -> dict:
    return {
        "trace_id": trace_id,
        "alert_id": alert_id,
        "created_at": created_at,
        "headline": "Test headline",
        "run_duration_ms": 123.4,
        "tool_decision": "analyze",
        "final_status": "new_alert",
        "supplier": "TSMC",
        "risk_type": "supply_disruption",
        "risk_level": "high",
        "change_type": "new_alert",
        "trace_steps": [{"node_name": "infer", "duration_ms": 50.0}],
        "retriever_metadata": None,
        "prompt_metadata": None,
        "model_metadata": None,
    }


# ---- JSONL ----

def test_jsonl_append_and_get_all(tmp_path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    t1 = _make_trace("t1", "a1", "2024-01-01T00:00:00+00:00")
    t2 = _make_trace("t2", "a2", "2024-01-02T00:00:00+00:00")
    store.append(t1)
    store.append(t2)
    records = store.get_all()
    assert len(records) == 2
    assert records[0]["trace_id"] == "t2"  # newest first
    assert records[1]["trace_id"] == "t1"
    assert records[0]["store_backend"] == "jsonl"


def test_jsonl_get_by_trace_id(tmp_path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    store.append(_make_trace("trace-abc", "alert-xyz"))
    result = store.get_by_identifier("trace-abc")
    assert result is not None
    assert result["trace_id"] == "trace-abc"


def test_jsonl_get_by_alert_id(tmp_path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    store.append(_make_trace("trace-abc", "alert-xyz"))
    result = store.get_by_identifier("alert-xyz")
    assert result is not None
    assert result["alert_id"] == "alert-xyz"


def test_jsonl_get_by_identifier_returns_none_for_unknown(tmp_path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    store.append(_make_trace())
    assert store.get_by_identifier("no-such-id") is None


def test_jsonl_empty_store_returns_empty_list(tmp_path):
    store = JsonlTraceStore(tmp_path / "traces.jsonl")
    assert store.get_all() == []
    assert store.get_by_identifier("anything") is None


def test_get_trace_store_factory_returns_jsonl(monkeypatch):
    monkeypatch.setenv("TRACE_STORE_BACKEND", "jsonl")
    store = get_trace_store()
    assert isinstance(store, JsonlTraceStore)


def test_get_trace_store_factory_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("TRACE_STORE_BACKEND", "pinecone")
    with pytest.raises(ValueError, match="Unsupported TRACE_STORE_BACKEND: 'pinecone'"):
        get_trace_store()


# ---- build_trace_explanation (pure function, no storage) ----

def test_build_trace_explanation_no_steps():
    record = _make_trace()
    record["trace_steps"] = []
    record["run_duration_ms"] = 5.0
    explanation = build_trace_explanation(record)
    assert "filtered before LangGraph" in explanation
    assert "TSMC" in explanation


def test_build_trace_explanation_with_steps():
    record = _make_trace()
    record["trace_steps"] = [
        {"node_name": "infer", "duration_ms": 10.0},
        {"node_name": "decide", "duration_ms": 100.0, "decision": "retrieve"},
        {"node_name": "analyze", "duration_ms": 200.0},
    ]
    explanation = build_trace_explanation(record)
    assert "infer → decide → analyze" in explanation
    assert "analyze" in explanation  # slowest node


# ---- DynamoDB (requires moto) ----

try:
    from moto import mock_aws
    MOTO_AVAILABLE = True
except ImportError:
    MOTO_AVAILABLE = False

moto_required = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")


@pytest.fixture
def aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _create_trace_table(region: str = "us-east-1") -> None:
    import boto3
    client = boto3.client("dynamodb", region_name=region)
    client.create_table(
        TableName="risk_traces",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "trace_id", "AttributeType": "S"},
            {"AttributeName": "alert_id", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "trace_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "alert-id-index",
                "KeySchema": [{"AttributeName": "alert_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }
        ],
    )


@moto_required
def test_dynamo_append_and_get_all(aws_credentials):
    with mock_aws():
        _create_trace_table()
        store = DynamoTraceStore("risk_traces", "us-east-1")
        t1 = _make_trace("t1", "a1", "2024-01-01T00:00:00+00:00")
        t2 = _make_trace("t2", "a2", "2024-01-02T00:00:00+00:00")
        store.append(t1)
        store.append(t2)
        records = store.get_all()
        assert len(records) == 2
        assert records[0]["trace_id"] == "t2"  # newest first
        assert records[0]["store_backend"] == "dynamodb"
        assert records[0]["run_duration_ms"] == pytest.approx(123.4)
        assert records[0]["trace_steps"] == t2["trace_steps"]


@moto_required
def test_dynamo_get_by_trace_id(aws_credentials):
    with mock_aws():
        _create_trace_table()
        store = DynamoTraceStore("risk_traces", "us-east-1")
        store.append(_make_trace("trace-abc", "alert-xyz"))
        result = store.get_by_identifier("trace-abc")
        assert result is not None
        assert result["trace_id"] == "trace-abc"
        assert result["supplier"] == "TSMC"


@moto_required
def test_dynamo_get_by_alert_id(aws_credentials):
    with mock_aws():
        _create_trace_table()
        store = DynamoTraceStore("risk_traces", "us-east-1")
        store.append(_make_trace("trace-abc", "alert-xyz"))
        result = store.get_by_identifier("alert-xyz")
        assert result is not None
        assert result["alert_id"] == "alert-xyz"
        assert result["trace_id"] == "trace-abc"


@moto_required
def test_dynamo_get_by_identifier_returns_none_for_unknown(aws_credentials):
    with mock_aws():
        _create_trace_table()
        store = DynamoTraceStore("risk_traces", "us-east-1")
        store.append(_make_trace())
        assert store.get_by_identifier("no-such-id") is None


@moto_required
def test_dynamo_early_return_trace_metadata_fields_absent_after_readback(aws_credentials):
    """Early-return traces omit prompt/model/retriever metadata.

    DynamoDB readback must not synthesize None for absent metadata fields —
    those fields should remain absent so Zod .optional() validation in the
    React frontend does not receive null and throw a ZodError.
    """
    with mock_aws():
        _create_trace_table()
        store = DynamoTraceStore("risk_traces", "us-east-1")
        early_return_trace = {
            "trace_id": "t-early",
            "alert_id": "a-early",
            "created_at": "2024-01-01T00:00:00+00:00",
            "headline": "Informational headline, no disruption signal",
            "run_duration_ms": 0,
            "tool_decision": "skip",
            "final_status": "inconclusive",
            "supplier": None,
            "risk_type": None,
            "risk_level": None,
            "change_type": "ignored",
            "trace_steps": [],
            # retriever_metadata, prompt_metadata, model_metadata intentionally absent
        }
        store.append(early_return_trace)
        result = store.get_by_identifier("t-early")
        assert result is not None
        assert "retriever_metadata" not in result
        assert "prompt_metadata" not in result
        assert "model_metadata" not in result
        # nullable scalar fields are still reconstructed as None
        assert result["supplier"] is None
        assert result["risk_type"] is None
        assert result["risk_level"] is None
