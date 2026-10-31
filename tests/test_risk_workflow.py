"""START / RESUME lifecycle over DynamoDB-shaped durable storage (moto).

Runs the real compiled graph with fake LLMs/retriever. Invocation boundaries
are simulated by discarding the saver/graph and constructing new ones: only
the thread_id and the (moto) tables carry over.
"""
import importlib.util
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
from app.storage.finalization import FinalizationStore  # noqa: E402
from app.storage.thread_lease import LeaseStore  # noqa: E402
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
    """Owned dependencies, as checkpoint_dependencies_from_env builds them in a new process."""
    return {
        "checkpointer": DynamoCheckpointSaver(CHECKPOINTS, REGION, fenced=True),
        "risk_store": DynamoRiskStateStore(SUPPLIER_STATE, REGION),
        "action_gate": ActionLedger(ACTIONS, AuthorityStore(AUTHORITY, REGION), REGION, lease_table=CHECKPOINTS),
        "ownership": risk_workflow.Ownership(LeaseStore(CHECKPOINTS, REGION),
                                             FinalizationStore(CHECKPOINTS, SUPPLIER_STATE, REGION)),
    }


def _unfenced_executor():
    """Pre-ownership dependencies: only for building legacy state, or the explicit unfenced baseline."""
    return {
        "checkpointer": DynamoCheckpointSaver(CHECKPOINTS, REGION),
        "risk_store": DynamoRiskStateStore(SUPPLIER_STATE, REGION),
        "action_gate": ActionLedger(ACTIONS, AuthorityStore(AUTHORITY, REGION), REGION),
        "unfenced_baseline": True,
    }


def _calls(env) -> tuple[int, int, int]:
    return env.triage.calls, env.retriever.calls, env.risk.calls


def _trace(trace_id: str) -> dict:
    return risk_trace_store.DynamoTraceStore(TRACES, REGION).get_by_trace_id(trace_id)


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
    assert _nodes(trace) == ["infer", "decide", "retrieve", "analyze", "validate", "assess", "act"]
    assert trace["action_status"] == "COMPLETED"
    assert trace["authority_version"] == 1


def _load_proof_script():
    """scripts/durable_resume_proof.py: its tripwires are the ones the two-process proof uses."""
    spec = importlib.util.spec_from_file_location(
        "durable_resume_proof", os.path.join(PROJECT_ROOT, "scripts", "durable_resume_proof.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pause_before_validate_resumes_without_retrieval_or_inference(env):
    """Analysis is checkpointed at START; RESUME runs only validate and act."""
    env.monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "stream-A")
    started = risk_workflow.start_workflow(
        {"headline": HEADLINE, "alert_id": "alert-1"}, pause_before=["validate"], **_new_executor())

    assert started["status"] == "paused"
    assert started["next"] == ["validate"]
    assert started["final_status"] == "ok"  # the saved analysis, not a completed outcome
    assert started["state_written"] is False
    assert _calls(env) == (1, 1, 1)
    assert env.supplier_state.scan()["Items"] == []
    assert env.actions.scan()["Items"] == []
    paused_trace = _trace(started["trace_id"])
    assert paused_trace["workflow_event"] == "paused"
    assert _nodes(paused_trace) == ["infer", "decide", "retrieve", "analyze"]

    # New executor: every retrieval, embedding and model entry point is a tripwire.
    tripwires = _load_proof_script().install_resume_tripwires(env.monkeypatch.setattr)
    env.monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "stream-B")
    outcome = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())

    assert tripwires.hits == []
    assert {call.split(":")[0] for call in tripwires.aws_calls} == {"dynamodb"}
    assert _calls(env) == (1, 1, 1)
    assert outcome["status"] == "completed"
    assert outcome["thread_id"] == started["thread_id"]
    assert outcome["trace_id"] != started["trace_id"]
    assert outcome["change_type"] == "new_alert"
    assert outcome["action_status"] == "COMPLETED"
    assert (outcome["eligibility_change_type"], outcome["eligibility_basis"]) == ("new_alert", "assessed")
    [item] = env.supplier_state.scan()["Items"]
    assert item["pk"] == "TSMC#earthquake"

    trace = _trace(outcome["trace_id"])
    assert trace["executor_id"] == "stream-B"
    assert trace["workflow_event"] == "resumed_completed"
    assert _nodes(trace) == ["infer", "decide", "retrieve", "analyze", "validate", "assess", "act"]
    assert trace["trace_steps"][:4] == paused_trace["trace_steps"]


