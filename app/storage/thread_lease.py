"""Per-thread ownership lease for checkpointed workflows.

Draft design: docs/SAME_THREAD_OWNERSHIP_DESIGN.md. This module implements the
lease item and its lifecycle (acquire, renew, release) plus the errors used
by fenced checkpoint writes (checkpoint_store.DynamoCheckpointSaver with
fenced=True). The workflow lifecycle does not use it yet.

The lease lives in the checkpoint table, PK thread_id, SK "lease":
  epoch          monotonically increasing; incremented on every acquisition
                 and never reset (release keeps the item and its epoch)
  state          HELD | RELEASED
  owner          the owning attempt's trace_id
  owner_executor audit only
  expires_at_ms  owner's clock; lets another worker take over once passed

Rules:
  - acquire succeeds if the lease is absent, RELEASED or expired;
  - fenced writes and renewal require owner and epoch to match and state to
    be HELD; expiry is NOT part of that condition (DynamoDB has no server
    clock), so an expired owner keeps working only until someone takes over,
    which changes the epoch and fences it out.
"""
import os
import threading
import time
from dataclasses import dataclass

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, ConnectionError as BotoConnectionError, HTTPClientError

LEASE_SK = "lease"
FINAL_SK = "final"
RESERVED_SKS = frozenset({LEASE_SK, FINAL_SK})  # never deleted with a thread's checkpoints
HELD = "HELD"
RELEASED = "RELEASED"
DEFAULT_TTL_MS = 120_000
# The request may or may not have been applied: resolved by reading the lease back.
UNKNOWN_OUTCOME_EXCEPTIONS = (HTTPClientError, BotoConnectionError)


class OwnershipError(RuntimeError):
    """Base class for same-thread ownership failures."""


class LeaseContended(OwnershipError):
    """Another live owner holds the thread; nothing was executed or written."""

    def __init__(self, thread_id: str, holder: dict | None):
        self.thread_id = thread_id
        self.holder = holder or {}
        super().__init__(
            f"thread {thread_id} is held by {self.holder.get('owner')} "
            f"(epoch {self.holder.get('epoch')}, expires_at_ms {self.holder.get('expires_at_ms')})")


class LeaseLost(OwnershipError):
    """A fenced write or renewal found that this attempt no longer owns the thread."""

    def __init__(self, thread_id: str, owner: str, epoch: int, operation: str):
        self.thread_id, self.owner, self.epoch, self.operation = thread_id, owner, epoch, operation
        super().__init__(f"lost ownership of {thread_id} (owner {owner}, epoch {epoch}) during {operation}")


class OwnershipRequired(OwnershipError):
    """A fenced saver was asked to write without an ownership token (fail closed)."""


class WriteConflict(OwnershipError):
    """A durable item already exists with different content (a data conflict, not retried)."""


class WriteOutcomeUnknown(OwnershipError):
    """Retries were exhausted and read-back could not establish whether a write applied."""


@dataclass(frozen=True)
class Lease:
    thread_id: str
    owner: str
    epoch: int
    expires_at_ms: int


def _now_ms() -> int:
    return int(time.time() * 1000)


