"""run_pipeline() on the OUTPUT_MODE=lambda path with RISK_STATE_BACKEND=dynamodb.

RSS fetch, the compiled LangGraph app and trace persistence are stubbed;
supplier state goes to a moto DynamoDB table. No live AWS or LLM calls.
"""
import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from moto import mock_aws
    MOTO_AVAILABLE = True
except ImportError:
    MOTO_AVAILABLE = False

pytestmark = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")

# supplier_risk_agent builds its LLM clients at import time; no request is made.
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
from app.workflows import supplier_risk_agent as agent  # noqa: E402

TABLE = "supplier_risk_state"
ACTIONABLE_HEADLINE = "TSMC halts production after earthquake in Taiwan"


def _ok_alert(level: str) -> dict:
    return {
        "status": "ok",
        "supplier": "TSMC",
        "risk_type": "earthquake",
        "risk_level": level,
        "impact": "Wafer output delayed",
        "recommended_action": "Contact supplier",
        "relevant_supplier_context": "Fabs in Taiwan",
    }


INCONCLUSIVE_ALERT = {
    "status": "inconclusive",
    "supplier": None,
    "risk_type": None,
    "risk_level": None,
    "impact": "No clearly relevant supplier could be identified from the current signal.",
    "recommended_action": "Monitor for more supplier-specific information before taking action.",
    "relevant_supplier_context": "",
}


@pytest.fixture
def lambda_env(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("OUTPUT_MODE", "lambda")
    monkeypatch.setenv("RISK_STATE_BACKEND", "dynamodb")
    monkeypatch.setenv("RISK_STATE_TABLE", TABLE)
    monkeypatch.setenv("MAX_ALERTS_PER_RUN", "1")
    monkeypatch.setattr(agent.time, "sleep", lambda _s: None)

    traces: list[dict] = []
    monkeypatch.setattr(agent, "append_risk_trace", traces.append)

    with mock_aws():
        import boto3
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        )
        yield SimpleNamespace(table=table, traces=traces, monkeypatch=monkeypatch)


def _run(env, headline: str, alert: dict | None) -> dict:
    """One run_pipeline() call with a single RSS headline and a fixed graph result."""
    env.monkeypatch.setattr(agent, "load_headlines_from_rss", lambda: [
        {"alert_id": "", "headline": headline, "source": "google_news_rss", "status": "new"},
    ])

    def fake_invoke(state, config=None, **kwargs):
        return {
            **state,
            "candidate_suppliers": ["TSMC"],
            "tool_decision": "skip",
            "alert": alert,
            "is_valid": True,
            "trace_steps": [],
        }

    env.monkeypatch.setattr(agent, "app", SimpleNamespace(invoke=fake_invoke))
    return agent.run_pipeline()


def _items(env) -> list[dict]:
    return env.table.scan()["Items"]


def _completion_logs(caplog) -> list[dict]:
    records = []
    for r in caplog.records:
        try:
            payload = json.loads(r.getMessage())
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("stage") == "alert_processing_complete":
            records.append(payload)
    return records


def test_ok_alert_writes_supplier_state(lambda_env, caplog):
    caplog.set_level(logging.INFO)

    summary = _run(lambda_env, ACTIONABLE_HEADLINE, _ok_alert("High"))

    assert summary["alerts_processed"] == 1
    assert summary["state_writes"] == 1
    items = _items(lambda_env)
    assert len(items) == 1
    assert items[0]["pk"] == "TSMC#earthquake"
    assert items[0]["current_risk_level"] == "High"
    assert items[0]["last_headline"] == ACTIONABLE_HEADLINE

    assert lambda_env.traces[-1]["alert_id"] == "runtime-1"
    assert lambda_env.traces[-1]["change_type"] == "new_alert"

    [log] = _completion_logs(caplog)
    assert log["alert_id"] == "runtime-1"
    assert log["analyzed_supplier"] == "TSMC"
    assert log["final_status"] == "ok"
    assert log["change_type"] == "new_alert"
    assert "supplier" not in log


def test_repeat_ok_alert_escalates_existing_state(lambda_env):
    _run(lambda_env, ACTIONABLE_HEADLINE, _ok_alert("Medium"))
    summary = _run(lambda_env, ACTIONABLE_HEADLINE, _ok_alert("High"))

    assert summary["state_writes"] == 1
    assert lambda_env.traces[-1]["change_type"] == "escalated"
    items = _items(lambda_env)
    assert len(items) == 1
    assert items[0]["current_risk_level"] == "High"


def test_inconclusive_alert_does_not_write_state(lambda_env, caplog):
    caplog.set_level(logging.INFO)

    summary = _run(lambda_env, ACTIONABLE_HEADLINE, dict(INCONCLUSIVE_ALERT))

    assert summary["alerts_processed"] == 1
    assert summary["enriched_alerts"] == 1
    assert summary["state_writes"] == 0
    assert _items(lambda_env) == []
    assert lambda_env.traces[-1]["change_type"] == "inconclusive"

    [log] = _completion_logs(caplog)
    assert log["analyzed_supplier"] is None
    assert log["final_status"] == "inconclusive"
    assert log["change_type"] == "inconclusive"


def test_non_actionable_headline_does_not_write_state(lambda_env, caplog):
    caplog.set_level(logging.INFO)

    summary = _run(lambda_env, "Quarterly earnings call scheduled for next week", None)

    assert summary["state_writes"] == 0
    assert _items(lambda_env) == []
    assert lambda_env.traces[-1]["change_type"] == "ignored"

    [log] = _completion_logs(caplog)
    assert log["final_status"] == "inconclusive"
    assert log["change_type"] == "ignored"
