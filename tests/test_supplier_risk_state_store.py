import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.storage.supplier_risk_state_store import (
    RiskStateStore,
    CsvRiskStateStore,
    DynamoRiskStateStore,
    get_risk_store,
)


def _row(supplier: str, risk_type: str, level: str = "High", seen_at: str = "2026-01-01T00:00:00+00:00") -> dict:
    return {
        "supplier": supplier,
        "risk_type": risk_type,
        "current_risk_level": level,
        "last_headline": f"{supplier} {risk_type} headline",
        "last_seen_at": seen_at,
    }


# ---- CSV ----

def test_csv_missing_file_is_empty(tmp_path):
    store = CsvRiskStateStore(tmp_path / "missing.csv")
    assert store.get("TSMC", "earthquake") is None
    assert store.list_all() == []


def test_csv_round_trip(tmp_path):
    path = tmp_path / "risk_state.csv"
    store = CsvRiskStateStore(path)
    store.put("TSMC", "earthquake", _row("TSMC", "earthquake", "High"))
    store.put("Foxconn", "strike", _row("Foxconn", "strike", "Medium"))
    store.flush()

    reloaded = CsvRiskStateStore(path)
    assert reloaded.get("TSMC", "earthquake") == _row("TSMC", "earthquake", "High")
    assert reloaded.get("Foxconn", "strike")["current_risk_level"] == "Medium"
    assert reloaded.get("TSMC", "flood") is None


def test_csv_put_overwrites_same_key(tmp_path):
    store = CsvRiskStateStore(tmp_path / "risk_state.csv")
    store.put("TSMC", "earthquake", _row("TSMC", "earthquake", "Medium"))
    store.put("TSMC", "earthquake", _row("TSMC", "earthquake", "High"))
    assert store.list_all() == [_row("TSMC", "earthquake", "High")]


def test_csv_list_all_reflects_unflushed_puts(tmp_path):
    store = CsvRiskStateStore(tmp_path / "risk_state.csv")
    store.put("TSMC", "earthquake", _row("TSMC", "earthquake"))
    assert store.list_all() == [_row("TSMC", "earthquake")]


def test_csv_list_all_is_canonical_state(tmp_path):
    """One row per (supplier, risk_type): rows with empty keys are skipped
    and the last duplicate wins."""
    path = tmp_path / "risk_state.csv"
    path.write_text(
        "supplier,risk_type,current_risk_level,last_headline,last_seen_at\n"
        "TSMC,earthquake,Medium,old,2026-01-01T00:00:00+00:00\n"
        "TSMC,earthquake,High,new,2026-01-02T00:00:00+00:00\n"
        "TSMC,,High,no type,2026-01-03T00:00:00+00:00\n"
        ",flood,High,no supplier,2026-01-03T00:00:00+00:00\n"
        "Foxconn,strike,Low,h,2026-01-01T00:00:00+00:00\n"
    )
    rows = CsvRiskStateStore(path).list_all()
    assert [(r["supplier"], r["risk_type"], r["last_headline"]) for r in rows] == [
        ("TSMC", "earthquake", "new"),
        ("Foxconn", "strike", "h"),
    ]


def test_store_classes_implement_abstraction(tmp_path):
    assert isinstance(CsvRiskStateStore(tmp_path / "x.csv"), RiskStateStore)
    assert issubclass(DynamoRiskStateStore, RiskStateStore)


# ---- Factory / config ----

def test_factory_defaults_to_csv(tmp_path, monkeypatch):
    monkeypatch.delenv("RISK_STATE_BACKEND", raising=False)
    monkeypatch.delenv("RISK_STATE_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    store = get_risk_store()
    assert isinstance(store, CsvRiskStateStore)
    assert str(store.path) == "risk_state.csv"


def test_factory_csv_uses_risk_state_file(tmp_path, monkeypatch):
    path = tmp_path / "custom.csv"
    monkeypatch.setenv("RISK_STATE_BACKEND", "CSV")
    monkeypatch.setenv("RISK_STATE_FILE", str(path))
    store = get_risk_store()
    assert isinstance(store, CsvRiskStateStore)
    assert store.path == path


def test_factory_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("RISK_STATE_BACKEND", "redis")
    with pytest.raises(ValueError, match="Unsupported RISK_STATE_BACKEND: redis"):
        get_risk_store()


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


def _create_state_table(table_name: str = "supplier_risk_state", region: str = "us-east-1") -> None:
    import boto3
    client = boto3.client("dynamodb", region_name=region)
    client.create_table(
        TableName=table_name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )


@moto_required
def test_dynamo_get_put(aws_credentials):
    with mock_aws():
        _create_state_table()
        store = DynamoRiskStateStore("supplier_risk_state", "us-east-1")
        assert store.get("TSMC", "earthquake") is None

        store.put("TSMC", "earthquake", _row("TSMC", "earthquake", "High"))
        item = store.get("TSMC", "earthquake")
        assert item == {**_row("TSMC", "earthquake", "High"), "pk": "TSMC#earthquake"}

        store.put("TSMC", "earthquake", _row("TSMC", "earthquake", "Low"))
        assert store.get("TSMC", "earthquake")["current_risk_level"] == "Low"


@moto_required
def test_dynamo_list_all(aws_credentials):
    with mock_aws():
        _create_state_table()
        store = DynamoRiskStateStore("supplier_risk_state", "us-east-1")
        assert store.list_all() == []
        store.put("TSMC", "earthquake", _row("TSMC", "earthquake"))
        store.put("Foxconn", "strike", _row("Foxconn", "strike", "Medium"))
        rows = store.list_all()
        assert sorted(r["pk"] for r in rows) == ["Foxconn#strike", "TSMC#earthquake"]


@moto_required
def test_dynamo_list_all_paginates(aws_credentials):
    with mock_aws():
        _create_state_table()
        store = DynamoRiskStateStore("supplier_risk_state", "us-east-1")
        for i in range(5):
            store.put(f"S{i}", "flood", _row(f"S{i}", "flood"))

        scan_calls = []
        real_scan = store.table.scan

        def one_item_scan(**kwargs):
            scan_calls.append(kwargs)
            return real_scan(Limit=1, **kwargs)

        store.table.scan = one_item_scan
        rows = store.list_all()
        assert len(rows) == 5
        assert len(scan_calls) >= 5
        assert "ExclusiveStartKey" in scan_calls[1]


@moto_required
def test_factory_dynamodb_uses_risk_state_table(aws_credentials, monkeypatch):
    monkeypatch.setenv("RISK_STATE_BACKEND", "dynamodb")
    monkeypatch.setenv("RISK_STATE_TABLE", "custom_state")
    with mock_aws():
        _create_state_table("custom_state")
        store = get_risk_store()
        assert isinstance(store, DynamoRiskStateStore)
        assert store.table.name == "custom_state"


@moto_required
def test_factory_dynamodb_default_table(aws_credentials, monkeypatch):
    monkeypatch.setenv("RISK_STATE_BACKEND", "dynamodb")
    monkeypatch.delenv("RISK_STATE_TABLE", raising=False)
    with mock_aws():
        store = get_risk_store()
        assert store.table.name == "supplier_risk_state"
