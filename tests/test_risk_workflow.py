"""START / RESUME lifecycle over DynamoDB-shaped durable storage (moto).

Runs the real compiled graph with fake LLMs/retriever. Invocation boundaries
are simulated by discarding the saver/graph and constructing new ones: only
the thread_id and the (moto) tables carry over.
"""
import inspect
import json
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
from app.governance.action_ledger import ActionLedger  # noqa: E402
from app.governance.authority import AuthorityStore  # noqa: E402
from app.governance.policy import PolicyStore, materialize, parse_policy  # noqa: E402
from app.storage import risk_trace_store  # noqa: E402
from app.storage.checkpoint_store import DynamoCheckpointSaver  # noqa: E402
from app.storage.supplier_risk_state_store import DynamoRiskStateStore  # noqa: E402
from app.workflows import risk_workflow  # noqa: E402
from app.workflows import supplier_risk_agent as agent  # noqa: E402

REGION = "us-east-1"
CHECKPOINTS = "risk_agent_checkpoints"
SUPPLIER_STATE = "supplier_risk_state"
TRACES = "risk_traces"
AUTHORITY = "risk_agent_authority"
ACTIONS = "risk_agent_actions"
HEADLINE = "TSMC halts production after earthquake in Taiwan"
POLICY_V1 = parse_policy(
    '{"policy_version": 1, "subject": "agent/supplier-risk-agent",'
    ' "capabilities": {"open_review_ticket": "allow", "claude_inference": "allow"}}'
)

OK_ALERT = {
    "status": "ok",
    "supplier": "TSMC",
    "risk_type": "earthquake",
    "risk_level": "High",
    "impact": "Wafer output delayed",
    "recommended_action": "Contact supplier",
    "relevant_supplier_context": "Fabs in Taiwan",
}


class FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return self.response


class FakeRetriever:
    retriever_id = "fake_retriever"
    retriever_version = "v0"
    embedding_provider = "fake"
    top_k = 2

    def __init__(self):
        self.calls = 0

    def retrieve(self, query, suppliers):
        self.calls += 1
        return SimpleNamespace(context="TSMC fabs are in Taiwan.")


def _create_tables(boto3):
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=CHECKPOINTS,
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
    client.create_table(
        TableName=SUPPLIER_STATE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )
    client.create_table(
        TableName=TRACES,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "trace_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "trace_id", "KeyType": "HASH"}],
    )
    for table, key in ((AUTHORITY, "scope_id"), (ACTIONS, "action_id")):
        client.create_table(
            TableName=table,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
            KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
        )


class _NoopIam:
    """IAM policy layer stand-in for fixtures that don't exercise IAM."""

    def policy_arn(self, cap):
        return f"arn:aws:iam::123456789012:policy/{cap.deny_policy_name}"

    def attach(self, cap):
        return None

    def detach(self, cap):
        return None


@pytest.fixture
def env(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("TRACE_STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("RISK_TRACES_TABLE", TRACES)
    monkeypatch.setattr(risk_trace_store, "_store", None)
    monkeypatch.setenv("AUTHORITY_TABLE", AUTHORITY)
    monkeypatch.setenv("ACTIONS_TABLE", ACTIONS)

    triage = FakeLLM("retrieve")
    risk = FakeLLM(json.dumps(OK_ALERT))
    retriever = FakeRetriever()
    monkeypatch.setattr(agent, "_triage_llm", triage)
    monkeypatch.setattr(agent, "_risk_llm", risk)
    monkeypatch.setattr(agent, "_retriever", None)
    monkeypatch.setattr(agent, "_get_retriever", lambda: retriever)

    with mock_aws():
        import boto3
        _create_tables(boto3)
        authority = AuthorityStore(AUTHORITY, REGION)
        authority.restore(reason="test grant", actor="pytest")
        # Materialized policy v1 (item + completed record + latest pointer):
        # open_review_ticket allowed. IAM steps are irrelevant here.
        materialize(POLICY_V1, store=PolicyStore(AUTHORITY, REGION), iam=_NoopIam(), actor="pytest",
                    approve_broadening=True)
        resource = boto3.resource("dynamodb", region_name=REGION)
        yield SimpleNamespace(
            authority=authority,
            actions=resource.Table(ACTIONS),
            triage=triage,
            risk=risk,
            retriever=retriever,
            monkeypatch=monkeypatch,
            checkpoints=resource.Table(CHECKPOINTS),
            supplier_state=resource.Table(SUPPLIER_STATE),
            traces=resource.Table(TRACES),
        )


def _new_executor():
    """Fresh saver + risk store + action gate, as a new process would build them."""
    return {
        "checkpointer": DynamoCheckpointSaver(CHECKPOINTS, REGION),
        "risk_store": DynamoRiskStateStore(SUPPLIER_STATE, REGION),
        "action_gate": ActionLedger(ACTIONS, AuthorityStore(AUTHORITY, REGION), REGION),
    }


def _calls(env) -> tuple[int, int, int]:
    return env.triage.calls, env.retriever.calls, env.risk.calls


def _trace(trace_id: str) -> dict:
    return risk_trace_store.DynamoTraceStore(TRACES, REGION).get_by_identifier(trace_id)


def _nodes(trace: dict) -> list[str]:
    return [s["node_name"] for s in trace["trace_steps"]]


def _start_paused(env) -> dict:
    env.monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "stream-A")
    return risk_workflow.start_workflow(
        {"headline": HEADLINE, "alert_id": "alert-1"},
        pause_before=["analyze"],
        **_new_executor(),
    )


def test_start_pauses_without_business_write(env):
    outcome = _start_paused(env)

    assert outcome["status"] == "paused"
    assert outcome["next"] == ["analyze"]
    assert outcome["thread_id"].startswith("thread-")
    assert outcome["trace_id"] != outcome["thread_id"]
    assert outcome["state_written"] is False
    assert outcome["change_type"] is None
    assert _calls(env) == (1, 1, 0)
    assert env.supplier_state.scan()["Items"] == []

    trace = _trace(outcome["trace_id"])
    assert trace["thread_id"] == outcome["thread_id"]
    assert trace["workflow_event"] == "paused"
    assert trace["executor_id"] == "stream-A"
    assert trace["final_status"] is None
    assert _nodes(trace) == ["infer", "decide", "retrieve"]


def test_resume_with_new_executor_completes_without_rerunning(env):
    started = _start_paused(env)

    # Invocation boundary: new saver/store/graph, no in-process retriever.
    env.monkeypatch.setattr(agent, "_retriever", None)
    env.monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "stream-B")
    outcome = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())

    assert outcome["status"] == "completed"
    assert outcome["thread_id"] == started["thread_id"]
    assert outcome["trace_id"] not in (started["trace_id"], started["thread_id"])
    assert outcome["change_type"] == "new_alert"
    assert outcome["state_written"] is True
    assert outcome["supplier"] == "TSMC"
    assert _calls(env) == (1, 1, 1)

    [item] = env.supplier_state.scan()["Items"]
    assert item["pk"] == "TSMC#earthquake"
    assert item["current_risk_level"] == "High"

    trace = _trace(outcome["trace_id"])
    assert trace["thread_id"] == started["thread_id"]
    assert trace["workflow_event"] == "resumed_completed"
    assert trace["executor_id"] == "stream-B"
    assert trace["change_type"] == "new_alert"
    assert trace["retriever_metadata"]["retriever_id"] == "fake_retriever"
    assert _nodes(trace) == ["infer", "decide", "retrieve", "analyze", "validate", "act"]
    assert trace["action_status"] == "COMPLETED"
    assert trace["authority_version"] == 1