class LeaseStore:
    """Acquire / renew / release the lease item. now_ms is injectable for tests."""

    def __init__(self, table_name: str, region: str | None = None, *, ttl_ms: int = DEFAULT_TTL_MS,
                 now_ms=_now_ms, table=None) -> None:
        self.ttl_ms = ttl_ms
        self.now_ms = now_ms
        self._table = table or boto3.resource(
            "dynamodb", region_name=region or os.getenv("AWS_DEFAULT_REGION", "us-west-2")).Table(table_name)

    def read(self, thread_id: str) -> dict | None:
        items = self._table.query(
            KeyConditionExpression=Key("thread_id").eq(thread_id) & Key("sk").eq(LEASE_SK),
            ConsistentRead=True,
        ).get("Items", [])
        if not items:
            return None
        item = dict(items[0])
        for field in ("epoch", "expires_at_ms"):
            if field in item:
                item[field] = int(item[field])
        return item

    def acquire(self, thread_id: str, owner: str, *, executor: str = "", new_thread: bool = False) -> Lease:
        """Take ownership; raises LeaseContended if a live owner holds it.

        new_thread=True (START) requires that no lease exists yet.
        """
        now = self.now_ms()
        if new_thread:
            condition = "attribute_not_exists(sk)"
        else:
            condition = "attribute_not_exists(sk) OR #state = :released OR expires_at_ms < :now"
        values = {":held": HELD, ":owner": owner, ":executor": executor, ":expires": now + self.ttl_ms,
                  ":at": now, ":one": 1}
        if not new_thread:
            values.update({":released": RELEASED, ":now": now})
        try:
            response = self._table.update_item(
                Key={"thread_id": thread_id, "sk": LEASE_SK},
                UpdateExpression=("SET #state = :held, #owner = :owner, owner_executor = :executor, "
                                  "expires_at_ms = :expires, acquired_at_ms = :at ADD epoch :one"),
                ConditionExpression=condition,
                ExpressionAttributeNames={"#state": "state", "#owner": "owner"},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                return self._acquire_outcome_from_read(thread_id, owner, exc)
            holder = self.read(thread_id)
            if holder and holder.get("owner") == owner and holder.get("state") == HELD:
                # A retry of our own acquisition whose acknowledgment was lost.
                return Lease(thread_id, owner, holder["epoch"], holder["expires_at_ms"])
            raise LeaseContended(thread_id, holder) from None
        except UNKNOWN_OUTCOME_EXCEPTIONS as exc:
            return self._acquire_outcome_from_read(thread_id, owner, exc)
        item = response["Attributes"]
        return Lease(thread_id, owner, int(item["epoch"]), int(item["expires_at_ms"]))

    def _acquire_outcome_from_read(self, thread_id: str, owner: str, exc: Exception) -> Lease:
        """Unknown outcome: the acquisition applied only if the lease now names us."""
        item = self.read(thread_id)
        if item and item.get("owner") == owner and item.get("state") == HELD:
            return Lease(thread_id, owner, item["epoch"], item["expires_at_ms"])
        raise exc

    def renew(self, lease: Lease) -> Lease:
        """Extend expiry; allowed even after expiry as long as nobody took over (same epoch)."""
        now = self.now_ms()
        try:
            self._table.update_item(
                Key={"thread_id": lease.thread_id, "sk": LEASE_SK},
                UpdateExpression="SET expires_at_ms = :expires, renewed_at_ms = :at",
                ConditionExpression="#owner = :owner AND epoch = :epoch AND #state = :held",
                ExpressionAttributeNames={"#owner": "owner", "#state": "state"},
                ExpressionAttributeValues={":expires": now + self.ttl_ms, ":at": now, ":owner": lease.owner,
                                           ":epoch": lease.epoch, ":held": HELD},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise LeaseLost(lease.thread_id, lease.owner, lease.epoch, "renew") from None
            return self._renew_outcome_from_read(lease, exc)
        except UNKNOWN_OUTCOME_EXCEPTIONS as exc:
            return self._renew_outcome_from_read(lease, exc)
        return Lease(lease.thread_id, lease.owner, lease.epoch, now + self.ttl_ms)

    def _renew_outcome_from_read(self, lease: Lease, exc: Exception) -> Lease:
        item = self.read(lease.thread_id)  # unknown outcome: still ours?
        if not (item and item.get("owner") == lease.owner and item.get("epoch") == lease.epoch
                and item.get("state") == HELD):
            raise LeaseLost(lease.thread_id, lease.owner, lease.epoch, "renew") from exc
        return Lease(lease.thread_id, lease.owner, lease.epoch, item["expires_at_ms"])

    def release(self, lease: Lease) -> bool:
        """Mark RELEASED, keeping the item and its epoch. False if we no longer own it."""
        try:
            self._table.update_item(
                Key={"thread_id": lease.thread_id, "sk": LEASE_SK},
                UpdateExpression="SET #state = :released, released_at_ms = :at",
                ConditionExpression="#owner = :owner AND epoch = :epoch",
                ExpressionAttributeNames={"#owner": "owner", "#state": "state"},
                ExpressionAttributeValues={":released": RELEASED, ":at": self.now_ms(), ":owner": lease.owner,
                                           ":epoch": lease.epoch},
            )
            return True
        except (ClientError, *UNKNOWN_OUTCOME_EXCEPTIONS) as exc:
            if isinstance(exc, ClientError) and exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            item = self.read(lease.thread_id)  # unknown outcome
            return bool(item and item.get("owner") == lease.owner and item.get("epoch") == lease.epoch
                        and item.get("state") == RELEASED)


class LeaseHeartbeat:
    """Renews a lease every interval until stopped; sets `lost` if renewal is refused."""

    def __init__(self, store: LeaseStore, lease: Lease, *, interval_s: float | None = None):
        self.store = store
        self.lease = lease
        self.interval_s = interval_s if interval_s is not None else store.ttl_ms / 3000
        self.lost = threading.Event()
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lease-heartbeat-{lease.thread_id}", daemon=True)

    def start(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.lease = self.store.renew(self.lease)
            except LeaseLost as exc:
                self.error = exc
                self.lost.set()
                return
            except Exception as exc:  # transient: keep trying; fencing still protects writes
                self.error = exc

    def stop(self, timeout_s: float = 10.0) -> None:
        self._stop.set()
        self._thread.join(timeout_s)
