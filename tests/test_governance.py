"""Authority store, transactional action ledger, fail-closed configuration,
and the governed act node's boundaries (moto)."""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
from app.governance import authority_cli  # noqa: E402
from app.governance.action_ledger import (  # noqa: E402
    ActionInFlight,
    ActionLedger,
    UnconfiguredActionGate,
    action_gate_from_env,
)
from app.governance.authority import (  # noqa: E402
    AGENT_SCOPE,
    AuthorityDenied,
    AuthorityStore,
)
from app.storage.supplier_risk_state_store import DynamoRiskStateStore  # noqa: E402
from app.workflows import risk_workflow  # noqa: E402
from app.workflows import supplier_risk_agent as agent  # noqa: E402
from tests.test_risk_workflow import (  # noqa: E402,F401  (env is a pytest fixture)
    ACTIONS,
    AUTHORITY,
    HEADLINE,
    MOTO_AVAILABLE,
    REGION,
    SUPPLIER_STATE,
    _new_executor,
    _trace,
    env,
)

pytestmark = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")

ACTION_ID = "action#thread-" + "c" * 32 + "#open_review_ticket"


def _context(trace_id: str = "trace-1") -> dict:
    return {
        "action_type": "open_review_ticket",
        "thread_id": "thread-" + "c" * 32,
        "trace_id": trace_id,
        "executor_id": "host:1",
        "subject": {"supplier": "TSMC"},
    }


class Effect:
    def __init__(self):
        self.calls = 0

    def __call__(self) -> dict:
        self.calls += 1
        return {"ticket_id": f"MOCK-{self.calls}"}


@pytest.fixture
def ledger(env):
    return ActionLedger(ACTIONS, AuthorityStore(AUTHORITY, REGION), REGION)


# ---- Authority store ----

def test_missing_authority_is_reported_as_missing(env):
    store = AuthorityStore(AUTHORITY, REGION)
    assert store.get("agent#other")["status"] == "MISSING"
    assert store.get("agent#other")["version"] == 0


def test_restore_and_revoke_increment_version(env):
    assert env.authority.get() == {"scope": AGENT_SCOPE, "status": "ACTIVE", "version": 1, "reason": "test grant"}

    revoked = env.authority.revoke(reason="incident 42", actor="operator-a")
    assert (revoked["status"], revoked["version"], revoked["reason"]) == ("REVOKED", 2, "incident 42")
    restored = env.authority.restore(reason="resolved", actor="operator-a")
    assert (restored["status"], restored["version"]) == ("ACTIVE", 3)


def test_revoke_never_creates_an_item(env):
    with pytest.raises(LookupError):
        env.authority.revoke("agent#unknown", reason="x", actor="op")
    assert env.authority.get("agent#unknown")["status"] == "MISSING"


# ---- Action ledger ----

def test_admitted_action_runs_once_and_replays(env, ledger):
    effect = Effect()
    first = ledger.execute(ACTION_ID, scope=AGENT_SCOPE, context=_context(), effect=effect)
    again = ledger.execute(ACTION_ID, scope=AGENT_SCOPE, context=_context("trace-2"), effect=effect)

    assert first["status"] == "COMPLETED" and first["replayed"] is False
    assert first["authority_version"] == 1
    assert first["effect"] == {"ticket_id": "MOCK-1"}
    assert again["replayed"] is True and again["effect"] == {"ticket_id": "MOCK-1"}
    assert effect.calls == 1
    item = ledger.get(ACTION_ID)
    assert item["attempt_count"] == 1
    assert item["claimed_by_trace_id"] == "trace-1"


def test_revoked_authority_denies_without_effect_then_restore_admits(env, ledger):
    effect = Effect()
    env.authority.revoke(reason="stop", actor="op")

    with pytest.raises(AuthorityDenied) as denied:
        ledger.execute(ACTION_ID, scope=AGENT_SCOPE, context=_context(), effect=effect)
    assert (denied.value.status, denied.value.version, denied.value.action_id) == ("REVOKED", 2, ACTION_ID)
    assert effect.calls == 0
    item = ledger.get(ACTION_ID)
    assert item["status"] == "DENIED"
    assert item["denied_authority_version"] == 2
    assert "effect" not in item

    env.authority.restore(reason="ok", actor="op")
    result = ledger.execute(ACTION_ID, scope=AGENT_SCOPE, context=_context("trace-2"), effect=effect)
    assert result["status"] == "COMPLETED"
    assert result["authority_version"] == 3
    assert effect.calls == 1
    item = ledger.get(ACTION_ID)
    assert (item["denied_count"], item["attempt_count"]) == (1, 1)


def test_missing_authority_item_denies(env):
    ledger = ActionLedger(ACTIONS, AuthorityStore(AUTHORITY, REGION), REGION)
    with pytest.raises(AuthorityDenied) as denied:
        ledger.execute(ACTION_ID, scope="agent#never-granted", context=_context(), effect=Effect())
    assert denied.value.status == "MISSING"


def test_revocation_after_read_is_caught_by_transaction(env, ledger, monkeypatch):
    """Check-then-claim race: authority read as ACTIVE v1, revoked before the
    transaction. The ConditionCheck refuses the claim; no effect runs."""
    effect = Effect()
    stale = env.authority.get()
    env.authority.revoke(reason="raced", actor="op")
    real_get = ledger.authority.get
    reads = iter([stale])
    monkeypatch.setattr(ledger.authority, "get", lambda scope=AGENT_SCOPE: next(reads, None) or real_get(scope))

    with pytest.raises(AuthorityDenied) as denied:
        ledger.execute(ACTION_ID, scope=AGENT_SCOPE, context=_context(), effect=effect)
    assert denied.value.version == 2
    assert effect.calls == 0
    assert ledger.get(ACTION_ID)["status"] == "DENIED"