def test_resume_tripwires_fire_when_analysis_is_not_checkpointed(env):
    """Control: paused before analyze, RESUME must call the risk model and trips."""
    started = _start_paused(env)
    proof = _load_proof_script()
    tripwires = proof.install_resume_tripwires(env.monkeypatch.setattr)

    with pytest.raises(proof.TripwireError, match="risk_analysis"):
        risk_workflow.resume_workflow(started["thread_id"], **_new_executor())

    assert tripwires.hits == ["model risk_analysis.invoke"]
    assert env.supplier_state.scan()["Items"] == []


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
    assert list(params) == ["thread_id", "checkpointer", "risk_store", "action_gate", "ownership",
                            "unfenced_baseline"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for name, p in params.items() if name != "thread_id")


def test_proof_evidence_redacts_host_and_sts_session():
    text = json.dumps({
        "executor_id": "build-host.local:123",
        "caller_arn": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Admin_abc/someone@example.com",
    })
    redacted = _load_proof_script().redact_evidence_text(text, hostname="build-host.local")
    assert "build-host.local" not in redacted and "someone" not in redacted
    assert "<host>:123" in redacted
    assert "assumed-role/AWSReservedSSO_Admin_abc/<session>" in redacted


# ---- Action eligibility (assess before act) ----

def _seed_state(level: str, headline: str = "earlier headline") -> dict:
    row = {"supplier": "TSMC", "risk_type": "earthquake", "current_risk_level": level,
           "last_headline": headline, "last_seen_at": "2026-01-01T00:00:00+00:00"}
    DynamoRiskStateStore(SUPPLIER_STATE, REGION).put("TSMC", "earthquake", row)
    return row


def _state_row() -> dict:
    [item] = DynamoRiskStateStore(SUPPLIER_STATE, REGION).list_all()
    return item


def _start_completed(env, risk_level: str = "High") -> dict:
    env.risk.response = json.dumps({**OK_ALERT, "risk_level": risk_level})
    return risk_workflow.start_workflow({"headline": HEADLINE, "alert_id": "alert-1"}, **_new_executor())


@pytest.mark.parametrize("prior_level, alert_level, expected", [
    (None, "High", "new_alert"),
    ("Medium", "High", "escalated"),
])
def test_action_eligible_changes_create_an_action(env, prior_level, alert_level, expected):
    if prior_level:
        _seed_state(prior_level)

    outcome = _start_completed(env, alert_level)

    assert outcome["status"] == "completed"
    assert (outcome["eligibility_change_type"], outcome["action_eligible"]) == (expected, True)
    assert outcome["eligibility_basis"] == "assessed"
    assert outcome["change_type"] == expected
    assert outcome["action_status"] == "COMPLETED"
    [action] = env.actions.scan()["Items"]
    assert action["action_id"] == agent.review_ticket_action_id(outcome["thread_id"])
    assert _state_row()["current_risk_level"] == alert_level
    trace = _trace(outcome["trace_id"])
    assert _nodes(trace)[-2:] == ["assess", "act"]
    assert (trace["eligibility_change_type"], trace["change_type"]) == (expected, expected)


@pytest.mark.parametrize("prior_level, alert_level, expected", [
    ("High", "High", "suppressed"),
    ("High", "Medium", "downgraded"),
])
def test_suppressed_and_downgraded_take_no_action_but_update_state(env, prior_level, alert_level, expected):
    _seed_state(prior_level)

    outcome = _start_completed(env, alert_level)

    assert outcome["status"] == "completed"
    assert (outcome["eligibility_change_type"], outcome["action_eligible"]) == (expected, False)
    assert outcome["change_type"] == expected
    assert outcome["state_written"] is True
    assert (outcome["action_id"], outcome["action_status"]) == (None, None)
    assert env.actions.scan()["Items"] == []
    row = _state_row()
    assert (row["current_risk_level"], row["last_headline"]) == (alert_level, HEADLINE)
    trace = _trace(outcome["trace_id"])
    assert _nodes(trace)[-1] == "assess"
    assert trace["trace_steps"][-1]["decision"] == "no_action"
    assert trace["action_eligible"] is False