def test_resume_after_completion_is_noop(env):
    started = _start_paused(env)
    completed = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())
    [item_before] = env.supplier_state.scan()["Items"]

    again = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())

    assert again["status"] == "already_completed"
    assert again["trace_id"] == completed["trace_id"]
    assert again["state_written"] is False
    assert again["supplier"] == "TSMC"
    assert _calls(env) == (1, 1, 1)
    assert env.supplier_state.scan()["Items"] == [item_before]
    assert len(env.traces.scan()["Items"]) == 2


def test_resume_unknown_thread_is_not_found(env):
    outcome = risk_workflow.resume_workflow("thread-unknown", **_new_executor())

    assert outcome["status"] == "not_found"
    assert outcome["trace_id"] is None
    assert _calls(env) == (0, 0, 0)
    assert env.checkpoints.scan()["Items"] == []
    assert env.traces.scan()["Items"] == []


def test_start_without_pause_completes(env):
    outcome = risk_workflow.start_workflow({"headline": HEADLINE}, **_new_executor())

    assert outcome["status"] == "completed"
    assert outcome["change_type"] == "new_alert"
    assert outcome["state_written"] is True
    assert _trace(outcome["trace_id"])["workflow_event"] == "started_completed"
    assert len(env.supplier_state.scan()["Items"]) == 1


def test_start_non_actionable_headline_creates_no_thread(env):
    outcome = risk_workflow.start_workflow(
        {"headline": "Quarterly earnings call scheduled for next week"}, **_new_executor()
    )

    assert outcome["status"] == "ignored"
    assert outcome["thread_id"] is None
    assert _calls(env) == (0, 0, 0)
    assert env.checkpoints.scan()["Items"] == []
    assert _trace(outcome["trace_id"])["change_type"] == "ignored"


def test_phase2_process_alert_row_has_no_workflow_identity(env):
    """The uncheckpointed path records an attempt (trace_id) but no thread."""
    env.monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "stream-P2")
    enriched = agent.process_alert_row(
        {"headline": HEADLINE, "alert_id": "alert-p2"},
        DynamoRiskStateStore(SUPPLIER_STATE, REGION),
    )

    trace = _trace(enriched["trace_id"])
    assert trace["trace_id"] == enriched["trace_id"]
    assert trace["executor_id"] == "stream-P2"
    assert trace["thread_id"] is None
    assert trace["workflow_event"] is None
    assert trace["change_type"] == "new_alert"
    assert env.checkpoints.scan()["Items"] == []


def test_checkpointed_traces_carry_workflow_identity(env):
    started = _start_paused(env)
    resumed = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())

    for outcome, event in ((started, "paused"), (resumed, "resumed_completed")):
        trace = _trace(outcome["trace_id"])
        assert trace["thread_id"] == started["thread_id"]
        assert trace["workflow_event"] == event
        assert trace["trace_id"] == outcome["trace_id"]
        assert trace["executor_id"]


def test_resume_accepts_no_workflow_input():
    """thread_id is the only input; the rest are keyword-only dependencies."""
    params = inspect.signature(risk_workflow.resume_workflow).parameters
    assert list(params) == ["thread_id", "checkpointer", "risk_store", "action_gate"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for name, p in params.items() if name != "thread_id")
