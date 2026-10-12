"""Phase 3F policy-as-code: schema, hashing, broadening, tighten-first planning,
materialization (moto DynamoDB + IAM), atomic admission precedence, CLI and
the probe's act command. No live AWS."""
import io
import json
import os
import sys
from types import SimpleNamespace

import boto3
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
from app.governance import policy as pol, policy_cli  # noqa: E402
from app.governance.action_ledger import ActionLedger  # noqa: E402
from app.governance.authority import AGENT_SCOPE, AuthorityDenied, AuthorityStore  # noqa: E402
from app.workflows import risk_workflow  # noqa: E402
from tests.test_bedrock_authority_probe import probe  # noqa: E402
from tests.test_risk_workflow import (  # noqa: E402,F401  (env is a pytest fixture)
    ACTIONS,
    AUTHORITY,
    HEADLINE,
    MOTO_AVAILABLE,
    REGION,
    _new_executor,
    _trace,
    env,
)

FIXTURES = os.path.join(PROJECT_ROOT, "policies")
SUBJECT = "agent/supplier-risk-agent"
ACCOUNT = "123456789012"
POLICY_KEY = "policy#supplier-risk-agent#open_review_ticket"
EMERGENCY_DENY = "RiskAgentBedrockClaudeDeny"


def doc(version, capabilities, subject=SUBJECT) -> str:
    return json.dumps({"policy_version": version, "subject": subject, "capabilities": capabilities})


def P(version, **capabilities):
    return pol.parse_policy(doc(version, capabilities))


def fixture(name):
    return os.path.join(FIXTURES, f"supplier-risk-agent.{name}.json")


# ---- Schema, validation, hashing (no AWS) ----

@pytest.mark.parametrize("name", ["v1", "v2", "v3"])
def test_fixtures_are_valid(name):
    policy = pol.load_policy(fixture(name))
    assert policy.version == int(name[1:])
    assert policy.sha256.startswith("sha256:")


@pytest.mark.parametrize("text, message", [
    ("not json", "not JSON"),
    ("[1, 2]", "top level"),
    (json.dumps({"policy_version": 1, "subject": SUBJECT}), "keys must be exactly"),
    (json.dumps({"policy_version": 1, "subject": SUBJECT, "capabilities": {}, "extra": 1}), "keys must be exactly"),
    (doc(0, {}), "positive integer"),
    (doc(-1, {}), "positive integer"),
    (doc(True, {}), "positive integer"),
    (doc("2", {}), "positive integer"),
    (doc(1, {}, subject="agent/someone-else"), "unknown subject"),
    (doc(1, {"delete_everything": "allow"}), "unknown capability"),
    (doc(1, {"open_review_ticket": "ALLOW"}), "unsupported effect"),
    (doc(1, {"open_review_ticket": "maybe"}), "unsupported effect"),
    (doc(1, {"open_review_ticket": None}), "unsupported effect"),
    (json.dumps({"policy_version": 1, "subject": SUBJECT, "capabilities": ["open_review_ticket"]}), "must be an object"),
    (doc(1, {"titan_embeddings": "allow"}), "beyond the capability ceiling"),
])
def test_invalid_policies_are_rejected(text, message):
    with pytest.raises(pol.PolicyError, match=message):
        pol.parse_policy(text)


def test_invalid_ceiling_fixture_is_rejected():
    with pytest.raises(pol.PolicyError, match="ceiling"):
        pol.load_policy(fixture("invalid-ceiling"))


def test_unavailable_source_is_an_error(tmp_path):
    with pytest.raises(pol.PolicyError, match="unavailable"):
        pol.load_policy(str(tmp_path / "missing.json"))


def test_absent_capability_is_denied_and_explicit_deny_of_out_of_ceiling_is_fine():
    policy = P(1, open_review_ticket="allow", titan_embeddings="deny")
    assert policy.effect("claude_inference") == "deny"
    assert policy.raw("claude_inference") == "absent"
    assert policy.effect("titan_embeddings") == "deny"


