"""DynamoDB-backed LangGraph checkpoint saver.

Minimal sync implementation of BaseCheckpointSaver for the supplier risk
graph: get_tuple / put / put_writes (used by Pregel), plus list and
delete_thread (used by tests and operations). Async methods,
get_delta_channel_history (only needed for DeltaChannel), copy_thread and
prune are not implemented.

Table schema:
  PK: thread_id (S)
  SK: sk (S)
    checkpoint    -> "chk#{checkpoint_ns}#{checkpoint_id}"
    pending write -> "wr#{checkpoint_ns}#{checkpoint_id}#{task_id}#{idx}"

    ownership     -> "lease", "final" (app/storage/thread_lease.py); never
                     deleted with the thread

Each checkpoint item holds the whole serialized checkpoint (including
channel_values), so no per-channel blob reassembly is needed. Checkpoint ids
are uuid6 strings, so sort-key order is creation order; the latest
checkpoint is the last chk# item for the thread/namespace.

Fenced mode (fenced=True; docs/SAME_THREAD_OWNERSHIP_DESIGN.md): every
checkpoint put and every put_writes batch (regular and special writes) is
one TransactWriteItems that also ConditionChecks the thread's lease
(owner = lease_owner AND epoch = lease_epoch AND state = HELD, taken from
configurable). A write without that token is refused. Retries reuse one
serialized payload and a deterministic ClientRequestToken; after an unknown
outcome or a data-condition failure the write is verified by reading it
back. Unfenced mode (the default) keeps the original unconditional
behavior; the workflow lifecycle does not use fenced mode yet.
"""
import hashlib
import os
import time
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import Binary, TypeSerializer
from botocore.exceptions import ClientError
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from app.storage.fenced_writes import ABSENT, APPLIED, run_fenced_transaction
from app.storage.thread_lease import (
    HELD,
    LEASE_SK,
    RESERVED_SKS,
    LeaseLost,
    OwnershipRequired,
    WriteConflict,
)

# DynamoDB rejects items over 400 KB; fail earlier with a clear message.
MAX_ITEM_BYTES = 350_000
# One task's writes go in one transaction (100 items, one is the lease check).
MAX_FENCED_WRITES = 99



def _bytes(value: Any) -> bytes:
    return value.value if isinstance(value, Binary) else bytes(value)


