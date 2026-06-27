import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.storage.risk_state_store import (
    JsonlDecisionStore,
    DynamoDecisionStore,
    get_decision_store,
    REVIEW_REQUIRED_DECISIONS,
)


def _event() -> dict:
    return {"event_type": "push", "repo": "acme/service", "branch": "main", "actor": "alice"}


def _decision(dtype: str = "safe_to_merge", score: int = 10) -> dict:
    return {"decision": dtype, "risk_score": score, "reason": "Test reason"}


# ---- JSONL ----

def test_jsonl_save_and_get_recent(tmp_path):
    store = JsonlDecisionStore(tmp_path / "decisions.jsonl")
    store.save(_event(), _decision())
    store.save(_event(), _decision("manual_review_required", 80))
    id3 = store.save(_event(), _decision("review_recommended", 60))
    records = store.get_recent(10)
    assert len(records) == 3
    assert records[0]["id"] == id3  # newest first
    assert records[0]["store_backend"] == "jsonl"


def test_jsonl_get_recent_respects_limit(tmp_path):
    store = JsonlDecisionStore(tmp_path / "decisions.jsonl")
    for _ in range(5):
        store.save(_event(), _decision())
    assert len(store.get_recent(3)) == 3


def test_jsonl_get_requiring_review_filters_correctly(tmp_path):
    store = JsonlDecisionStore(tmp_path / "decisions.jsonl")
    store.save(_event(), _decision("safe_to_merge", 10))
    store.save(_event(), _decision("manual_review_required", 80))
    store.save(_event(), _decision("review_recommended", 60))
    results = store.get_requiring_review(10)
    assert len(results) == 2
    for r in results:
        assert r["decision"]["decision"] in REVIEW_REQUIRED_DECISIONS


def test_jsonl_get_requiring_review_respects_limit(tmp_path):
    store = JsonlDecisionStore(tmp_path / "decisions.jsonl")
    for _ in range(5):
        store.save(_event(), _decision("manual_review_required", 80))
    assert len(store.get_requiring_review(2)) == 2


def test_jsonl_empty_store(tmp_path):
    store = JsonlDecisionStore(tmp_path / "decisions.jsonl")
    assert store.get_recent() == []
    assert store.get_requiring_review() == []


def test_get_decision_store_factory_returns_jsonl(monkeypatch):
    monkeypatch.setenv("DECISION_STORE_BACKEND", "jsonl")
    store = get_decision_store()
    assert isinstance(store, JsonlDecisionStore)


def test_get_decision_store_factory_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("DECISION_STORE_BACKEND", "redis")
    with pytest.raises(ValueError, match="Unsupported DECISION_STORE_BACKEND: 'redis'"):
        get_decision_store()


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


def _create_decision_table(region: str = "us-east-1") -> None:
    import boto3
    client = boto3.client("dynamodb", region_name=region)
    client.create_table(
        TableName="risk_decisions",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    )


@moto_required
def test_dynamo_save_and_get_recent(aws_credentials):
    with mock_aws():
        _create_decision_table()
        store = DynamoDecisionStore("risk_decisions", "us-east-1")
        store.save(_event(), _decision("safe_to_merge", 10))
        id2 = store.save(_event(), _decision("manual_review_required", 80))
        records = store.get_recent(10)
        assert len(records) == 2
        assert records[0]["store_backend"] == "dynamodb"
        # verify round-trip — newest first by timestamp
        assert records[0]["id"] == id2
        assert records[0]["decision"]["risk_score"] == 80
        assert records[0]["normalized_event"]["repo"] == "acme/service"


@moto_required
def test_dynamo_get_requiring_review_filters_correctly(aws_credentials):
    with mock_aws():
        _create_decision_table()
        store = DynamoDecisionStore("risk_decisions", "us-east-1")
        store.save(_event(), _decision("safe_to_merge", 10))
        store.save(_event(), _decision("manual_review_required", 80))
        store.save(_event(), _decision("review_recommended", 60))
        results = store.get_requiring_review(10)
        assert len(results) == 2
        for r in results:
            assert r["decision"]["decision"] in REVIEW_REQUIRED_DECISIONS
