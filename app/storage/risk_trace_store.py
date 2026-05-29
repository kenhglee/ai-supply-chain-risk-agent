import json
from pathlib import Path

_STORE_PATH = Path(__file__).parent / "risk_traces.jsonl"


def append_risk_trace(record: dict) -> None:
    with open(_STORE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