def test_denial_leaves_business_state_unchanged(env):
    seeded = _seed_state("Medium")
    env.authority.revoke(reason="incident", actor="pytest")

    outcome = _start_completed(env, "High")

    assert outcome["status"] == "authority_denied"
    assert outcome["next"] == ["act"]
    assert (outcome["eligibility_change_type"], outcome["action_eligible"]) == ("escalated", True)
    assert outcome["state_written"] is False
    assert _state_row() == {**seeded, "pk": "TSMC#earthquake"}
    [action] = env.actions.scan()["Items"]
    assert action["status"] == "DENIED"


def test_resume_after_denial_reuses_saved_assessment(env):
    """The checkpointed assessment decides act on RESUME, even though the
    supplier state changed meanwhile; the write-time change_type is separate."""
    env.authority.revoke(reason="incident", actor="pytest")
    denied = _start_completed(env, "High")
    assert denied["eligibility_change_type"] == "new_alert"

    # Another workflow writes the same supplier/risk at the same level while this one is parked.
    _seed_state("High", headline="another workflow")
    env.authority.restore(reason="resolved", actor="pytest")
    outcome = risk_workflow.resume_workflow(denied["thread_id"], **_new_executor())

    assert outcome["status"] == "completed"
    assert outcome["action_status"] == "COMPLETED"
    assert (outcome["eligibility_change_type"], outcome["change_type"]) == ("new_alert", "suppressed")
    denial_trace, trace = _trace(denied["trace_id"]), _trace(outcome["trace_id"])
    assert _nodes(trace) == _nodes(denial_trace) + ["act"]
    assert trace["trace_steps"][-2] == denial_trace["trace_steps"][-1]  # assess not re-run
    assert (trace["eligibility_change_type"], trace["change_type"]) == ("new_alert", "suppressed")
    assert _calls(env) == (1, 1, 1)


def _build_legacy_app(checkpointer, action_gate):
    """The pre-assess governed graph (Phase 3D-3F): validate -> act directly."""
    from langgraph.graph import END, StateGraph

    graph = StateGraph(agent.RiskState)
    for name, fn in (("infer", agent.infer_suppliers), ("decide", agent.decide_tool_use),
                     ("retrieve", agent.retrieve_context), ("analyze", agent.analyze_risk),
                     ("validate", agent.validate_alert), ("fallback", agent.fallback_alert)):
        graph.add_node(name, fn)
    graph.add_node("act", agent.make_act_node(action_gate))
    graph.set_entry_point("infer")
    graph.add_edge("infer", "decide")
    graph.add_conditional_edges("decide", agent.route_after_decision, {"retrieve": "retrieve", "analyze": "analyze"})
    graph.add_edge("retrieve", "analyze")
    graph.add_edge("analyze", "validate")
    graph.add_conditional_edges(
        "validate",
        lambda s: "fallback" if not s.get("is_valid") else ("act" if s["alert"]["status"] == "ok" else "end"),
        {"act": "act", "end": END, "fallback": "fallback"},
    )
    graph.add_edge("act", END)
    graph.add_edge("fallback", END)
    return graph.compile(checkpointer=checkpointer)