def test_hash_is_deterministic_over_meaning():
    a = pol.parse_policy('{"subject": "agent/supplier-risk-agent", "capabilities": '
                         '{"claude_inference": "allow", "open_review_ticket": "deny"}, "policy_version": 3}')
    b = pol.parse_policy(json.dumps({"policy_version": 3, "subject": SUBJECT, "capabilities":
                                     {"open_review_ticket": "deny", "claude_inference": "allow"}}, indent=4))
    assert a.sha256 == b.sha256
    assert P(4, open_review_ticket="deny", claude_inference="allow").sha256 != a.sha256
    assert P(3, open_review_ticket="allow", claude_inference="allow").sha256 != a.sha256


# ---- Monotonic versions ----

@pytest.mark.parametrize("new_version", [2, 1])
def test_version_must_increase(new_version):
    with pytest.raises(pol.PolicyError, match="must increase"):
        pol.plan(P(2, open_review_ticket="deny"), P(new_version, open_review_ticket="deny"))


# ---- Broadening (per capability) ----

def _broadening(current, new):
    return pol.plan(current, new, approve_broadening=True)["broadening"]


def test_deny_to_allow_is_broadening():
    assert _broadening(P(1, open_review_ticket="deny"), P(2, open_review_ticket="allow")) == ["open_review_ticket"]


def test_absent_to_allow_is_broadening_including_new_capability():
    assert _broadening(P(1, open_review_ticket="allow"),
                       P(2, open_review_ticket="allow", claude_inference="allow")) == ["claude_inference"]
    assert _broadening(None, P(1, open_review_ticket="allow")) == ["open_review_ticket"]


def test_allow_to_deny_and_allow_to_absent_are_not_broadening():
    assert _broadening(P(1, open_review_ticket="allow", claude_inference="allow"),
                       P(2, open_review_ticket="deny")) == []


def test_swap_detects_newly_allowed_capability():
    """Same number of allowed capabilities, but claude_inference is newly allowed."""
    the_plan = pol.plan(P(1, open_review_ticket="allow", claude_inference="deny"),
                        P(2, open_review_ticket="deny", claude_inference="allow"))
    assert the_plan["broadening"] == ["claude_inference"]
    assert the_plan["requires_approval"] is True
    assert the_plan["approved"] is False


def test_deny_to_absent_is_a_change_but_not_broadening():
    the_plan = pol.plan(P(1, open_review_ticket="deny"), P(2))
    assert [(c["capability"], c["before"], c["after"], c["broadening"]) for c in the_plan["changes"]] == [
        ("open_review_ticket", "deny", "absent", False)]
    assert the_plan["requires_approval"] is False


# ---- Tighten-first planning ----

def _phases(the_plan):
    return [(s["phase"], s["capability"], s["operation"]) for s in the_plan["steps"]]


def test_multi_boundary_tightening_comes_first():
    """v3 -> v4 tightens IAM (claude) and broadens application (ticket):
    the IAM tightening is planned before the application broadening."""
    the_plan = pol.plan(P(3, open_review_ticket="deny", claude_inference="allow"),
                        P(4, open_review_ticket="allow", claude_inference="deny"), approve_broadening=True)
    assert _phases(the_plan) == [
        ("tighten", "claude_inference", "attach_deny"),
        ("restamp", "titan_embeddings", "none"),
        ("broaden", "open_review_ticket", "put_policy_item"),
    ]
    phases = [s["phase"] for s in the_plan["steps"]]
    assert phases == sorted(phases, key=["tighten", "restamp", "broaden"].index)


def test_full_revocation_plans_only_tightenings_across_both_boundaries():
    the_plan = pol.plan(P(1, open_review_ticket="allow", claude_inference="allow"),
                        P(2, open_review_ticket="deny", claude_inference="deny"))
    assert _phases(the_plan) == [
        ("tighten", "claude_inference", "attach_deny"),
        ("tighten", "open_review_ticket", "put_policy_item"),
        ("restamp", "titan_embeddings", "none"),
    ]
    assert the_plan["requires_approval"] is False


