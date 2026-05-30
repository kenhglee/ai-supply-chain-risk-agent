import json
from pathlib import Path

_STORE_PATH = Path(__file__).parent / "risk_traces.jsonl"


def append_risk_trace(record: dict) -> None:
    with open(_STORE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _load_all_traces() -> list[dict]:
    if not _STORE_PATH.exists():
        return []
    records = []
    with open(_STORE_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def get_all_traces() -> list[dict]:
    """Return all trace records, newest first."""
    return sorted(_load_all_traces(), key=lambda r: r.get("created_at", ""), reverse=True)


def get_risk_trace_by_identifier(identifier: str) -> dict | None:
    """Return the most recent trace record where alert_id or trace_id matches identifier."""
    matches = [
        r for r in _load_all_traces()
        if r.get("alert_id") == identifier or r.get("trace_id") == identifier
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda r: r.get("created_at", ""), reverse=True)[0]


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

    return "\n".join(lines)
