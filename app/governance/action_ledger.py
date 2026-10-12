"""Action ledger: lifecycle and idempotency of governed side effects.

Every privileged action has a deterministic action_id, so re-running the
workflow node that performs it maps to the same ledger item. Admission is a
single DynamoDB transaction:

  ConditionCheck  authority[agent#<id>]: status = ACTIVE AND version = observed
                  (operator emergency state, Phase 3D)
  ConditionCheck  authority[policy#<id>#<capability>]: effect = allow AND
                  policy_version AND policy_sha256 = observed
                  (materialized declarative policy, Phase 3F)
  Update          action[action_id] -> IN_FLIGHT,
                  only if the action is new or was previously DENIED

so "the operator has not revoked it", "policy allows it" and "this attempt
claimed it" are one atomic decision; there is no policy read followed by an
unguarded claim. Missing or malformed policy state denies. The side effect
runs after admission and is not interrupted by a later revocation or policy
change.

Statuses:
  DENIED     admission refused by authority; may be admitted later
  IN_FLIGHT  claimed; effect may or may not have happened
  COMPLETED  effect done and recorded; replays return the stored result

An action left IN_FLIGHT (executor died mid-effect) is never re-executed
here; it needs reconciliation, which is out of scope.

Table schema:
  PK: action_id (S)
"""
import os
from datetime import datetime, timezone
from typing import Callable

import boto3
from botocore.exceptions import ClientError

from app.governance.authority import (
    ACTIVE,
    UNCONFIGURED,
    AuthorityDenied,
    AuthorityStore,
)
from app.governance.policy import ALLOW, POLICY_ITEM_KIND, PolicyStore, policy_key_for_scope

DENIED = "DENIED"
IN_FLIGHT = "IN_FLIGHT"
COMPLETED = "COMPLETED"

# Admission retries only when authority or policy changed between read and
# transaction but both still allow (a restore or re-materialization racing with us).
MAX_ADMISSION_ATTEMPTS = 3