def test_admission_is_pinned_to_the_authority_version_read(env, ledger, monkeypatch):
    """Revoke + restore between read and transaction: the stale version is
    refused, admission retries and records the current version."""
    effect = Effect()
    stale = env.authority.get()
    env.authority.revoke(reason="r", actor="op")
    env.authority.restore(reason="r", actor="op")
    real_get = ledger.authority.get
    reads = iter([stale])
    monkeypatch.setattr(ledger.authority, "get", lambda scope=AGENT_SCOPE: next(reads, None) or real_get(scope))

    result = ledger.execute(ACTION_ID, scope=AGENT_SCOPE, context=_context(), effect=effect)
    assert result["authority_version"] == 3
    assert effect.calls == 1


def test_in_flight_action_is_not_reexecuted(env, ledger):
    """A claim whose executor died before completion needs reconciliation."""
    effect = Effect()
    ledger._transact_claim(ACTION_ID, scope=AGENT_SCOPE, version=1, context=_context("dead-trace"))

    with pytest.raises(ActionInFlight, match="dead-trace"):
        ledger.execute(ACTION_ID, scope=AGENT_SCOPE, context=_context("trace-2"), effect=effect)
    assert effect.calls == 0


def test_denial_never_downgrades_a_completed_action(env, ledger):
    effect = Effect()
    ledger.execute(ACTION_ID, scope=AGENT_SCOPE, context=_context(), effect=effect)
    env.authority.revoke(reason="late", actor="op")

    # A replay after revocation returns the stored result: no new effect.
    result = ledger.execute(ACTION_ID, scope=AGENT_SCOPE, context=_context("trace-2"), effect=effect)
    assert result["replayed"] is True
    assert ledger.get(ACTION_ID)["status"] == "COMPLETED"
    assert effect.calls == 1


# ---- Fail-closed configuration ----

@pytest.mark.parametrize("missing", ["AUTHORITY_TABLE", "ACTIONS_TABLE"])
def test_gate_from_env_fails_closed(monkeypatch, missing):
    monkeypatch.setenv("AUTHORITY_TABLE", AUTHORITY)
    monkeypatch.setenv("ACTIONS_TABLE", ACTIONS)
    monkeypatch.delenv(missing)
    gate = action_gate_from_env()
    assert isinstance(gate, UnconfiguredActionGate)
    effect = Effect()
    with pytest.raises(AuthorityDenied) as denied:
        gate.execute(ACTION_ID, scope=AGENT_SCOPE, context=_context(), effect=effect)
    assert denied.value.status == "UNCONFIGURED"
    assert effect.calls == 0


def test_lifecycle_without_gate_denies_at_act(env):
    deps = _new_executor()
    started = risk_workflow.start_workflow({"headline": HEADLINE}, pause_before=["analyze"], **deps)

    ungoverned = {k: v for k, v in _new_executor().items() if k != "action_gate"}
    outcome = risk_workflow.resume_workflow(started["thread_id"], **ungoverned)

    assert outcome["status"] == "authority_denied"
    assert outcome["next"] == ["act"]
    assert outcome["state_written"] is False
    assert env.supplier_state.scan()["Items"] == []
    assert env.actions.scan()["Items"] == []
    trace = _trace(outcome["trace_id"])
    assert trace["workflow_event"] == "authority_denied"
    assert trace["authority_status"] == "UNCONFIGURED"


def test_inconclusive_alert_completes_without_action(env):
    env.risk.response = json.dumps({"status": "inconclusive"})
    outcome = risk_workflow.start_workflow({"headline": HEADLINE}, **_new_executor())

    assert outcome["status"] == "completed"
    assert outcome["action_id"] is None
    assert env.actions.scan()["Items"] == []


# ---- Phase 2 path has no privileged action ----

def test_phase2_graph_has_no_act_node():
    assert "act" not in agent.app.get_graph().nodes
    assert "act" in agent.build_app(action_gate=UnconfiguredActionGate()).get_graph().nodes


def test_phase2_process_alert_row_takes_no_action(env):
    env.authority.revoke(reason="revoked", actor="op")
    enriched = agent.process_alert_row(
        {"headline": HEADLINE, "alert_id": "alert-p2"}, DynamoRiskStateStore(SUPPLIER_STATE, REGION)
    )
    assert enriched["change_type"] == "new_alert"
    assert env.actions.scan()["Items"] == []
    trace = _trace(enriched["trace_id"])
    assert trace["action_id"] is None and trace["authority_status"] is None


# ---- Operator CLI ----

def test_authority_cli(env, capsys):
    assert authority_cli.main(["revoke", "--reason", "incident", "--actor", "op-1"]) == 0
    assert authority_cli.main(["show"]) == 0
    assert authority_cli.main(["restore", "--reason", "clear", "--actor", "op-1"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(l["command"], l["status"], l["version"]) for l in lines] == [
        ("revoke", "REVOKED", 2), ("show", "REVOKED", 2), ("restore", "ACTIVE", 3),
    ]


def test_authority_cli_requires_table(monkeypatch):
    monkeypatch.delenv("AUTHORITY_TABLE", raising=False)
    with pytest.raises(RuntimeError, match="AUTHORITY_TABLE"):
        authority_cli.main(["show"])