def test_legacy_checkpoint_parked_at_act_still_resumes(env):
    _seed_state("High")  # would be suppressed if it were assessed now
    deps = _unfenced_executor()
    thread_id = "thread-" + "c" * 32
    state = agent.initial_risk_state("alert-legacy", thread_id, "trace-legacy", HEADLINE)
    del state["assessment"]  # the old RiskState had no such channel
    legacy = _build_legacy_app(deps["checkpointer"], deps["action_gate"])
    legacy.invoke(state, {"configurable": {"thread_id": thread_id}},
                  interrupt_before=["act"], durability="sync")

    # Parked at act while authority is revoked: denied, recorded as legacy.
    env.authority.revoke(reason="incident", actor="pytest")
    denied = risk_workflow.resume_workflow(thread_id, **_new_executor())
    assert denied["status"] == "authority_denied"
    assert denied["eligibility_basis"] == "legacy_unassessed"
    assert _trace(denied["trace_id"])["eligibility_basis"] == "legacy_unassessed"

    env.authority.restore(reason="resolved", actor="pytest")
    outcome = risk_workflow.resume_workflow(thread_id, **_new_executor())

    assert outcome["status"] == "completed"
    assert outcome["action_status"] == "COMPLETED"  # legacy behavior: no eligibility gate
    assert outcome["eligibility_basis"] == "legacy_unassessed"
    assert (outcome["eligibility_change_type"], outcome["action_eligible"]) == (None, None)
    assert outcome["change_type"] == "suppressed"
    trace = _trace(outcome["trace_id"])
    assert trace["eligibility_basis"] == "legacy_unassessed"
    assert "assess" not in _nodes(trace)
    assert _nodes(trace)[-1] == "act"


# ---- DR1 harness: action checks against real workflow outputs ----

def _harness_action_checks(env, outcome, prior_row, expect="auto"):
    alert = {**OK_ALERT, "risk_level": outcome["risk_level"]}
    action_id = agent.review_ticket_action_id(outcome["thread_id"])
    item = env.actions.get_item(Key={"action_id": action_id}, ConsistentRead=True).get("Item")
    return _load_proof_script().action_checks(
        expect, alert=alert, headline=HEADLINE, prior_row=prior_row, b_outcome=outcome,
        b_steps=_trace(outcome["trace_id"])["trace_steps"], action_item=item, action_id=action_id,
    )


def _all_ok(checks: dict) -> bool:
    return all(c["ok"] for c in checks.values())


def test_harness_suppressed_run_passes_only_by_skipping_the_action(env):
    prior = _seed_state("High")
    outcome = _start_completed(env, "High")
    checks = _harness_action_checks(env, outcome, prior)
    assert _all_ok(checks), checks
    assert checks["eligibility_assessed_by_b"]["detail"]["expected_change"] == "suppressed"

    # Had a ticket been opened for this suppressed run, the harness must fail it.
    action_id = agent.review_ticket_action_id(outcome["thread_id"])
    env.actions.put_item(Item={"action_id": action_id, "status": "COMPLETED",
                               "claimed_by_trace_id": outcome["trace_id"]})
    assert not _harness_action_checks(env, outcome, prior)["action_matches_eligibility"]["ok"]

    assert not _harness_action_checks(env, outcome, prior, expect="required")["expected_action_mode"]["ok"]


def test_harness_action_eligible_run_requires_the_completed_action(env):
    outcome = _start_completed(env, "High")  # no prior state: new_alert
    checks = _harness_action_checks(env, outcome, None, expect="required")
    assert _all_ok(checks), checks

    # A missing action for an eligible change fails; so does demanding a skip.
    env.actions.delete_item(Key={"action_id": agent.review_ticket_action_id(outcome["thread_id"])})
    assert not _harness_action_checks(env, outcome, None)["action_matches_eligibility"]["ok"]
    assert not _harness_action_checks(env, outcome, None, expect="skipped")["expected_action_mode"]["ok"]


def test_harness_rejects_assessment_that_disagrees_with_prior_state(env):
    outcome = _start_completed(env, "High")  # assessed as new_alert against empty state
    checks = _harness_action_checks(env, outcome, {"current_risk_level": "High"})
    assert not checks["eligibility_assessed_by_b"]["ok"]


