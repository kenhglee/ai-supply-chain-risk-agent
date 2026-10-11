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

Each checkpoint item holds the whole serialized checkpoint (including
channel_values), so no per-channel blob reassembly is needed. Checkpoint ids
are uuid6 strings, so sort-key order is creation order; the latest
checkpoint is the last chk# item for the thread/namespace.
"""
import os
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import Binary
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

# DynamoDB rejects items over 400 KB; fail earlier with a clear message.
MAX_ITEM_BYTES = 350_000


def _bytes(value: Any) -> bytes:
    return value.value if isinstance(value, Binary) else bytes(value)


class DynamoCheckpointSaver(BaseCheckpointSaver):
    def __init__(self, table_name: str, region: str | None = None, *, serde=None) -> None:
        super().__init__(serde=serde)
        self._table = boto3.resource(
            "dynamodb",
            region_name=region or os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
        ).Table(table_name)

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
        keys = [
            {"thread_id": item["thread_id"], "sk": item["sk"]}
            for item in self._query_all(
                KeyConditionExpression=Key("thread_id").eq(thread_id),
                ProjectionExpression="thread_id, sk",
            )
        ]
        with self._table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key=key)