def test_unchanged_application_capability_is_restamped_with_new_version():
    the_plan = pol.plan(P(2, open_review_ticket="deny", claude_inference="deny"),
                        P(3, open_review_ticket="deny", claude_inference="allow"), approve_broadening=True)
    assert ("restamp", "open_review_ticket", "put_policy_item") in _phases(the_plan)
    assert ("broaden", "claude_inference", "detach_deny") in _phases(the_plan)


# ---- Materialization against moto DynamoDB + IAM ----

@pytest.fixture
def gov(env):
    """env (authority ACTIVE) + IAM role and Deny policies, with the env
    fixture's seeded v1 materialization removed: materializer tests start empty."""
    iam_client = boto3.client("iam", region_name=REGION)
    iam_client.create_role(RoleName=pol.GOVERNED_WORKER_ROLE, AssumeRolePolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": ACCOUNT}, "Action": "sts:AssumeRole"}]}))
    deny_doc = json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Deny", "Action": "bedrock:InvokeModel", "Resource": "*"}]})
    for name in ("RiskAgentPolicyClaudeDeny", EMERGENCY_DENY):
        iam_client.create_policy(PolicyName=name, PolicyDocument=deny_doc)
    env.iam = iam_client
    env.store = pol.PolicyStore(AUTHORITY, REGION)
    for key in (POLICY_KEY, pol.pointer_key("supplier-risk-agent"), pol.materialization_key("supplier-risk-agent", 1)):
        env.store._table.delete_item(Key={"scope_id": key})
    env.layer = pol.IamPolicyLayer(iam_client, ACCOUNT)
    env.attached = lambda: sorted(p["PolicyName"] for p in iam_client.list_attached_role_policies(
        RoleName=pol.GOVERNED_WORKER_ROLE)["AttachedPolicies"])
    env.item = lambda: env.store.get(POLICY_KEY)
    return env


def _materialize(gov, policy, approve=False):
    return pol.materialize(policy, store=gov.store, iam=gov.layer, actor="pytest", approve_broadening=approve)


def test_first_materialization_requires_approval_and_writes_nothing(gov):
    with pytest.raises(pol.BroadeningNotApproved):
        _materialize(gov, pol.load_policy(fixture("v1")))
    assert gov.store.latest_completed("supplier-risk-agent") is None
    assert gov.attached() == []


def test_v1_v2_v3_materialization_sequence(gov):
    v1 = _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    assert v1["status"] == "completed"
    assert (gov.item()["effect"], gov.item()["policy_version"]) == ("allow", 1)
    assert gov.attached() == []

    v2 = _materialize(gov, pol.load_policy(fixture("v2")))  # pure tightening: no approval
    assert [s["phase"] for s in v2["steps"]] == ["tighten", "tighten", "restamp"]
    assert (gov.item()["effect"], gov.item()["policy_version"]) == ("deny", 2)
    assert gov.attached() == ["RiskAgentPolicyClaudeDeny"]
    attach = next(s for s in v2["steps"] if s["operation"] == "attach_deny")
    assert attach["policy_arn"].endswith(":policy/RiskAgentPolicyClaudeDeny")

    with pytest.raises(pol.BroadeningNotApproved, match="claude_inference"):
        _materialize(gov, pol.load_policy(fixture("v3")))
    assert gov.item()["policy_version"] == 2 and gov.attached() == ["RiskAgentPolicyClaudeDeny"]

    v3 = _materialize(gov, pol.load_policy(fixture("v3")), approve=True)
    assert v3["broadening"] == ["claude_inference"] and v3["broadening_approved"] is True
    assert (gov.item()["effect"], gov.item()["policy_version"]) == ("deny", 3)
    assert gov.attached() == []
    assert gov.store.current_policy("supplier-risk-agent").sha256 == pol.load_policy(fixture("v3")).sha256


def test_materializer_never_touches_emergency_state(gov):
    gov.authority.revoke(reason="emergency", actor="operator")
    gov.iam.attach_role_policy(RoleName=pol.GOVERNED_WORKER_ROLE,
                               PolicyArn=f"arn:aws:iam::{ACCOUNT}:policy/{EMERGENCY_DENY}")
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)  # all allow
    assert gov.authority.get()["status"] == "REVOKED"
    assert gov.attached() == [EMERGENCY_DENY]


