"""Once-only finalization: business state and the final marker in one transaction.

docs/SAME_THREAD_OWNERSHIP_DESIGN.md §3 and §7. The `final` item lives in the
checkpoint table (PK thread_id, SK "final") next to the lease. commit() writes,
atomically and only while the caller owns the thread:

  ConditionCheck  lease: owner = :me AND epoch = :epoch AND state = HELD
  Put             final, attribute_not_exists(sk)          (once per thread)
  Put             supplier-state row (optional; unconditional, versioning is X1)

Legacy recognition writes the marker without a supplier row.
"""
import hashlib
import os
import time

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from app.storage.fenced_writes import ABSENT, APPLIED, run_fenced_transaction
from app.storage.thread_lease import FINAL_SK, HELD, LEASE_SK, Lease, OwnershipError

FINALIZED = "finalized"
LEGACY_RECOGNIZED = "legacy_recognized"


class AlreadyFinalized(OwnershipError):
    """The thread's final marker exists and was written by another attempt."""

    def __init__(self, thread_id: str, marker: dict):
        self.thread_id, self.marker = thread_id, marker
        super().__init__(f"thread {thread_id} was already finalized by {marker.get('trace_id')} "
                         f"(epoch {marker.get('epoch')})")


def _plain(value):
    """DynamoDB numbers come back as Decimal; markers carry small integers only."""
    if hasattr(value, "as_integer_ratio") and not isinstance(value, (int, float)):
        return int(value) if value == int(value) else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


class FinalizationStore:
    def __init__(self, checkpoint_table: str, state_table: str, region: str | None = None, *,
                 max_attempts: int = 5, backoff_s: float = 0.05, sleep=time.sleep) -> None:
        region = region or os.getenv("AWS_DEFAULT_REGION", "us-west-2")
        self.checkpoint_table, self.state_table = checkpoint_table, state_table
        self._table = boto3.resource("dynamodb", region_name=region).Table(checkpoint_table)
        self._client = boto3.client("dynamodb", region_name=region)
        self._serializer, self._deserializer = TypeSerializer(), TypeDeserializer()
        self.max_attempts, self.backoff_s, self._sleep = max_attempts, backoff_s, sleep

    def read(self, thread_id: str) -> dict | None:
        items = self._table.query(
            KeyConditionExpression=Key("thread_id").eq(thread_id) & Key("sk").eq(FINAL_SK),
            ConsistentRead=True,
        ).get("Items", [])
        return _plain(dict(items[0])) if items else None

    def _av(self, mapping: dict) -> dict:
        return {k: self._serializer.serialize(v) for k, v in mapping.items() if v is not None}

    def _still_owner(self, lease: Lease) -> bool:
        items = self._table.query(
            KeyConditionExpression=Key("thread_id").eq(lease.thread_id) & Key("sk").eq(LEASE_SK),
            ConsistentRead=True,
        ).get("Items", [])
        item = items[0] if items else {}
        return item.get("owner") == lease.owner and int(item.get("epoch", -1)) == lease.epoch \
            and item.get("state") == HELD

    def commit(self, lease: Lease, marker: dict, supplier: tuple[str, str, dict] | None = None) -> dict:
        """Write the marker (and supplier row) once. Raises LeaseLost or AlreadyFinalized."""
        thread_id = lease.thread_id
        marker = {**marker, "thread_id": thread_id, "sk": FINAL_SK, "epoch": lease.epoch}
        items = [
            {"ConditionCheck": {
                "TableName": self.checkpoint_table,
                "Key": self._av({"thread_id": thread_id, "sk": LEASE_SK}),
                "ConditionExpression": "#owner = :owner AND epoch = :epoch AND #state = :held",
                "ExpressionAttributeNames": {"#owner": "owner", "#state": "state"},
                "ExpressionAttributeValues": self._av({":owner": lease.owner, ":epoch": lease.epoch,
                                                       ":held": HELD}),
            }},
            {"Put": {"TableName": self.checkpoint_table, "Item": self._av(marker),
                     "ConditionExpression": "attribute_not_exists(sk)"}},
        ]
        if supplier is not None:
            name, risk_type, row = supplier
            items.append({"Put": {"TableName": self.state_table, "Item": self._av(
                {**row, "pk": f"{name}#{risk_type}", "supplier": name, "risk_type": risk_type})}})

        def verify() -> str:
            existing = self.read(thread_id)
            if existing is None:
                return ABSENT
            if existing.get("trace_id") == marker.get("trace_id") and existing.get("epoch") == lease.epoch:
                return APPLIED  # our own earlier attempt (the supplier row was in the same transaction)
            raise AlreadyFinalized(thread_id, existing)

        request_token = hashlib.sha256(f"final|{thread_id}|{lease.owner}|{lease.epoch}".encode()).hexdigest()[:32]
        run_fenced_transaction(self._client, token=(thread_id, lease.owner, lease.epoch), operation="finalization",
                               items=items, request_token=request_token, verify=verify,
                               still_owner=lambda: self._still_owner(lease), max_attempts=self.max_attempts,
                               backoff_s=self.backoff_s, sleep=self._sleep)
        return _plain({k: v for k, v in marker.items() if k != "sk"})