@pytest.mark.parametrize("prior_level", [None, "High"])  # action-eligible, then suppressed
def test_harness_resume_checks_accept_both_resume_shapes(env, prior_level):
    """In-process stand-in for the two processes: real outcomes, synthetic pids."""
    import time as _time
    proof = _load_proof_script()
    if prior_level:
        _seed_state(prior_level)
    saver = DynamoCheckpointSaver(CHECKPOINTS, REGION)
    store = DynamoRiskStateStore(SUPPLIER_STATE, REGION)
    traces = risk_trace_store.DynamoTraceStore(TRACES, REGION)
    before_a = proof._state_rows(store)

    t_a = _time.time()
    env.monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "proc-A")
    started = risk_workflow.start_workflow({"headline": HEADLINE}, pause_before=["validate"], **_new_executor())
    t_a_end = _time.time()
    between = {"checkpoint": proof._checkpoint_snapshot(saver, started["thread_id"]),
               "state_rows": proof._state_rows(store), "action_item": None,
               "trace_a": traces.get_by_identifier(started["trace_id"])}

    t_b = _time.time()
    env.monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "proc-B")
    resumed = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())
    after = {"checkpoint": proof._checkpoint_snapshot(saver, started["thread_id"]),
             "state_rows": proof._state_rows(store),
             "trace_b": traces.get_by_identifier(resumed["trace_id"])}

    def child(outcome, pid, t0, t1, tripwires=None):
        return {"outcome": {**outcome, "executor_id": f"proc:{pid}"}, "pid": pid, "returncode": 0,
                "alive_after_exit": False, "started_ts": t0, "ended_ts": t1,
                "started_at": proof._iso(t0), "ended_at": proof._iso(t1), "tripwire_report": tripwires}

    a = child(started, 1, t_a, t_a_end)
    b = child(resumed, 2, t_b, _time.time(),
              {"hits": [], "error": None, "aws_services": ["dynamodb"], "aws_calls": ["dynamodb:Query"]})
    checks = proof._resume_checks(HEADLINE, a, b, between, after, before_a)

    assert _all_ok(checks), {k: v for k, v in checks.items() if not v["ok"]}
    expected_tail = ["validate", "assess"] if prior_level else ["validate", "assess", "act"]
    assert checks["b_reused_checkpointed_steps"]["detail"]["b_trace_nodes"][4:] == expected_tail


# ---- Completion detection: pending node writes without a newer checkpoint ----
# In-process simulation: the saver raises on the checkpoint put that follows
# the target node's stored writes, which leaves the durable state a crash at
# that point would leave. The process-kill version is tests/test_c2b_driver.py.

class SimulatedCrash(Exception):
    pass


def _crash_after_writes_of(saver, node: str):
    """Make saver fail the checkpoint that follows `node`, once `node`'s writes are stored.

    LangGraph writes a task's pending writes and the next checkpoint from
    separate background threads, in no guaranteed order, so the checkpoint
    is recognised by its contents and the put waits for the writes first.
    """
    import threading
    original_put_writes, original_put = saver.put_writes, saver.put
    stored = threading.Event()

    def put_writes(config, writes, task_id, task_path=""):
        original_put_writes(config, writes, task_id, task_path)
        if task_path.rsplit(",", 1)[-1].strip() == node:
            stored.set()

    def put(config, checkpoint, metadata, new_versions):
        steps = checkpoint["channel_values"].get("trace_steps") or []
        if steps and steps[-1]["node_name"] == node:
            assert stored.wait(10), f"{node} writes were never stored"
            raise SimulatedCrash(f"checkpoint after {node} not written")
        return original_put(config, checkpoint, metadata, new_versions)

    saver.put_writes, saver.put = put_writes, put
    return saver


def _governed_app():
    deps = _new_executor()
    return agent.build_app(deps["checkpointer"], action_gate=deps["action_gate"], risk_reader=deps["risk_store"])


def _cfg(thread_id):
    return {"configurable": {"thread_id": thread_id}}


def test_langgraph_snapshot_semantics_relied_on_by_completion_detection(env):
    """Pins LangGraph 1.2.x: next omits tasks with stored writes; tasks lists all scheduled tasks."""
    paused = _start_paused(env)
    snap = _governed_app().get_state(_cfg(paused["thread_id"]))
    assert snap.next == ("analyze",)
    assert [(t.name, t.result is None) for t in snap.tasks] == [("analyze", True)]

    env.authority.revoke(reason="incident", actor="pytest")
    denied = risk_workflow.resume_workflow(paused["thread_id"], **_new_executor())
    snap = _governed_app().get_state(_cfg(denied["thread_id"]))
    assert denied["status"] == "authority_denied"
    assert snap.next == ("act",)
    assert [(t.name, bool(t.error)) for t in snap.tasks] == [("act", True)]

    env.authority.restore(reason="resolved", actor="pytest")
    completed = risk_workflow.resume_workflow(paused["thread_id"], **_new_executor())
    snap = _governed_app().get_state(_cfg(paused["thread_id"]))
    assert completed["status"] == "completed"
    assert (snap.next, snap.tasks) == ((), ())