def test_stale_materialization_is_rejected(gov):
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    _materialize(gov, pol.load_policy(fixture("v2")))
    with pytest.raises(pol.PolicyError, match="must increase"):
        _materialize(gov, pol.load_policy(fixture("v2")))
    with pytest.raises(pol.PolicyError, match="must increase"):
        _materialize(gov, P(1, open_review_ticket="deny"))


def test_partial_failure_is_recorded_and_leaves_tightenings(gov, monkeypatch):
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)

    def broken_attach(cap):
        raise RuntimeError("iam unavailable")

    monkeypatch.setattr(gov.layer, "attach", broken_attach)
    with pytest.raises(RuntimeError, match="iam unavailable"):
        _materialize(gov, pol.load_policy(fixture("v2")))
    failed = gov.store._table.get_item(Key={"scope_id": pol.materialization_key("supplier-risk-agent", 2)})["Item"]
    assert failed["status"] == "failed" and "iam unavailable" in failed["error"]
    # Completed version is still v1; status reports the mismatch.
    assert gov.store.current_policy("supplier-risk-agent").version == 1


def test_same_version_with_different_content_is_refused(gov):
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    with pytest.raises(pol.PolicyError, match="different v1"):
        gov.store.put_capability(P(1, open_review_ticket="deny"), "open_review_ticket", actor="x")


def test_materialized_item_is_never_overwritten_by_older_version(gov):
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    _materialize(gov, pol.load_policy(fixture("v2")))
    with pytest.raises(pol.PolicyError, match="newer version"):
        gov.store.put_capability(P(1, open_review_ticket="allow"), "open_review_ticket", actor="x")


# ---- Atomic admission: precedence and fail-closed ----

def _ledger():
    return ActionLedger(ACTIONS, AuthorityStore(AUTHORITY, REGION), REGION)


def _ctx(trace="t-1"):
    return {"action_type": "open_review_ticket", "thread_id": "thread-" + "e" * 32,
            "trace_id": trace, "executor_id": "host:1", "subject": {}}


class Effect:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return {"ticket_id": f"MOCK-{self.calls}"}


def _try(action_id="action#p#open_review_ticket"):
    effect = Effect()
    try:
        result = _ledger().execute(action_id, scope=AGENT_SCOPE, context=_ctx(), effect=effect)
        return "ALLOW", result, effect.calls
    except AuthorityDenied as denial:
        return "DENY", denial, effect.calls


@pytest.mark.parametrize("policy_effect, emergency, expected, layer", [
    ("allow", "active", "ALLOW", None),
    ("allow", "revoked", "DENY", "emergency"),
    ("deny", "active", "DENY", "policy"),
    ("deny", "revoked", "DENY", "emergency+policy"),
])
def test_precedence_matrix(gov, policy_effect, emergency, expected, layer):
    """ceiling=allow for open_review_ticket in all rows."""
    _materialize(gov, P(2, open_review_ticket=policy_effect, claude_inference="allow"), approve=True)
    if emergency == "revoked":
        gov.authority.revoke(reason="emergency", actor="operator")
    result, detail, effect_calls = _try()
    assert result == expected
    if expected == "ALLOW":
        assert detail["status"] == "COMPLETED" and detail["policy_version"] == 2 and effect_calls == 1
        assert detail["policy_sha256"] == gov.item()["policy_sha256"]
    else:
        assert detail.layer == layer and effect_calls == 0
        item = _ledger().get("action#p#open_review_ticket")
        assert item["status"] == "DENIED" and item["denied_layer"] == layer
        assert item["denied_policy_version"] == 2
        assert item["denied_policy_status"] == ("POLICY_ALLOW" if policy_effect == "allow" else "POLICY_DENY")


def test_precedence_ceiling_deny_policy_allow_cannot_materialize(gov):
    with pytest.raises(pol.PolicyError, match="ceiling"):
        _materialize(gov, pol.parse_policy(doc(2, {"titan_embeddings": "allow"})), approve=True)
    assert gov.item()["status"] == "POLICY_MISSING"  # nothing materialized
    assert gov.store.get_pointer("supplier-risk-agent") is None


