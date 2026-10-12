"""Derive the AWS API calls the governed act path and the policy materializer
actually make (moto, recorded at the botocore layer). These are the inputs
to the F2b IAM policies in terraform/policy_governance.tf: if the code starts
making a new call, these tests fail and the IAM policy must be revisited."""
import json
import os
import sys

import pytest
from botocore.client import BaseClient

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
from app.governance import policy as pol  # noqa: E402
from tests.test_policy import (  # noqa: E402,F401  (env, gov are pytest fixtures)
    AUTHORITY,
    ACTIONS,
    P,
    _ctx,
    _ledger,
    _materialize,
    env,
    fixture,
    gov,
)
from app.governance.authority import AGENT_SCOPE, AuthorityDenied  # noqa: E402

# IAM action for each DynamoDB API call / transaction item type.
DDB_IAM_ACTION = {
    "GetItem": "dynamodb:GetItem", "PutItem": "dynamodb:PutItem", "UpdateItem": "dynamodb:UpdateItem",
    "Scan": "dynamodb:Scan", "Query": "dynamodb:Query",
    "ConditionCheck": "dynamodb:ConditionCheckItem", "Update": "dynamodb:UpdateItem", "Put": "dynamodb:PutItem",
}


@pytest.fixture
def recorder(monkeypatch):
    calls = []
    original = BaseClient._make_api_call

    def recording(self, operation_name, params):
        service = self.meta.service_model.service_name
        if service == "dynamodb" and operation_name == "TransactWriteItems":
            for item in params["TransactItems"]:
                [(kind, body)] = item.items()
                calls.append(("dynamodb", DDB_IAM_ACTION[kind], body["TableName"], _key(body["Key"])))
        elif service == "dynamodb":
            key = params.get("Key") or params.get("Item") or {}
            calls.append(("dynamodb", DDB_IAM_ACTION[operation_name], params["TableName"],
                          _key(key) if key else None))
        else:
            calls.append((service, f"{service}:{operation_name}", params.get("RoleName"), params.get("PolicyArn")))
        return original(self, operation_name, params)

    monkeypatch.setattr(BaseClient, "_make_api_call", recording)
    return calls


def _key(key: dict):
    value = next(iter(key.values()))
    return value.get("S") if isinstance(value, dict) else value


def _needs(calls, service="dynamodb"):
    return sorted({(action, table) for svc, action, table, _ in calls if svc == service})


def test_act_path_dynamodb_requirements(gov, recorder):
    """Admitted, replayed, policy-denied and emergency-denied admissions.
    Operator/materializer calls between them are excluded from the record."""
    effect = lambda: {"ticket_id": "MOCK-1"}
    calls = []

    def executor(fn):
        recorder.clear()
        try:
            fn()
        except AuthorityDenied:
            pass
        calls.extend(recorder)
        recorder.clear()

    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    run = lambda action_id, trace="t-1": _ledger().execute(action_id, scope=AGENT_SCOPE, context=_ctx(trace), effect=effect)
    executor(lambda: run("action#probe-a#open_review_ticket"))               # admitted
    executor(lambda: run("action#probe-a#open_review_ticket", "t-2"))        # replayed
    _materialize(gov, P(2, open_review_ticket="deny"))
    executor(lambda: run("action#probe-b#open_review_ticket"))               # policy denied
    gov.authority.revoke(reason="x", actor="op")
    executor(lambda: run("action#probe-c#open_review_ticket"))               # emergency + policy denied
    gov.authority.restore(reason="x", actor="op")
    _materialize(gov, P(3, open_review_ticket="allow"), approve=True)
    executor(lambda: run("action#probe-b#open_review_ticket", "t-3"))        # DENIED -> admitted

    assert _needs(calls) == [
        ("dynamodb:ConditionCheckItem", AUTHORITY),
        ("dynamodb:GetItem", ACTIONS),
        ("dynamodb:GetItem", AUTHORITY),
        ("dynamodb:UpdateItem", ACTIONS),
    ]
    authority_keys = {key for _, _, table, key in calls if table == AUTHORITY}
    assert authority_keys == {"agent#supplier-risk-agent", "policy#supplier-risk-agent#open_review_ticket"}
    assert all(key.startswith("action#probe-") for _, _, table, key in calls if table == ACTIONS)
    assert not any(svc == "iam" for svc, *_ in calls)


def test_materializer_requirements(gov, recorder):
    _materialize(gov, pol.load_policy(fixture("v1")), approve=True)
    _materialize(gov, pol.load_policy(fixture("v2")))
    _materialize(gov, pol.load_policy(fixture("v3")), approve=True)
    from app.governance import policy_cli
    policy_cli.status(gov.store, gov.layer)

    policy_cli.status(gov.store, gov.layer)
    gov.store.repair_pointer("supplier-risk-agent")

    # Namespace-restricted GetItem/PutItem only: no Scan (it cannot be
    # confined by dynamodb:LeadingKeys) and no Query.
    assert _needs(recorder) == [("dynamodb:GetItem", AUTHORITY), ("dynamodb:PutItem", AUTHORITY)]
    assert not any(action in ("dynamodb:Scan", "dynamodb:Query") for _, action, _, _ in recorder)
    namespace = ("policy#supplier-risk-agent#", "policymat#supplier-risk-agent#")
    touched = {key for _, action, table, key in recorder if table == AUTHORITY}
    assert all(k.startswith(namespace) for k in touched), touched
    assert "policymat#supplier-risk-agent#latest" in touched
    assert "agent#supplier-risk-agent" not in touched
    assert not any(t == ACTIONS for _, _, t, _ in recorder)

    iam = sorted({(action, role, arn) for svc, action, role, arn in recorder if svc == "iam"})
    deny_arn = "arn:aws:iam::123456789012:policy/RiskAgentPolicyClaudeDeny"
    assert iam == [
        ("iam:AttachRolePolicy", "RiskAgentGovernedWorkerRole", deny_arn),
        ("iam:DetachRolePolicy", "RiskAgentGovernedWorkerRole", deny_arn),
        ("iam:ListAttachedRolePolicies", "RiskAgentGovernedWorkerRole", None),
    ]
