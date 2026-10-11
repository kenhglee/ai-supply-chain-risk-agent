"""lambda_handler routing: default RSS path vs explicit checkpoint actions.

Routing tests stub the pipeline and lifecycle. The end-to-end test runs
START -> RESUME through lambda_handler against moto tables with fake LLMs,
reusing the lifecycle fixture from test_risk_workflow.
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# supplier_risk_agent builds its LLM clients at import time; no request is made.
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
from handlers.rss_handler import handler  # noqa: E402
from app.workflows import supplier_risk_agent as agent  # noqa: E402
from tests.test_risk_workflow import (  # noqa: E402,F401  (env is a pytest fixture)
    CHECKPOINTS,
    HEADLINE,
    MOTO_AVAILABLE,
    SUPPLIER_STATE,
    env,
)

THREAD_ID = "thread-" + "a" * 32
CONTEXT = SimpleNamespace(aws_request_id="req-1")


def _body(response: dict) -> dict:
    return json.loads(response["body"])


def _outcome(status: str, **overrides) -> dict:
    outcome = {
        "status": status,
        "thread_id": THREAD_ID,
        "trace_id": "trace-x",
        "next": [],
        "change_type": None,
        "state_written": False,
        "final_status": None,
        "supplier": None,
        "risk_type": None,
        "risk_level": None,
    }
    outcome.update(overrides)
    return outcome


@pytest.fixture
def stubs(monkeypatch):
    """Stub the pipeline, lifecycle and dependency constructors; record calls."""
    calls = SimpleNamespace(pipeline=[], start=[], resume=[], savers=[], stores=[])

    def fake_pipeline(event):
        calls.pipeline.append(event)
        return {"alerts_processed": 1, "state_writes": 0}

    def fake_start(row, *, checkpointer, risk_store, pause_before=None):
        calls.start.append({"row": row, "pause_before": pause_before,
                            "checkpointer": checkpointer, "risk_store": risk_store})
        return calls.start_outcome

    def fake_resume(thread_id, *, checkpointer, risk_store):
        calls.resume.append({"thread_id": thread_id, "checkpointer": checkpointer, "risk_store": risk_store})
        return calls.resume_outcome

    class FakeSaver:
        def __init__(self, table_name, region=None):
            self.table_name = table_name
            calls.savers.append(self)

    def fake_get_risk_store():
        store = object()
        calls.stores.append(store)
        return store

    calls.start_outcome = _outcome("paused", next=["analyze"])
    calls.resume_outcome = _outcome("completed", change_type="new_alert", state_written=True,
                                    final_status="ok", supplier="TSMC")

    monkeypatch.setenv("CHECKPOINT_TABLE", "risk_agent_checkpoints")
    monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "stream-1")
    monkeypatch.setattr(handler, "run_supplier_risk_flow", fake_pipeline)
    monkeypatch.setattr(handler, "start_workflow", fake_start)
    monkeypatch.setattr(handler, "resume_workflow", fake_resume)
    monkeypatch.setattr(handler, "DynamoCheckpointSaver", FakeSaver)
    monkeypatch.setattr(handler, "get_risk_store", fake_get_risk_store)
    return calls


def _assert_no_workflow_calls(calls) -> None:
    assert calls.pipeline == []
    assert calls.start == []
    assert calls.resume == []
    assert calls.savers == []


# ---- Default path unchanged ----

@pytest.mark.parametrize("event", [
    {},
    {"source": "local-test"},
    {
        "version": "0",
        "id": "e1",
        "detail-type": "Scheduled Event",
        "source": "aws.events",
        "account": "123456789012",
        "time": "2026-10-11T00:00:00Z",
        "region": "us-west-2",
        "resources": ["arn:aws:events:us-west-2:123456789012:rule/rss"],
        "detail": {},
    },
])
def test_events_without_action_run_rss_pipeline(stubs, event):
    response = handler.lambda_handler(event, CONTEXT)

    assert response == {"statusCode": 200, "body": json.dumps({"alerts_processed": 1, "state_writes": 0})}
    assert stubs.pipeline == [event]
    assert stubs.start == [] and stubs.resume == [] and stubs.savers == []


# ---- checkpoint_start ----

def test_checkpoint_start_routes_with_pause_before_analyze(stubs):
    response = handler.lambda_handler({"action": "checkpoint_start", "headline": f"  {HEADLINE} "}, CONTEXT)

    assert response["statusCode"] == 200
    [call] = stubs.start
    assert call["row"] == {"headline": HEADLINE}
    assert call["pause_before"] == ["analyze"]
    assert call["checkpointer"].table_name == "risk_agent_checkpoints"
    assert stubs.pipeline == [] and stubs.resume == []
    assert _body(response) == {
        "action": "checkpoint_start",
        "status": "paused",
        "thread_id": THREAD_ID,
        "trace_id": "trace-x",
        "next": ["analyze"],
        "change_type": None,
        "state_written": False,
        "final_status": None,
        "supplier": None,
        "executor_id": "stream-1",
        "request_id": "req-1",
    }


# ---- checkpoint_resume ----

def test_checkpoint_resume_receives_only_thread_id(stubs):
    response = handler.lambda_handler({"action": "checkpoint_resume", "thread_id": THREAD_ID}, CONTEXT)

    assert response["statusCode"] == 200
    [call] = stubs.resume
    assert call["thread_id"] == THREAD_ID
    assert set(call) == {"thread_id", "checkpointer", "risk_store"}
    body = _body(response)
    assert body["action"] == "checkpoint_resume"
    assert body["status"] == "completed"
    assert body["change_type"] == "new_alert"
    assert body["state_written"] is True
    assert stubs.pipeline == [] and stubs.start == []


@pytest.mark.parametrize("status", ["already_completed", "paused"])
def test_checkpoint_resume_other_success_statuses_are_200(stubs, status):
    stubs.resume_outcome = _outcome(status)
    response = handler.lambda_handler({"action": "checkpoint_resume", "thread_id": THREAD_ID}, CONTEXT)
    assert response["statusCode"] == 200
    assert _body(response)["status"] == status


def test_checkpoint_resume_not_found_is_404(stubs):
    stubs.resume_outcome = _outcome("not_found", trace_id=None)
    response = handler.lambda_handler({"action": "checkpoint_resume", "thread_id": THREAD_ID}, CONTEXT)
    assert response["statusCode"] == 404
    assert _body(response)["status"] == "not_found"


def test_dependencies_constructed_per_invocation(stubs):
    handler.lambda_handler({"action": "checkpoint_start", "headline": HEADLINE}, CONTEXT)
    handler.lambda_handler({"action": "checkpoint_resume", "thread_id": THREAD_ID}, CONTEXT)

    assert len(stubs.savers) == 2
    assert stubs.savers[0] is not stubs.savers[1]
    assert stubs.stores[0] is not stubs.stores[1]
    assert stubs.start[0]["checkpointer"] is stubs.savers[0]
    assert stubs.resume[0]["checkpointer"] is stubs.savers[1]


# ---- Rejected requests (400, no work done) ----

@pytest.mark.parametrize("event", [
    {"action": "checkpoint_start"},
    {"action": "checkpoint_start", "headline": ""},
    {"action": "checkpoint_start", "headline": "   "},
    {"action": "checkpoint_start", "headline": 42},
    {"action": "checkpoint_start", "headline": HEADLINE, "pause_before": ["validate"]},
    {"action": "checkpoint_start", "headline": HEADLINE, "thread_id": THREAD_ID},
    {"action": "checkpoint_resume"},
    {"action": "checkpoint_resume", "thread_id": THREAD_ID, "headline": HEADLINE},
    {"action": "checkpoint_resume", "thread_id": "thread-123"},
    {"action": "checkpoint_resume", "thread_id": "thread-" + "A" * 32},
    {"action": "checkpoint_resume", "thread_id": THREAD_ID + "0"},
    {"action": "checkpoint_resume", "thread_id": None},
    {"action": "checkpoint_delete", "thread_id": THREAD_ID},
    {"action": None},
    {"action": ""},
])
def test_invalid_checkpoint_requests_are_400(stubs, event):
    response = handler.lambda_handler(event, CONTEXT)

    assert response["statusCode"] == 400
    body = _body(response)
    assert body["error"]
    assert body["request_id"] == "req-1"
    _assert_no_workflow_calls(stubs)


# ---- Configuration failure ----

def test_missing_checkpoint_table_is_configuration_error(stubs, monkeypatch):
    monkeypatch.delenv("CHECKPOINT_TABLE")
    with pytest.raises(RuntimeError, match="CHECKPOINT_TABLE"):
        handler.lambda_handler({"action": "checkpoint_start", "headline": HEADLINE}, CONTEXT)
    assert stubs.start == []


# ---- End to end: START -> RESUME through lambda_handler (moto + fake LLMs) ----

@pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")
def test_start_then_resume_through_handler(env, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_TABLE", CHECKPOINTS)
    monkeypatch.setenv("RISK_STATE_BACKEND", "dynamodb")
    monkeypatch.setenv("RISK_STATE_TABLE", SUPPLIER_STATE)

    savers = []

    class CountingSaver(handler.DynamoCheckpointSaver):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            savers.append(self)

    monkeypatch.setattr(handler, "DynamoCheckpointSaver", CountingSaver)

    monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "stream-A")
    started = handler.lambda_handler(
        {"action": "checkpoint_start", "headline": HEADLINE}, SimpleNamespace(aws_request_id="req-A")
    )
    start_body = _body(started)
    assert started["statusCode"] == 200
    assert start_body["status"] == "paused"
    assert start_body["next"] == ["analyze"]
    assert start_body["state_written"] is False
    assert env.supplier_state.scan()["Items"] == []
    assert (env.triage.calls, env.retriever.calls, env.risk.calls) == (1, 1, 0)

    # New invocation: nothing carried over except thread_id and the tables.
    monkeypatch.setattr(agent, "_retriever", None)
    monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "stream-B")
    resumed = handler.lambda_handler(
        {"action": "checkpoint_resume", "thread_id": start_body["thread_id"]},
        SimpleNamespace(aws_request_id="req-B"),
    )
    resume_body = _body(resumed)
    assert resumed["statusCode"] == 200
    assert resume_body["status"] == "completed"
    assert resume_body["thread_id"] == start_body["thread_id"]
    assert resume_body["trace_id"] != start_body["trace_id"]
    assert resume_body["state_written"] is True
    assert resume_body["executor_id"] == "stream-B"
    assert (env.triage.calls, env.retriever.calls, env.risk.calls) == (1, 1, 1)
    assert len(env.supplier_state.scan()["Items"]) == 1

    again = handler.lambda_handler(
        {"action": "checkpoint_resume", "thread_id": start_body["thread_id"]},
        SimpleNamespace(aws_request_id="req-C"),
    )
    assert _body(again)["status"] == "already_completed"
    assert len(savers) == 3
    assert len({id(s) for s in savers}) == 3