def test_missing_policy_item_denies(gov):
    gov.store._table.delete_item(Key={"scope_id": POLICY_KEY})
    result, denial, calls = _try()
    assert (result, denial.layer, denial.status, calls) == ("DENY", "policy", "POLICY_MISSING", 0)


@pytest.mark.parametrize("corrupt", [{"effect": "ALLOW"}, {"kind": "something-else"}, {"policy_sha256": ""}])
def test_malformed_policy_item_denies(gov, corrupt):
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    item = gov.store._table.get_item(Key={"scope_id": POLICY_KEY})["Item"]
    gov.store._table.put_item(Item={**item, **corrupt})
    result, denial, calls = _try()
    assert (result, denial.status, calls) == ("DENY", "POLICY_INVALID", 0)


def test_policy_change_between_read_and_claim_is_caught_by_transaction(gov, monkeypatch):
    """Admission read v1 (allow); policy materialized to v2 (deny) before the
    transaction: the policy ConditionCheck cancels the claim."""
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    ledger = _ledger()
    stale = ledger.policies.get(POLICY_KEY)
    _materialize(gov, P(2, open_review_ticket="deny"))
    real_get = ledger.policies.get
    reads = iter([stale])
    monkeypatch.setattr(ledger.policies, "get", lambda key: next(reads, None) or real_get(key))
    effect = Effect()
    with pytest.raises(AuthorityDenied) as denial:
        ledger.execute("action#race#open_review_ticket", scope=AGENT_SCOPE, context=_ctx(), effect=effect)
    assert (denial.value.layer, denial.value.policy_version, effect.calls) == ("policy", 2, 0)


def test_policy_restamp_between_read_and_claim_retries_at_new_version(gov, monkeypatch):
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    ledger = _ledger()
    stale = ledger.policies.get(POLICY_KEY)
    _materialize(gov, P(2, open_review_ticket="allow"), approve=True)  # same effect, new version/hash
    real_get = ledger.policies.get
    reads = iter([stale])
    monkeypatch.setattr(ledger.policies, "get", lambda key: next(reads, None) or real_get(key))
    result = ledger.execute("action#restamp#open_review_ticket", scope=AGENT_SCOPE, context=_ctx(), effect=Effect())
    assert (result["status"], result["policy_version"]) == ("COMPLETED", 2)


def test_unavailable_policy_state_fails_closed(gov):
    ledger = ActionLedger(ACTIONS, AuthorityStore(AUTHORITY, REGION), REGION,
                          policies=pol.PolicyStore("no_such_table", REGION))
    effect = Effect()
    with pytest.raises(Exception):
        ledger.execute("action#x#open_review_ticket", scope=AGENT_SCOPE, context=_ctx(), effect=effect)
    assert effect.calls == 0


def test_enforcement_uses_materialized_state_not_the_policy_file(gov, tmp_path):
    source = tmp_path / "policy.json"
    source.write_text(doc(2, {"open_review_ticket": "allow"}))
    _materialize(gov, pol.load_policy(str(source)), approve=True)
    source.write_text(doc(3, {"open_review_ticket": "deny"}))  # edited, never materialized
    result, detail, _ = _try()
    assert result == "ALLOW" and detail["policy_version"] == 2
    source.unlink()  # source unavailable: still no runtime effect
    assert _try("action#second#open_review_ticket")[0] == "ALLOW"


def test_workflow_denial_and_completion_traces_carry_policy_evidence(gov):
    started = risk_workflow.start_workflow({"headline": HEADLINE}, pause_before=["analyze"], **_new_executor())
    _materialize(gov, P(2, open_review_ticket="deny"))
    denied = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())
    assert denied["status"] == "authority_denied" and denied["next"] == ["act"]
    trace = _trace(denied["trace_id"])
    assert (trace["denied_layer"], trace["authority_status"], trace["policy_version"]) == ("policy", "POLICY_DENY", 2)
    assert trace["authority_version"] == 1  # emergency item untouched

    _materialize(gov, P(3, open_review_ticket="allow"), approve=True)
    completed = risk_workflow.resume_workflow(started["thread_id"], **_new_executor())
    assert completed["status"] == "completed" and completed["action_status"] == "COMPLETED"
    trace = _trace(completed["trace_id"])
    assert trace["policy_version"] == 3 and trace["policy_sha256"] == gov.item()["policy_sha256"]


