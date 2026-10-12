"""Durable runtime authority (kill switch) for the supplier risk agent.

Authority is external to the executor: one DynamoDB item per scope, changed
only by an operator (authority_cli), read by the agent before each
privileged action. Revoking authority does not stop compute; it makes the
next privileged action fail admission.

Table schema:
  PK: scope_id (S), e.g. "agent#supplier-risk-agent"
  status:     ACTIVE | REVOKED
  version:    N, incremented on every change
  reason, updated_by, updated_at

A missing item is treated as not authorized (fail closed).
"""
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# The agent's identity is fixed in code: an executor must not be able to
# choose the scope it is governed by.
AGENT_ID = "supplier-risk-agent"
AGENT_SCOPE = f"agent#{AGENT_ID}"

ACTIVE = "ACTIVE"
REVOKED = "REVOKED"
MISSING = "MISSING"  # no authority item: never granted
UNCONFIGURED = "UNCONFIGURED"  # executor has no governance configuration


class AuthorityDenied(RuntimeError):
    """A privileged action was refused because of external authority state."""

    def __init__(self, scope: str, status: str, version: int | None,
                 reason: str | None = None, action_id: str | None = None, *,
                 layer: str = "emergency", authority_status: str | None = None,
                 authority_version: int | None = None, policy_status: str | None = None,
                 policy_version: int | None = None, policy_sha256: str | None = None) -> None:
        super().__init__(f"{layer} denied action {action_id}: {status} (version {version})")
        self.scope = scope
        # status/version describe the denying layer: the emergency authority
        # item when it denies, otherwise the materialized policy item.
        self.status = status
        self.version = version
        self.reason = reason
        self.action_id = action_id
        self.layer = layer
        self.authority_status = authority_status if authority_status is not None else (
            status if layer == "emergency" else None)
        self.authority_version = authority_version if authority_version is not None else (
            version if layer == "emergency" else None)
        self.policy_status = policy_status
        self.policy_version = policy_version
        self.policy_sha256 = policy_sha256


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def snapshot(item: dict | None, scope: str) -> dict:
    """Plain view of an authority item; MISSING when it doesn't exist."""
    if item is None:
        return {"scope": scope, "status": MISSING, "version": 0, "reason": None}
    return {
        "scope": scope,
        "status": item.get("status"),
        "version": int(item.get("version", 0)),
        "reason": item.get("reason"),
    }


class AuthorityStore:
    def __init__(self, table_name: str, region: str | None = None) -> None:
        self.table_name = table_name
        self._table = boto3.resource(
            "dynamodb",
            region_name=region or os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
        ).Table(table_name)

    def get(self, scope: str = AGENT_SCOPE) -> dict:
        item = self._table.get_item(Key={"scope_id": scope}, ConsistentRead=True).get("Item")
        return snapshot(item, scope)

    def _set_status(self, scope: str, status: str, *, reason: str, actor: str, must_exist: bool) -> dict:
        kwargs = {}
        if must_exist:
            kwargs["ConditionExpression"] = "attribute_exists(scope_id)"
        try:
            response = self._table.update_item(
                Key={"scope_id": scope},
                UpdateExpression=(
                    "SET #status = :status, version = if_not_exists(version, :zero) + :one, "
                    "reason = :reason, updated_by = :actor, updated_at = :now"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": status, ":zero": 0, ":one": 1,
                    ":reason": reason, ":actor": actor, ":now": _now(),
                },
                ReturnValues="ALL_NEW",
                **kwargs,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise LookupError(f"no authority item for {scope}; restore it first") from exc
            raise
        return snapshot(response["Attributes"], scope)

    def revoke(self, scope: str = AGENT_SCOPE, *, reason: str, actor: str) -> dict:
        # Never creates an item: revoking an unknown scope is an operator error.
        return self._set_status(scope, REVOKED, reason=reason, actor=actor, must_exist=True)

    def restore(self, scope: str = AGENT_SCOPE, *, reason: str, actor: str) -> dict:
        # Also the initial grant: creates the item at version 1.
        return self._set_status(scope, ACTIVE, reason=reason, actor=actor, must_exist=False)
