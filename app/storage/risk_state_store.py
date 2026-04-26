import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

_STORE_PATH = Path(__file__).parent / "risk_decisions.jsonl"


def save_risk_decision(
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
    }
    with open(_STORE_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record_id


def _load_all() -> list[dict]:
    if not _STORE_PATH.exists():
        return []
    records = []
    with open(_STORE_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def get_recent_risk_decisions(limit: int = 20) -> list[dict]:
    records = _load_all()
    return sorted(records, key=lambda r: r["timestamp"], reverse=True)[:limit]


REVIEW_REQUIRED_DECISIONS = {"manual_review_required", "review_recommended"}


def get_decisions_requiring_review(limit: int = 10) -> list[dict]:
    records = _load_all()
    filtered = [
        r for r in records
        if r.get("decision", {}).get("decision") in REVIEW_REQUIRED_DECISIONS
    ]
    return sorted(filtered, key=lambda r: r["timestamp"], reverse=True)[:limit]
