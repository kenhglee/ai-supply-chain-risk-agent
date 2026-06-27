import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Attr

_STORE_PATH = Path(__file__).parent / "risk_decisions.jsonl"

REVIEW_REQUIRED_DECISIONS = {"manual_review_required", "review_recommended"}


# ---- JSONL implementation ----

class JsonlDecisionStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def save(
        self,
        normalized_event: dict,
        decision: dict,
        ticket: dict | None = None,
    ) -> str:
        record_id = uuid.uuid4().hex
        record = {
            "id": record_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "normalized_event": normalized_event,
            "decision": decision,
            "ticket": ticket,
            "store_backend": "jsonl",
        }
        with open(self._path, "a") as f:
            f.write(json.dumps(record) + "\n")
        return record_id

    def _load_all(self) -> list[dict]:
        if not self._path.exists():
            return []
        records = []
        with open(self._path) as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def get_recent(self, limit: int = 20) -> list[dict]:
        return sorted(self._load_all(), key=lambda r: r["timestamp"], reverse=True)[:limit]

    def get_requiring_review(self, limit: int = 10) -> list[dict]:
        records = self._load_all()
        filtered = [
            r for r in records
            if r.get("decision", {}).get("decision") in REVIEW_REQUIRED_DECISIONS
        ]
        return sorted(filtered, key=lambda r: r["timestamp"], reverse=True)[:limit]


# ---- DynamoDB implementation ----

class DynamoDecisionStore:
    """
    Table schema:
      PK: id (S)
      Native attributes: id, timestamp, decision_type, store_backend
      JSON strings: normalized_event, decision, ticket (omitted when None)

    decision_type is the decision["decision"] value lifted to a top-level attribute
    so FilterExpression can filter server-side without deserializing payloads.
    """

    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    @staticmethod
    def _to_dynamo_item(
        record_id: str,
        timestamp: str,
        normalized_event: dict,
        decision: dict,
        ticket: dict | None,
    ) -> dict:
        item: dict = {
            "id": record_id,
            "timestamp": timestamp,
            "decision_type": decision.get("decision", ""),
            "normalized_event": json.dumps(normalized_event),
            "decision": json.dumps(decision),
            "store_backend": "dynamodb",
        }
        if ticket is not None:
            item["ticket"] = json.dumps(ticket)
        return item

    @staticmethod
    def _from_dynamo_item(item: dict) -> dict:
        return {
            "id": item["id"],
            "timestamp": item["timestamp"],
            "normalized_event": json.loads(item["normalized_event"]),
            "decision": json.loads(item["decision"]),
            "ticket": json.loads(item["ticket"]) if "ticket" in item else None,
            "store_backend": item.get("store_backend"),
        }

    def _scan_all(self, filter_expression=None) -> list[dict]:
        items: list[dict] = []
        kwargs: dict = {}
        if filter_expression is not None:
            kwargs["FilterExpression"] = filter_expression
        while True:
            response = self._table.scan(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return [self._from_dynamo_item(item) for item in items]

    def save(
        self,
        normalized_event: dict,
        decision: dict,
        ticket: dict | None = None,
    ) -> str:
        record_id = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        item = self._to_dynamo_item(record_id, timestamp, normalized_event, decision, ticket)
        self._table.put_item(Item=item)
        return record_id

    def get_recent(self, limit: int = 20) -> list[dict]:
        records = self._scan_all()
        return sorted(records, key=lambda r: r["timestamp"], reverse=True)[:limit]

    def get_requiring_review(self, limit: int = 10) -> list[dict]:
        filter_expr = Attr("decision_type").is_in(list(REVIEW_REQUIRED_DECISIONS))
        records = self._scan_all(filter_expression=filter_expr)
        return sorted(records, key=lambda r: r["timestamp"], reverse=True)[:limit]


# ---- Factory and lazy accessor ----

def get_decision_store() -> JsonlDecisionStore | DynamoDecisionStore:
    backend = os.getenv("DECISION_STORE_BACKEND", "jsonl").lower()
    if backend == "jsonl":
        return JsonlDecisionStore(_STORE_PATH)
    if backend == "dynamodb":
        return DynamoDecisionStore(
            table_name=os.getenv("RISK_DECISIONS_TABLE", "risk_decisions"),
            region=os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
        )
    raise ValueError(f"Unsupported DECISION_STORE_BACKEND: '{backend}'")


_store: JsonlDecisionStore | DynamoDecisionStore | None = None


def _get_decision_store() -> JsonlDecisionStore | DynamoDecisionStore:
    global _store
    if _store is None:
        _store = get_decision_store()
    return _store


# ---- Public API (signatures unchanged) ----

def save_risk_decision(
    normalized_event: dict,
    decision: dict,
    ticket: dict | None = None,
) -> str:
    return _get_decision_store().save(normalized_event, decision, ticket)


def get_recent_risk_decisions(limit: int = 20) -> list[dict]:
    return _get_decision_store().get_recent(limit)


def get_decisions_requiring_review(limit: int = 10) -> list[dict]:
    return _get_decision_store().get_requiring_review(limit)