def test_resume_continues_from_pending_analyze_writes_without_repeating_inference(env):
    _seed_state("High")
    started = _start_paused(env)  # pauses before analyze
    deps = _new_executor()
    _crash_after_writes_of(deps["checkpointer"], "analyze")
    with pytest.raises(SimulatedCrash):
        risk_workflow.resume_workflow(started["thread_id"], **deps)
    assert _calls(env) == (1, 1, 1)  # analyze ran once and its writes were stored

    snap = _governed_app().get_state(_cfg(started["thread_id"]))
    assert snap.next == ()  # the state the old completion check misread
    assert [(t.name, t.result is not None) for t in snap.tasks] == [("analyze", True)]

    outcome = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())

    assert outcome["status"] == "completed"
    assert (outcome["eligibility_change_type"], outcome["change_type"]) == ("suppressed", "suppressed")
    assert outcome["state_written"] is True
    assert _calls(env) == (1, 1, 1)  # no second inference, no retrieval
    trace = _trace(outcome["trace_id"])
    assert _nodes(trace) == ["infer", "decide", "retrieve", "analyze", "validate", "assess"]
    assert _state_row()["last_headline"] == HEADLINE
    assert _governed_app().get_state(_cfg(started["thread_id"])).tasks == ()


def test_resume_reuses_completed_action_from_pending_act_writes_and_finalizes(env):
    tickets = []
    real_ticket = agent.create_servicenow_ticket
    env.monkeypatch.setattr(agent, "create_servicenow_ticket",
                            lambda event, decision: tickets.append(1) or real_ticket(event, decision))
    deps = _new_executor()
    _crash_after_writes_of(deps["checkpointer"], "act")
    with pytest.raises(SimulatedCrash):
        risk_workflow.start_workflow({"headline": HEADLINE, "alert_id": "alert-1"}, **deps)
    [action] = env.actions.scan()["Items"]
    assert action["status"] == "COMPLETED" and len(tickets) == 1
    assert env.supplier_state.scan()["Items"] == []  # finalization never ran
    thread_id = action["thread_id"]
    snap = _governed_app().get_state(_cfg(thread_id))
    assert snap.next == () and [(t.name, t.result is not None) for t in snap.tasks] == [("act", True)]

    executes = []
    resume_deps = _new_executor()
    gate = resume_deps["action_gate"]
    original_execute = gate.execute
    gate.execute = lambda *a, **k: executes.append(1) or original_execute(*a, **k)
    outcome = risk_workflow.resume_workflow(thread_id, **resume_deps)

    assert outcome["status"] == "completed"
    assert executes == [] and len(tickets) == 1  # act did not run again
    assert (outcome["action_id"], outcome["action_status"]) == (action["action_id"], "COMPLETED")
    assert (outcome["eligibility_change_type"], outcome["change_type"]) == ("new_alert", "new_alert")
    assert outcome["state_written"] is True
    assert _state_row()["current_risk_level"] == "High"
    trace = _trace(outcome["trace_id"])
    assert trace["workflow_event"] == "resumed_completed"
    assert _nodes(trace)[-2:] == ["assess", "act"]
    assert risk_workflow.resume_workflow(thread_id, **_new_executor())["status"] == "already_completed"


def test_truly_completed_thread_is_still_already_completed(env):
    completed = _start_completed(env, "High")
    assert _governed_app().get_state(_cfg(completed["thread_id"])).tasks == ()

    again = risk_workflow.resume_workflow(completed["thread_id"], **_new_executor())

    assert again["status"] == "already_completed"
    assert again["trace_id"] == completed["trace_id"]
    assert len(env.actions.scan()["Items"]) == 1