# ---- CLI ----

@pytest.fixture
def cli(gov, monkeypatch):
    monkeypatch.setattr(policy_cli, "build_backends", lambda: (gov.store, gov.layer))
    return gov


def _cli(capsys, *argv):
    code = policy_cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_cli_validate(capsys):
    assert _cli(capsys, "validate", fixture("v1"))[0] == policy_cli.EXIT_OK
    code, out = _cli(capsys, "validate", fixture("invalid-ceiling"))
    assert code == policy_cli.EXIT_INVALID and "ceiling" in out["error"]


def test_cli_plan_apply_status(cli, capsys):
    code, out = _cli(capsys, "plan", fixture("v1"))
    assert code == 0 and out["requires_approval"] is True and out["approved"] is False
    code, out = _cli(capsys, "apply", fixture("v1"), "--actor", "pytest")
    assert code == policy_cli.EXIT_NEEDS_APPROVAL and out["applied"] is False
    code, out = _cli(capsys, "apply", fixture("v1"), "--approve-broadening", "--actor", "pytest")
    assert code == 0 and out["status"] == "completed"
    code, out = _cli(capsys, "apply", fixture("v2"), "--actor", "pytest")
    assert code == 0
    code, out = _cli(capsys, "status")
    assert out["materialized_version"] == 2 and out["mismatches"] == []
    assert out["capabilities"]["claude_inference"]["observed"]["deny_attached"] is True
    code, out = _cli(capsys, "apply", fixture("v2"), "--actor", "pytest")
    assert code == policy_cli.EXIT_INVALID and "must increase" in out["error"]
    code, out = _cli(capsys, "apply", fixture("invalid-ceiling"), "--approve-broadening")
    assert code == policy_cli.EXIT_INVALID


def test_cli_status_reports_drift(cli, capsys):
    _cli(capsys, "apply", fixture("v1"), "--approve-broadening", "--actor", "pytest")
    cli.iam.attach_role_policy(RoleName=pol.GOVERNED_WORKER_ROLE,
                               PolicyArn=f"arn:aws:iam::{ACCOUNT}:policy/RiskAgentPolicyClaudeDeny")
    _, out = _cli(capsys, "status")
    assert out["mismatches"] == ["claude_inference"]


# ---- Probe act command (no Converse) ----

def test_probe_act_follows_materialized_policy_without_bedrock_calls(gov):
    from botocore.stub import Stubber
    session = boto3.Session(region_name=REGION)
    bedrock = session.client("bedrock-runtime", config=probe.CLIENT_CONFIG)
    sts = session.client("sts", config=probe.CLIENT_CONFIG)
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)

    def run(commands):
        out = io.StringIO()
        with Stubber(bedrock) as bedrock_stub:  # no Converse responses queued
            assert probe.run(commands, bedrock=bedrock, sts=sts, fingerprint=lambda: "sha256:test",
                             model_id=probe.DEFAULT_MODEL_ID, region=REGION, out=out, gate=_ledger()) == 0
            bedrock_stub.assert_no_pending_responses()
        return [json.loads(line) for line in out.getvalue().splitlines()]

    records = run([json.dumps({"act": "v1"})])
    act = records[1]
    assert act["event"] == "act" and act["outcome"] == "ok" and act["action_status"] == "COMPLETED"
    assert act["policy_version"] == 1 and act["ticket_id"].startswith("MOCK-CHG-")
    assert records[-1]["acts"] == 1 and records[-1]["probes"] == 0

    _materialize(gov, pol.load_policy(fixture("v2")))
    act = run([json.dumps({"act": "v2"})])[1]
    assert (act["outcome"], act["denied_layer"], act["policy_status"], act["policy_version"]) == (
        "denied", "policy", "POLICY_DENY", 2)