class DynamoCheckpointSaver(BaseCheckpointSaver):
    def __init__(self, table_name: str, region: str | None = None, *, serde=None, fenced: bool = False,
                 max_attempts: int = 5, backoff_s: float = 0.05, sleep=time.sleep) -> None:
        super().__init__(serde=serde)
        region = region or os.getenv("AWS_DEFAULT_REGION", "us-west-2")
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self.table_name = table_name
        # LangGraph calls put / put_writes concurrently from background threads;
        # fenced transactions use a dedicated low-level client (boto3 clients are
        # thread-safe) with explicit AttributeValue serialization.
        self._client = boto3.client("dynamodb", region_name=region)
        self._serializer = TypeSerializer()
        self.fenced = fenced
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self._sleep = sleep
        # (thread_id, owner, epoch) tokens that have lost ownership: later writes
        # are refused without a request. LangGraph can swallow an exception raised
        # by a background writer while another exception propagates, so callers
        # check this instead of relying on the exception reaching them.
        self.lost: dict[tuple[str, str, int], LeaseLost] = {}
        # Lineage: the last checkpoint each (thread, owner, epoch) wrote.
        self.last_checkpoint: dict[tuple[str, str, int], str] = {}

    # ---- Keys ----

    @staticmethod
    def _ns(config: RunnableConfig) -> str:
        ns = config["configurable"].get("checkpoint_ns", "")
        if "#" in ns:
            raise ValueError(f"checkpoint_ns may not contain '#': {ns!r}")
        return ns

    @staticmethod
    def _chk_prefix(ns: str) -> str:
        return f"chk#{ns}#"

    @classmethod
    def _chk_sk(cls, ns: str, checkpoint_id: str) -> str:
        return f"{cls._chk_prefix(ns)}{checkpoint_id}"

    @staticmethod
    def _wr_prefix(ns: str, checkpoint_id: str) -> str:
        return f"wr#{ns}#{checkpoint_id}#"

    # ---- Read helpers ----

    def _query_all(self, **kwargs) -> Iterator[dict]:
        while True:
            response = self._table.query(ConsistentRead=True, **kwargs)
            yield from response.get("Items", [])
            last = response.get("LastEvaluatedKey")
            if not last:
                return
            kwargs["ExclusiveStartKey"] = last

    def _load_writes(self, thread_id: str, ns: str, checkpoint_id: str) -> list[tuple[str, str, Any]]:
        items = list(self._query_all(
            KeyConditionExpression=Key("thread_id").eq(thread_id)
            & Key("sk").begins_with(self._wr_prefix(ns, checkpoint_id)),
        ))
        items.sort(key=lambda i: (i["task_id"], int(i["idx"])))
        return [
            (i["task_id"], i["channel"], self.serde.loads_typed((i["value_type"], _bytes(i["value"]))))
            for i in items
        ]

    def _to_tuple(self, item: dict) -> CheckpointTuple:
        thread_id = item["thread_id"]
        ns = item["checkpoint_ns"]
        checkpoint_id = item["checkpoint_id"]
        parent_id = item.get("parent_checkpoint_id")
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=self.serde.loads_typed((item["checkpoint_type"], _bytes(item["checkpoint"]))),
            metadata=self.serde.loads_typed((item["metadata_type"], _bytes(item["metadata"]))),
            pending_writes=self._load_writes(thread_id, ns, checkpoint_id),
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id
                else None
            ),
        )

    # ---- BaseCheckpointSaver ----

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        ns = self._ns(config)

        if checkpoint_id := get_checkpoint_id(config):
            item = self._table.get_item(
                Key={"thread_id": thread_id, "sk": self._chk_sk(ns, checkpoint_id)},
                ConsistentRead=True,
            ).get("Item")
        else:
            items = self._table.query(
                KeyConditionExpression=Key("thread_id").eq(thread_id)
                & Key("sk").begins_with(self._chk_prefix(ns)),
                ScanIndexForward=False,
                Limit=1,
                ConsistentRead=True,
            ).get("Items", [])
            item = items[0] if items else None

        return self._to_tuple(item) if item else None

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config is None:
            raise NotImplementedError("DynamoCheckpointSaver.list requires a thread_id config")

        thread_id = config["configurable"]["thread_id"]
        ns = self._ns(config)
        prefix = self._chk_prefix(ns)

        if checkpoint_id := get_checkpoint_id(config):
            key_condition = Key("thread_id").eq(thread_id) & Key("sk").eq(self._chk_sk(ns, checkpoint_id))
        elif before is not None and (before_id := get_checkpoint_id(before)):
            # BETWEEN is inclusive; the upper bound itself is skipped below.
            key_condition = Key("thread_id").eq(thread_id) & Key("sk").between(prefix, self._chk_sk(ns, before_id))
        else:
            key_condition = Key("thread_id").eq(thread_id) & Key("sk").begins_with(prefix)

        before_sk = (
            self._chk_sk(ns, get_checkpoint_id(before))
            if before is not None and get_checkpoint_id(before)
            else None
        )

        returned = 0
        for item in self._query_all(KeyConditionExpression=key_condition, ScanIndexForward=False):
            if limit is not None and returned >= limit:
                return
            if item["sk"] == before_sk:
                continue
            tup = self._to_tuple(item)
            if filter and not all(tup.metadata.get(k) == v for k, v in filter.items()):
                continue
            returned += 1
            yield tup

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        ns = self._ns(config)
        checkpoint_id = checkpoint["id"]
        parent_id = config["configurable"].get("checkpoint_id")

        checkpoint_type, checkpoint_bytes = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_bytes = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))

        size = len(checkpoint_bytes) + len(metadata_bytes)
        if size > MAX_ITEM_BYTES:
            raise ValueError(
                f"Checkpoint {checkpoint_id} for thread {thread_id} is {size} bytes; "
                f"limit is {MAX_ITEM_BYTES}"
            )

        item = {
            "thread_id": thread_id,
            "sk": self._chk_sk(ns, checkpoint_id),
            "checkpoint_ns": ns,
            "checkpoint_id": checkpoint_id,
            "checkpoint_type": checkpoint_type,
            "checkpoint": checkpoint_bytes,
            "metadata_type": metadata_type,
            "metadata": metadata_bytes,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if parent_id:
            item["parent_checkpoint_id"] = parent_id
        if self.fenced:
            self._fenced_put(config, item)
        else:
            self._table.put_item(Item=item)

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        ns = self._ns(config)
        checkpoint_id = config["configurable"]["checkpoint_id"]
        prefix = self._wr_prefix(ns, checkpoint_id)

        if self.fenced:
            self._fenced_put_writes(config, writes, task_id, task_path)
            return

        for i, (channel, value) in enumerate(writes):
            idx = WRITES_IDX_MAP.get(channel, i)
            value_type, value_bytes = self.serde.dumps_typed(value)
            item = {
                "thread_id": thread_id,
                "sk": f"{prefix}{task_id}#{idx}",
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "task_path": task_path,
                "channel": channel,
                "idx": idx,
                "value_type": value_type,
                "value": value_bytes,
            }
            if idx < 0:
                # Special writes (error/interrupt/resume/scheduled) overwrite.
                self._table.put_item(Item=item)
                continue
            # Regular writes are first-write-wins, as in InMemorySaver.
            try:
                self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(sk)")
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise

    def delete_thread(self, thread_id: str) -> None:
        """Delete checkpoints and pending writes; the lease and final items are kept,
        so an epoch is never reused for the thread."""
        keys = [
            {"thread_id": item["thread_id"], "sk": item["sk"]}
            for item in self._query_all(
                KeyConditionExpression=Key("thread_id").eq(thread_id),
                ProjectionExpression="thread_id, sk",
            )
            if item["sk"] not in RESERVED_SKS
        ]
        with self._table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key=key)

    # ---- Fenced writes ----

    def _token(self, config: RunnableConfig) -> tuple[str, str, int]:
        configurable = config["configurable"]
        owner, epoch = configurable.get("lease_owner"), configurable.get("lease_epoch")
        if not owner or epoch is None:
            raise OwnershipRequired(
                f"fenced write for thread {configurable.get('thread_id')} has no lease_owner/lease_epoch")
        token = (configurable["thread_id"], str(owner), int(epoch))
        if token in self.lost:
            raise self.lost[token]
        return token

    def _av(self, mapping: dict) -> dict:
        """Plain Python values -> DynamoDB AttributeValue maps (for the low-level client)."""
        return {k: self._serializer.serialize(v) for k, v in mapping.items()}

    def _lease_check(self, token: tuple[str, str, int]) -> dict:
        thread_id, owner, epoch = token
        return {"ConditionCheck": {
            "TableName": self.table_name,
            "Key": self._av({"thread_id": thread_id, "sk": LEASE_SK}),
            "ConditionExpression": "#owner = :owner AND epoch = :epoch AND #state = :held",
            "ExpressionAttributeNames": {"#owner": "owner", "#state": "state"},
            "ExpressionAttributeValues": self._av({":owner": owner, ":epoch": epoch, ":held": HELD}),
        }}

    def _lease_lost(self, token: tuple[str, str, int], operation: str) -> LeaseLost:
        error = LeaseLost(token[0], token[1], token[2], operation)
        self.lost[token] = error
        return error

    def _still_owner(self, token: tuple[str, str, int]) -> bool:
        items = self._table.query(
            KeyConditionExpression=Key("thread_id").eq(token[0]) & Key("sk").eq(LEASE_SK),
            ConsistentRead=True,
        ).get("Items", [])
        lease = items[0] if items else {}
        return lease.get("owner") == token[1] and int(lease.get("epoch", -1)) == token[2] \
            and lease.get("state") == HELD

    @staticmethod
    def _request_token(*parts: Any) -> str:
        return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]

    def _transact(self, token, operation: str, items: "list[dict]", request_token: str, verify) -> None:
        try:
            run_fenced_transaction(self._client, token=token, operation=operation, items=items,
                                   request_token=request_token, verify=verify,
                                   still_owner=lambda: self._still_owner(token),
                                   max_attempts=self.max_attempts, backoff_s=self.backoff_s, sleep=self._sleep)
        except LeaseLost:
            raise self._lease_lost(token, operation) from None

    def _fenced_put(self, config: RunnableConfig, item: dict) -> None:
        token = self._token(config)
        item = {**item, "lease_owner": token[1], "lease_epoch": token[2]}

        def verify() -> str:
            stored = self._table.get_item(Key={"thread_id": item["thread_id"], "sk": item["sk"]},
                                          ConsistentRead=True).get("Item")
            if stored is None:
                return ABSENT
            same = all(_bytes(stored[k]) == item[k] for k in ("checkpoint", "metadata")) and all(
                stored.get(k) == item.get(k)
                for k in ("checkpoint_type", "metadata_type", "parent_checkpoint_id", "lease_owner"))
            same = same and int(stored.get("lease_epoch", -1)) == token[2]
            if same:
                return APPLIED  # our own earlier attempt: an identical retry is not a conflict
            raise WriteConflict(f"checkpoint {item['checkpoint_id']} exists with different content")

        self._transact(
            token, f"checkpoint {item['checkpoint_id']}",
            [self._lease_check(token),
             {"Put": {"TableName": self.table_name, "Item": self._av(item),
                      "ConditionExpression": "attribute_not_exists(sk)"}}],
            self._request_token("chk", *token, item["sk"]),
            verify,
        )
        self.last_checkpoint[token] = item["checkpoint_id"]

    def _fenced_put_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str,
                           task_path: str) -> None:
        token = self._token(config)
        if not writes:
            return
        if len(writes) > MAX_FENCED_WRITES:
            raise ValueError(f"task {task_id} has {len(writes)} writes; a fenced batch holds at most "
                             f"{MAX_FENCED_WRITES} (refusing to split it)")
        thread_id, ns = token[0], self._ns(config)
        checkpoint_id = config["configurable"]["checkpoint_id"]
        prefix = self._wr_prefix(ns, checkpoint_id)
        batch = []
        for i, (channel, value) in enumerate(writes):
            idx = WRITES_IDX_MAP.get(channel, i)
            value_type, value_bytes = self.serde.dumps_typed(value)  # serialized once, reused on retry
            batch.append({
                "thread_id": thread_id, "sk": f"{prefix}{task_id}#{idx}", "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id, "task_id": task_id, "task_path": task_path,
                "channel": channel, "idx": idx, "value_type": value_type, "value": value_bytes,
                "lease_owner": token[1], "lease_epoch": token[2],
            })

        def verify() -> str:
            stored = {i["sk"]: i for i in self._query_all(
                KeyConditionExpression=Key("thread_id").eq(thread_id)
                & Key("sk").begins_with(f"{prefix}{task_id}#"))}

            def same(item):
                found = stored.get(item["sk"])
                return found is not None and found["channel"] == item["channel"] \
                    and found["value_type"] == item["value_type"] and _bytes(found["value"]) == item["value"]

            regular = [i for i in batch if i["idx"] >= 0]
            special = [i for i in batch if i["idx"] < 0]
            if regular:
                present = [i for i in regular if i["sk"] in stored]
                if not present:
                    return ABSENT
                if len(present) == len(regular) and all(same(i) for i in regular) and all(same(i) for i in special):
                    return APPLIED
                raise WriteConflict(f"pending writes for task {task_id} are partial or differ from this batch")
            # Special writes only: they overwrite, so a differing one is simply not applied yet.
            return APPLIED if all(same(i) for i in special) else ABSENT

        items = [self._lease_check(token)]
        for item in batch:
            put = {"TableName": self.table_name, "Item": self._av(item)}
            if item["idx"] >= 0:
                put["ConditionExpression"] = "attribute_not_exists(sk)"  # regular writes: first wins
            items.append({"Put": put})
        payload = hashlib.sha256(b"".join(i["value"] for i in batch)).hexdigest()
        self._transact(token, f"pending writes for task {task_id}", items,
                       self._request_token("wr", *token, prefix, task_id, payload), verify)

