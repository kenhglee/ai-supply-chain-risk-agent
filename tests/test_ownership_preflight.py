"""The read-only rollout preflight against moto: thread classification and the read-only guard."""
import importlib.util
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from app.governance.action_ledger import ActionInFlight  # noqa: E402
from app.workflows import risk_workflow  # noqa: E402
from app.workflows import supplier_risk_agent as agent  # noqa: E402
from tests.test_risk_workflow import (  # noqa: E402,F401  (env is a fixture)
    ACTIONS,
    CHECKPOINTS,
    HEADLINE,
    MOTO_AVAILABLE,
    REGION,
    TRACES,
    _new_executor,
    _unfenced_executor,
    env,
)

pytestmark = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")


def _preflight():
    spec = importlib.util.spec_from_file_location("ownership_preflight",
                                                  os.path.join(PROJECT_ROOT, "scripts", "ownership_preflight.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tables(env):
    env.monkeypatch.setenv("CHECKPOINT_TABLE", CHECKPOINTS)
    env.monkeypatch.setenv("ACTIONS_TABLE", ACTIONS)
    env.monkeypatch.setenv("RISK_TRACES_TABLE", TRACES)
    return env


def _legacy_start(**kwargs):
    return risk_workflow.start_workflow({"headline": HEADLINE, "alert_id": "a"}, **_unfenced_executor(), **kwargs)


def test_preflight_classifies_threads_without_writing_anything(tables):
    # First, against empty supplier state, so it assesses new_alert and reaches act.
    in_flight = _legacy_start(pause_before=["analyze"])["thread_id"]
    tables.actions.put_item(Item={"action_id": agent.review_ticket_action_id(in_flight), "status": "IN_FLIGHT"})
    with pytest.raises(ActionInFlight):  # the unfenced baseline raises; the thread stays parked at act
        risk_workflow.resume_workflow(in_flight, **_unfenced_executor())
    paused = _legacy_start(pause_before=["analyze"])["thread_id"]
    completed = _legacy_start()["thread_id"]
    no_trace = _legacy_start()
    tables.traces.delete_item(Key={"trace_id": no_trace["trace_id"]})
    owned = risk_workflow.start_workflow({"headline": HEADLINE}, pause_before=["analyze"], **_new_executor())

    preflight = _preflight()
    before = {name: getattr(tables, name).scan()["Items"] for name in ("checkpoints", "actions", "supplier_state",
                                                                        "traces")}
    calls = []
    preflight.enforce_read_only(calls, setattr_=tables.monkeypatch.setattr)
    by_thread = {t["thread_id"]: t for t in preflight.classify_threads(REGION)}

    assert by_thread[paused]["classification"] == "adopt_paused"
    assert by_thread[completed]["classification"] == "legacy_recognized"
    assert by_thread[no_trace["thread_id"]]["classification"] == "completion_unverified"
    assert by_thread[no_trace["thread_id"]]["evidence_reasons"] == ["completion_trace_missing"]
    assert by_thread[in_flight]["classification"] == "action_in_flight"
    assert (by_thread[owned["thread_id"]]["classification"], by_thread[owned["thread_id"]]["lease_epoch"]) == (
        "owned", 1)
    after = {name: getattr(tables, name).scan()["Items"] for name in before}
    assert after == before  # nothing written: no lease, marker, trace or state
    assert all(op.split(":")[1].startswith(preflight.READ_PREFIXES) for op in calls)


def test_preflight_guard_refuses_writes(tables):
    preflight = _preflight()
    preflight.enforce_read_only(setattr_=tables.monkeypatch.setattr)
    with pytest.raises(preflight.ReadOnlyViolation, match="PutItem"):
        tables.checkpoints.put_item(Item={"thread_id": "t", "sk": "x"})
    with pytest.raises(preflight.ReadOnlyViolation, match="UpdateItem"):
        tables.checkpoints.update_item(Key={"thread_id": "t", "sk": "lease"}, UpdateExpression="SET a = :a",
                                       ExpressionAttributeValues={":a": 1})