def test_probe_act_without_governance_config_fails_closed(monkeypatch):
    monkeypatch.delenv("AUTHORITY_TABLE", raising=False)
    monkeypatch.delenv("ACTIONS_TABLE", raising=False)
    act = probe.act_once(probe.build_gate_from_env(), action_id="action#x#open_review_ticket",
                         thread_id="probe-x", executor_id="host:1")
    assert (act["outcome"], act["denial_status"]) == ("denied", "UNCONFIGURED")


# ---- Latest-completed pointer (GetItem only; no Scan) ----

AGENT = "supplier-risk-agent"


def _pointer(gov):
    return gov.store.get_pointer(AGENT)


@pytest.fixture
def no_scan(gov, monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("dynamodb Scan must never be used by the materializer")
    monkeypatch.setattr(gov.store._table, "scan", forbidden)
    return gov


def test_first_completed_materialization_creates_pointer(no_scan):
    gov = no_scan
    v1 = pol.load_policy(fixture("v1"))
    _materialize(gov, v1, approve=True)
    assert _pointer(gov) == {"policy_version": 1, "policy_sha256": v1.sha256,
                             "materialization_key": pol.materialization_key(AGENT, 1)}


def test_v2_advances_v1_and_delayed_v1_cannot_overwrite(no_scan):
    gov = no_scan
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    v1_record = gov.store.get_record(AGENT, 1)
    _materialize(gov, pol.load_policy(fixture("v2")))
    assert _pointer(gov)["policy_version"] == 2
    with pytest.raises(pol.PolicyError, match="already v2"):
        gov.store.advance_pointer(v1_record)  # a delayed v1 completion
    assert _pointer(gov)["policy_version"] == 2


def test_pointer_retry_same_version_and_hash_is_idempotent(no_scan):
    gov = no_scan
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    record = gov.store.get_record(AGENT, 1)
    gov.store.advance_pointer(record)
    gov.store.advance_pointer(record)
    assert _pointer(gov)["policy_version"] == 1


def test_pointer_same_version_different_hash_fails(no_scan):
    gov = no_scan
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    forged = {**gov.store.get_record(AGENT, 1), "policy_sha256": "sha256:" + "0" * 64}
    with pytest.raises(pol.PolicyError, match="different hash"):
        gov.store.advance_pointer(forged)


def test_only_completed_records_may_advance_the_pointer(no_scan):
    gov = no_scan
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    started = {**gov.store.get_record(AGENT, 1), "status": "started"}
    with pytest.raises(pol.PolicyError, match="only a completed"):
        gov.store.advance_pointer(started)


def test_failed_materialization_does_not_advance_pointer(no_scan, monkeypatch):
    gov = no_scan
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    monkeypatch.setattr(gov.layer, "attach", lambda cap: (_ for _ in ()).throw(RuntimeError("iam down")))
    with pytest.raises(RuntimeError):
        _materialize(gov, pol.load_policy(fixture("v2")))
    assert _pointer(gov)["policy_version"] == 1
    assert gov.store.get_record(AGENT, 2)["status"] == "failed"
    assert gov.store.current_policy(AGENT).version == 1  # failed attempt is not the baseline


def test_missing_pointer_with_no_materialized_state_is_a_clean_start(no_scan):
    assert no_scan.store.latest_completed(AGENT) is None


def test_missing_pointer_next_to_materialized_items_fails_closed(no_scan):
    gov = no_scan
    gov.store.put_capability(P(1, open_review_ticket="allow"), "open_review_ticket", actor="x")  # no record/pointer
    with pytest.raises(pol.PolicyError, match="no latest pointer"):
        gov.store.latest_completed(AGENT)
    with pytest.raises(pol.PolicyError):
        _materialize(gov, pol.load_policy(fixture("v2")))


@pytest.mark.parametrize("corrupt", [
    {"kind": "something-else"},
    {"policy_sha256": "not-a-hash"},
    {"policy_version": None},
    {"materialization_key": "policymat#supplier-risk-agent#v0000000099"},
])
def test_malformed_pointer_fails_closed(no_scan, corrupt):
    gov = no_scan
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    key = pol.pointer_key(AGENT)
    item = gov.store._table.get_item(Key={"scope_id": key})["Item"]
    gov.store._table.put_item(Item={**item, **corrupt})
    with pytest.raises(pol.PolicyError, match="malformed"):
        gov.store.latest_completed(AGENT)
    with pytest.raises(pol.PolicyError):
        _materialize(gov, pol.load_policy(fixture("v2")))


@pytest.mark.parametrize("record_state", ["missing", "started", "failed", "hash_mismatch"])
def test_pointer_referencing_unusable_record_fails_closed(no_scan, record_state):
    gov = no_scan
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    key = pol.materialization_key(AGENT, 1)
    if record_state == "missing":
        gov.store._table.delete_item(Key={"scope_id": key})
    else:
        record = gov.store.get_record(AGENT, 1)
        change = {"policy_sha256": "sha256:" + "f" * 64} if record_state == "hash_mismatch" else {"status": record_state}
        gov.store._table.put_item(Item={**record, **change})
    with pytest.raises(pol.PolicyError):
        gov.store.latest_completed(AGENT)


def _crash_before_pointer(gov, monkeypatch, policy, approve=False):
    """COMPLETED record written, then the process dies before the pointer update."""
    real = gov.store.advance_pointer

    def crash(record):
        raise SystemExit("crashed after COMPLETED, before pointer update")

    monkeypatch.setattr(gov.store, "advance_pointer", crash)
    with pytest.raises(SystemExit):
        _materialize(gov, policy, approve=approve)
    monkeypatch.setattr(gov.store, "advance_pointer", real)


def test_crash_between_completed_and_pointer_is_detected_and_repaired(no_scan, monkeypatch, capsys):
    gov = no_scan
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    v2 = pol.load_policy(fixture("v2"))
    _crash_before_pointer(gov, monkeypatch, v2)
    assert gov.store.get_record(AGENT, 2)["status"] == "completed"
    assert _pointer(gov)["policy_version"] == 1  # behind

    # Detected, never silently treated as the baseline.
    with pytest.raises(pol.PointerBehind) as behind:
        gov.store.latest_completed(AGENT)
    assert (behind.value.version, behind.value.pointer_version) == (2, 1)
    report = policy_cli.status(gov.store, gov.layer)
    assert report["pointer_state"] == "pointer_behind" and "latest_pointer" in report["mismatches"]

    # A different version may not proceed on the stale baseline.
    with pytest.raises(pol.PointerBehind):
        _materialize(gov, pol.load_policy(fixture("v3")), approve=True)

    # Repair advances the pointer without re-running steps.
    assert gov.store.repair_pointer(AGENT) == {"repaired": True, "from_version": 1, "to_version": 2}
    assert _pointer(gov)["policy_version"] == 2
    assert gov.store.current_policy(AGENT).sha256 == v2.sha256
    assert policy_cli.status(gov.store, gov.layer)["pointer_state"] == "consistent"
    assert gov.store.repair_pointer(AGENT)["repaired"] is False


def test_crash_recovery_by_reapplying_same_version(no_scan, monkeypatch):
    gov = no_scan
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    v2 = pol.load_policy(fixture("v2"))
    _crash_before_pointer(gov, monkeypatch, v2)
    record = _materialize(gov, v2)  # same version + hash: completes the pointer update
    assert record["status"] == "completed" and _pointer(gov)["policy_version"] == 2


def test_crash_on_first_materialization_is_repairable(no_scan, monkeypatch):
    gov = no_scan
    _crash_before_pointer(gov, monkeypatch, pol.load_policy(fixture("v1")), approve=True)
    assert _pointer(gov) is None
    with pytest.raises(pol.PointerBehind):
        gov.store.latest_completed(AGENT)
    assert gov.store.repair_pointer(AGENT)["to_version"] == 1


def test_cli_repair(cli, monkeypatch, capsys):
    _materialize(cli, pol.load_policy(fixture("v1")), approve=True)
    _crash_before_pointer(cli, monkeypatch, pol.load_policy(fixture("v2")))
    code, out = _cli(capsys, "repair")
    assert code == 0 and out["repaired"] is True and out["to_version"] == 2
