import json
from pathlib import Path

_STORE_PATH = Path(__file__).parent / "risk_traces.jsonl"


def append_risk_trace(record: dict) -> None:
    with open(_STORE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def get_risk_trace_by_identifier(identifier: str) -> dict | None:
    """Return the most recent trace record where alert_id or trace_id matches identifier."""
    if not _STORE_PATH.exists():
        return None
    matches = []
    with open(_STORE_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("alert_id") == identifier or record.get("trace_id") == identifier:
                matches.append(record)
    if not matches:
        return None
    return sorted(matches, key=lambda r: r.get("created_at", ""), reverse=True)[0]