class ActionInFlight(RuntimeError):
    """The action was claimed by an earlier attempt that never completed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _result(item: dict, *, replayed: bool) -> dict:
    return {
        "action_id": item["action_id"],
        "action_type": item.get("action_type"),
        "status": item["status"],
        "authority_version": int(item["authority_version"]) if item.get("authority_version") is not None else None,
        "policy_version": int(item["policy_version"]) if item.get("policy_version") is not None else None,
        "policy_sha256": item.get("policy_sha256"),
        "effect": item.get("effect"),
        "replayed": replayed,
    }


class ActionLedger:
    """Gate for privileged actions: authority check + claim + completion."""

    def __init__(self, table_name: str, authority: AuthorityStore, region: str | None = None,
                 policies: PolicyStore | None = None) -> None:
        self.table_name = table_name
        self.authority = authority
        # Materialized policy lives in the authority table under policy#...
        self.policies = policies or PolicyStore(authority.table_name, region)
        resource = boto3.resource(
            "dynamodb",
            region_name=region or os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
        )
        self._table = resource.Table(table_name)
        # The resource's client serializes plain Python values in transactions.
        self._client = resource.meta.client

    def get(self, action_id: str) -> dict | None:
        return self._table.get_item(Key={"action_id": action_id}, ConsistentRead=True).get("Item")

    def execute(self, action_id: str, *, scope: str, context: dict, effect: Callable[[], dict]) -> dict:
        """Run effect() at most once per action_id, only if authority and policy admit it.

        Raises AuthorityDenied (action recorded as DENIED, no effect) or
        ActionInFlight (an earlier claim never completed). A COMPLETED action
        returns its stored result without running effect() again.
        """
        admitted = self._admit(action_id, scope=scope, context=context)
        if admitted["status"] == COMPLETED:
            return admitted
        return self._complete(action_id, context["trace_id"], effect())

    # ---- Admission ----

    def _admit(self, action_id: str, *, scope: str, context: dict) -> dict:
        policy_key = policy_key_for_scope(scope, context["action_type"])
        for _ in range(MAX_ADMISSION_ATTEMPTS):
            authority = self.authority.get(scope)
            policy = self.policies.get(policy_key)
            if authority["status"] == ACTIVE and policy["effect"] == ALLOW:
                try:
                    self._transact_claim(action_id, scope=scope, authority=authority,
                                         policy=policy, context=context)
                    return {"status": IN_FLIGHT, "authority_version": authority["version"],
                            "policy_version": policy["policy_version"], "policy_sha256": policy["policy_sha256"]}
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "TransactionCanceledException":
                        raise

            # Not admitted: work out why from durable state, action first.
            existing = self.get(action_id)
            if existing and existing["status"] == COMPLETED:
                return _result(existing, replayed=True)
            if existing and existing["status"] == IN_FLIGHT:
                raise ActionInFlight(
                    f"action {action_id} is IN_FLIGHT from trace {existing.get('claimed_by_trace_id')}; "
                    "reconciliation required"
                )
            authority = self.authority.get(scope)
            policy = self.policies.get(policy_key)
            emergency_denies = authority["status"] != ACTIVE
            policy_denies = policy["effect"] != ALLOW
            if emergency_denies or policy_denies:
                layer = "+".join(name for name, denies in (("emergency", emergency_denies),
                                                           ("policy", policy_denies)) if denies)
                self._record_denied(action_id, scope=scope, authority=authority, policy=policy,
                                    layer=layer, context=context)
                raise AuthorityDenied(
                    scope,
                    authority["status"] if emergency_denies else policy["status"],
                    authority["version"] if emergency_denies else policy["policy_version"],
                    authority["reason"] if emergency_denies else None,
                    action_id,
                    layer=layer,
                    authority_status=authority["status"],
                    authority_version=authority["version"],
                    policy_status=policy["status"],
                    policy_version=policy["policy_version"],
                    policy_sha256=policy["policy_sha256"],
                )
            # Both allow again at newer versions: retry against them.
        raise RuntimeError(f"admission for {action_id} did not settle after {MAX_ADMISSION_ATTEMPTS} attempts")

    def _transact_claim(self, action_id: str, *, scope: str, authority: dict, policy: dict, context: dict) -> None:
        now = _now()
        version = authority["version"]
        self._client.transact_write_items(TransactItems=[
            {"ConditionCheck": {
                "TableName": self.authority.table_name,
                "Key": {"scope_id": scope},
                "ConditionExpression": "#status = :active AND version = :version",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {":active": ACTIVE, ":version": version},
            }},
            {"ConditionCheck": {
                "TableName": self.policies.table_name,
                "Key": {"scope_id": policy["key"]},
                "ConditionExpression": (
                    "kind = :kind AND effect = :allow AND policy_version = :pv AND policy_sha256 = :ph"
                ),
                "ExpressionAttributeValues": {
                    ":kind": POLICY_ITEM_KIND, ":allow": ALLOW,
                    ":pv": policy["policy_version"], ":ph": policy["policy_sha256"],
                },
            }},
            {"Update": {
                "TableName": self.table_name,
                "Key": {"action_id": action_id},
                "UpdateExpression": (
                    "SET #status = :in_flight, action_type = :action_type, thread_id = :thread_id, "
                    "#scope = :scope, authority_version = :version, "
                    "policy_version = :pv, policy_sha256 = :ph, "
                    "claimed_by_trace_id = :trace_id, claimed_by_executor = :executor_id, "
                    "attempt_count = if_not_exists(attempt_count, :zero) + :one, "
                    "subject = :subject, created_at = if_not_exists(created_at, :now), updated_at = :now"
                ),
                "ConditionExpression": "attribute_not_exists(action_id) OR #status = :denied",
                "ExpressionAttributeNames": {"#status": "status", "#scope": "scope"},
                "ExpressionAttributeValues": {
                    ":in_flight": IN_FLIGHT, ":denied": DENIED,
                    ":action_type": context["action_type"], ":thread_id": context["thread_id"],
                    ":scope": scope, ":version": version,
                    ":pv": policy["policy_version"], ":ph": policy["policy_sha256"],
                    ":trace_id": context["trace_id"], ":executor_id": context["executor_id"],
                    ":zero": 0, ":one": 1, ":subject": context.get("subject") or {}, ":now": now,
                },
            }},
        ])

    def _record_denied(self, action_id: str, *, scope: str, authority: dict, policy: dict, layer: str,
                       context: dict) -> None:
        now = _now()
        try:
            self._table.update_item(
                Key={"action_id": action_id},
                UpdateExpression=(
                    "SET #status = :denied, action_type = :action_type, thread_id = :thread_id, "
                    "#scope = :scope, denied_authority_status = :auth_status, "
                    "denied_authority_version = :auth_version, denied_layer = :layer, "
                    "denied_policy_status = :pol_status, denied_policy_version = :pol_version, "
                    "denied_policy_sha256 = :pol_sha, denied_by_trace_id = :trace_id, "
                    "denied_executor = :executor_id, denied_count = if_not_exists(denied_count, :zero) + :one, "
                    "subject = :subject, created_at = if_not_exists(created_at, :now), updated_at = :now"
                ),
                # Never downgrade a claimed or completed action.
                ConditionExpression="attribute_not_exists(action_id) OR #status = :denied",
                ExpressionAttributeNames={"#status": "status", "#scope": "scope"},
                ExpressionAttributeValues={
                    ":denied": DENIED, ":action_type": context["action_type"],
                    ":thread_id": context["thread_id"], ":scope": scope,
                    ":auth_status": authority["status"], ":auth_version": authority["version"],
                    ":layer": layer, ":pol_status": policy["status"],
                    ":pol_version": policy["policy_version"], ":pol_sha": policy["policy_sha256"],
                    ":trace_id": context["trace_id"], ":executor_id": context["executor_id"],
                    ":zero": 0, ":one": 1, ":subject": context.get("subject") or {}, ":now": now,
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    # ---- Completion ----

    def _complete(self, action_id: str, trace_id: str, effect: dict) -> dict:
        response = self._table.update_item(
            Key={"action_id": action_id},
            UpdateExpression="SET #status = :completed, effect = :effect, completed_at = :now, updated_at = :now",
            ConditionExpression="#status = :in_flight AND claimed_by_trace_id = :trace_id",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":completed": COMPLETED, ":in_flight": IN_FLIGHT,
                ":trace_id": trace_id, ":effect": effect, ":now": _now(),
            },
            ReturnValues="ALL_NEW",
        )
        return _result(response["Attributes"], replayed=False)


class UnconfiguredActionGate:
    """Used when an executor has no governance configuration: deny everything.

    Skipping the action instead would let an unconfigured executor bypass
    authority.
    """

    def execute(self, action_id: str, *, scope: str, context: dict, effect: Callable[[], dict]) -> dict:
        raise AuthorityDenied(scope, UNCONFIGURED, None, "AUTHORITY_TABLE/ACTIONS_TABLE not configured", action_id)


def action_gate_from_env():
    """Ledger + authority from AUTHORITY_TABLE / ACTIONS_TABLE; fail closed if either is missing."""
    authority_table = os.environ.get("AUTHORITY_TABLE")
    actions_table = os.environ.get("ACTIONS_TABLE")
    if not (authority_table and actions_table):
        return UnconfiguredActionGate()
    return ActionLedger(actions_table, AuthorityStore(authority_table))
