import json
import os
from decimal import Decimal
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

_STORE_PATH = Path(__file__).parent / "risk_traces.jsonl"


# ---- JSONL implementation ----

class JsonlTraceStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, record: dict) -> None:
        out = dict(record)
        out["store_backend"] = "jsonl"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(out) + "\n")

    def _load_all(self) -> list[dict]:
        if not self._path.exists():
            return []
        records = []
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def get_all(self) -> list[dict]:
        return sorted(self._load_all(), key=lambda r: r.get("created_at", ""), reverse=True)

    def get_by_identifier(self, identifier: str) -> dict | None:
        matches = [
            r for r in self._load_all()
            if r.get("alert_id") == identifier or r.get("trace_id") == identifier
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda r: r.get("created_at", ""), reverse=True)[0]


# ---- DynamoDB implementation ----

class DynamoTraceStore:
    """
    Table schema:
      PK: trace_id (S)
      GSI alert-id-index: PK=alert_id (S), Projection=KEYS_ONLY

    Scalar top-level fields stored as native DynamoDB attributes.
    Nested blobs (trace_steps, *_metadata) stored as JSON strings.
    """

    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    @staticmethod
    def _to_dynamo_item(record: dict) -> dict:
        item: dict = {
            "trace_id": record["trace_id"],
            "alert_id": record["alert_id"],
            "created_at": record["created_at"],
            "headline": record["headline"],
            "run_duration_ms": Decimal(str(record.get("run_duration_ms", 0))),
            "change_type": record.get("change_type", ""),
            "store_backend": "dynamodb",
            "trace_steps": json.dumps(record.get("trace_steps") or []),
        }
        for field in ("tool_decision", "final_status", "supplier", "risk_type", "risk_level"):
            val = record.get(field)
            if val is not None:
                item[field] = val
        for field in ("retriever_metadata", "prompt_metadata", "model_metadata"):
            val = record.get(field)
            if val is not None:
                item[field] = json.dumps(val)
        return item

    @staticmethod
    def _from_dynamo_item(item: dict) -> dict:
        record = dict(item)
        if "run_duration_ms" in record:
            record["run_duration_ms"] = float(record["run_duration_ms"])
        record["trace_steps"] = json.loads(record.get("trace_steps", "[]"))
        for field in ("retriever_metadata", "prompt_metadata", "model_metadata"):
            if field in record:
                record[field] = json.loads(record[field])
        for field in ("tool_decision", "final_status", "supplier", "risk_type", "risk_level"):
            if field not in record:
                record[field] = None
        return record

    def _scan_all(self) -> list[dict]:
        items: list[dict] = []
        kwargs: dict = {}
        while True:
            response = self._table.scan(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return [self._from_dynamo_item(item) for item in items]

    def append(self, record: dict) -> None:
        self._table.put_item(Item=self._to_dynamo_item(record))

    def get_all(self) -> list[dict]:
        return sorted(self._scan_all(), key=lambda r: r.get("created_at", ""), reverse=True)

    def get_by_identifier(self, identifier: str) -> dict | None:
        # Try PK lookup first (trace_id)
        response = self._table.get_item(Key={"trace_id": identifier})
        item = response.get("Item")
        if item:
            return self._from_dynamo_item(item)
        # Fall back to GSI query by alert_id (KEYS_ONLY: returns trace_id + alert_id)
        response = self._table.query(
            IndexName="alert-id-index",
            KeyConditionExpression=Key("alert_id").eq(identifier),
        )
        gsi_items = response.get("Items", [])
        if not gsi_items:
            return None
        records = []
        for gsi_item in gsi_items:
            r = self._table.get_item(Key={"trace_id": gsi_item["trace_id"]})
            full = r.get("Item")
            if full:
                records.append(self._from_dynamo_item(full))
        if not records:
            return None
        return sorted(records, key=lambda r: r.get("created_at", ""), reverse=True)[0]


# ---- Factory and lazy accessor ----

def get_trace_store() -> JsonlTraceStore | DynamoTraceStore:
    backend = os.getenv("TRACE_STORE_BACKEND", "jsonl").lower()
    if backend == "jsonl":
        return JsonlTraceStore(_STORE_PATH)
    if backend == "dynamodb":
        return DynamoTraceStore(
            table_name=os.getenv("RISK_TRACES_TABLE", "risk_traces"),
            region=os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
        )
    raise ValueError(f"Unsupported TRACE_STORE_BACKEND: '{backend}'")


_store: JsonlTraceStore | DynamoTraceStore | None = None


def _get_trace_store() -> JsonlTraceStore | DynamoTraceStore:
    global _store
    if _store is None:
        _store = get_trace_store()
    return _store


# ---- Public API (signatures unchanged) ----

def append_risk_trace(record: dict) -> None:
    _get_trace_store().append(record)


def get_all_traces() -> list[dict]:
    return _get_trace_store().get_all()


def get_risk_trace_by_identifier(identifier: str) -> dict | None:
    return _get_trace_store().get_by_identifier(identifier)


def build_trace_explanation(record: dict) -> str:
    """Return a human-readable explanation of a trace record."""
    alert_id = record.get("alert_id") or "(unknown)"
    trace_id = record.get("trace_id") or "(unknown)"
    short_trace = trace_id[:12] + "..." if len(trace_id) > 12 else trace_id
    headline = record.get("headline") or "(none)"
    final_status = record.get("final_status") or "(unknown)"
    supplier = record.get("supplier") or "(none)"
    risk_type = record.get("risk_type") or "(none)"
    risk_level = record.get("risk_level") or "(none)"
    change_type = record.get("change_type") or "(none)"
    run_duration_ms = record.get("run_duration_ms", 0)
    steps = record.get("trace_steps") or []

    lines = [
        f"Alert {alert_id}  (trace: {short_trace})",
        f"Headline: {headline}",
        "",
        f"Status:      {final_status}",
        f"Supplier:    {supplier}",
        f"Risk type:   {risk_type}",
        f"Risk level:  {risk_level}",
        f"Change:      {change_type}",
        "",
    ]

    if not steps:
        lines.append(f"Run: {run_duration_ms}ms  (filtered before LangGraph — no node trace)")
    else:
        slowest = max(steps, key=lambda s: s.get("duration_ms", 0))
        node_seq = " → ".join(s["node_name"] for s in steps)
        lines += [
            f"Run: {run_duration_ms}ms across {len(steps)} nodes",
            f"  {node_seq}",
            f"  Slowest: {slowest['node_name']} ({slowest.get('duration_ms', 0)}ms)",
        ]

        decisions = [(s["node_name"], s["decision"]) for s in steps if "decision" in s]
        errors = [(s["node_name"], s["error"]) for s in steps if "error" in s]

        if decisions:
            lines.append("")
            lines.append("Decisions:")
            for node, decision in decisions:
                lines.append(f"  {node:<10} → {decision}")

        if errors:
            lines.append("")
            lines.append("Errors:")
            for node, error in errors:
                lines.append(f"  {node:<10} → {error}")

    prompt_meta = record.get("prompt_metadata")
    if prompt_meta:
        lines.append("")
        lines.append("Prompts:")
        for p in prompt_meta:
            pid = p.get("prompt_id", "?")
            ver = p.get("prompt_version", "?")
            status = p.get("prompt_status", "?")
            desc = p.get("prompt_description", "")
            lines.append(f"  {pid} {ver} ({status})")
            if desc:
                lines.append(f"    {desc}")

    model_meta = record.get("model_metadata")
    if model_meta:
        lines.append("")
        lines.append("Models:")
        for m in model_meta:
            mid = m.get("model_id", "?")
            ver = m.get("model_version", "?")
            status = m.get("model_status", "?")
            provider = m.get("model_provider", "?")
            name = m.get("model_name", "?")
            desc = m.get("model_description", "")
            lines.append(f"  {mid} {ver} ({status})  [{provider}/{name}]")
            if desc:
                lines.append(f"    {desc}")

    return "\n".join(lines)
