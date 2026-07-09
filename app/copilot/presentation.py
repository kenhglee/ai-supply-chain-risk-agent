"""Plain-text rendering for planner copilot responses.

Pure formatting functions only — no ranking, matching, retrieval, or
persistence logic lives here. Handlers in planner_copilot.py compute
structured data (ranked suppliers, matched evidence, decision records)
and pass it in; this module turns that data into the string a CLI (or,
later, an MCP tool or web decision workflow) shows the user. Keeping it
isolated means a future surface can reuse these functions as-is, or
swap in a structured (JSON) renderer without touching business logic.
"""
import textwrap

_LINE_WIDTH = 78
_VALUE_COLUMN = 25


def _badge_line(rank: int, supplier: str, risk_level: str) -> str:
    left = f"{rank}. {supplier}"
    badge = f"{(risk_level or '').upper()} RISK"
    padding = max(1, _LINE_WIDTH - len(left) - len(badge))
    return f"{left}{' ' * padding}{badge}"


def _field_line(label: str, value: str) -> str:
    prefix = f"   {label}"
    pad = " " * max(1, _VALUE_COLUMN - len(prefix))
    wrap_width = max(20, _LINE_WIDTH - _VALUE_COLUMN)
    wrapped = textwrap.wrap(value, width=wrap_width) or [""]
    first, *rest = wrapped
    lines = [f"{prefix}{pad}{first}"]
    continuation = " " * _VALUE_COLUMN
    lines.extend(f"{continuation}{line}" for line in rest)
    return "\n".join(lines)


def format_monitor_briefing(suppliers: list[dict]) -> str:
    """Render the ranked-supplier briefing for the 'monitor' turn."""
    if not suppliers:
        return "No supplier risk signals found. Run the RSS pipeline first to populate the risk state ledger."

    count = len(suppliers)
    lines = [f"Supplier Risk Briefing — {count} supplier{'' if count == 1 else 's'} requiring attention"]

    for i, s in enumerate(suppliers, start=1):
        signal_count = s["risk_signal_count"]
        plural = "" if signal_count == 1 else "s"
        confidence_value = f"{s['confidence']} ({signal_count} corroborating signal{plural})"

        lines.append("")
        lines.append(_badge_line(i, s["supplier"], s["risk_level"]))
        lines.append(_field_line("business impact:", s["business_impact"]))
        lines.append(_field_line("confidence:", confidence_value))
        lines.append(_field_line("➜ recommended action:", s["recommended_action"]))

    top_supplier = suppliers[0]["supplier"]
    lines.append("")
    lines.append(
        f'Ask "Why {top_supplier}?" for the evidence behind a recommendation, '
        f'or say "Approve review" to act on the top-priority supplier.'
    )
    return "\n".join(lines)


def format_why_response(
    matched: dict | None,
    targets: list[dict],
    others: list[dict],
    created_at: str,
    context: str | None,
) -> str:
    """Render the 'why' turn — scoped to one supplier if matched, else all."""
    lines: list[str] = []

    if matched:
        lines.append(f"Evidence for {matched['supplier']}:")
        if others:
            other_desc = ", ".join(
                f"{o['supplier']} ({o['risk_level']} risk, {o['risk_signal_count']} signal(s))"
                for o in others
            )
            lines.append("")
            lines.append("Recommendation")
            lines.append(
                f"  Prioritized above {other_desc} because {matched['supplier']}'s risk level "
                f"and signal count rank higher."
            )
        lines.append("")
        lines.append("Supporting evidence")
    else:
        lines.append(f"Evidence behind the recommendation from {created_at}:")
        lines.append("")

    for s in targets:
        lines.append(
            f"  - {s['supplier']}: {s['risk_level']} risk from '{s['risk_type']}' — "
            f"\"{s['last_headline']}\" (seen {s['last_seen_at']}, {s['risk_signal_count']} signal(s) on record)"
        )
        if s.get("exposures"):
            lines.append(f"    Structural exposure: {', '.join(s['exposures'])}")
        if s.get("business_impact"):
            lines.append(f"    Business impact: {s['business_impact']}")
        if s.get("confidence"):
            lines.append(f"    Confidence: {s['confidence']}")
        if s.get("profile"):
            lines.append(f"    Profile: {s['profile']}")
        lines.append("")

    if context:
        lines.append("Retrieved context:")
        lines.append(context)
        lines.append("")

    if matched:
        lines.append(f'Say "Approve review" to move {matched["supplier"]} into formal review.')

    return "\n".join(lines).rstrip()


def format_approval_confirmation(supplier: dict, record_id: str) -> str:
    """Render the 'approve review' confirmation — workflow state, not just a record id."""
    name = supplier["supplier"]
    risk_level = supplier["risk_level"]

    lines = [
        "Decision recorded",
        f"  Approved review for {name} ({risk_level} risk).",
        "",
        "Workflow state",
        "  Recommended → Explained → Approved → Awaiting ticket assignment",
        "",
        "What's next",
        f'  • Say "create a review ticket" to open a ServiceNow change ticket for {name}',
        '  • Ask "which suppliers require attention today?" for a refreshed briefing',
        "",
        f"Audit reference: {record_id}",
    ]
    return "\n".join(lines)
