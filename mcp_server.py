from mcp.server.fastmcp import FastMCP

from app.storage.risk_state_store import (
    get_decisions_requiring_review as _get_requiring_review,
    get_recent_risk_decisions as _get_recent,
    save_risk_decision,
)
from app.storage.risk_trace_store import get_risk_trace_by_identifier as _get_risk_trace
from app.storage.risk_trace_store import trace_display_status
from app.workflows.decision_ticket_action import open_ticket_for_decision as _open_ticket_for_decision
from app.workflows.github_risk_evaluator import evaluate_github_event_risk as _evaluate_risk

mcp = FastMCP("supply-chain-risk")


def _build_normalized_event(
    event_type: str,
    repository: str,
    ref: str,
    base_ref: str,
    pr_number: int,
    pr_title: str,
) -> dict:
    normalized: dict = {"event_type": event_type, "repository": repository}
    if event_type == "push":
        normalized["push"] = {"ref": ref}
    elif event_type == "pull_request":
        normalized["pull_request"] = {
            "base_ref": base_ref,
            "number": pr_number or None,
            "title": pr_title or None,
        }
    return normalized


@mcp.tool()
def evaluate_github_event_risk(
    event_type: str,
    repository: str,
    ref: str = "",
    base_ref: str = "",
    pr_number: int = 0,
    pr_title: str = "",
) -> dict:
    """Evaluate the risk of a GitHub push or pull_request event and persist the decision.

    Args:
        event_type: "push" or "pull_request"
        repository: full repo name, e.g. "org/repo"
        ref: branch ref for push events, e.g. "refs/heads/main"
        base_ref: target branch for pull_request events, e.g. "main"
        pr_number: pull request number (pull_request events only)
        pr_title: pull request title (pull_request events only)
    """
    normalized = _build_normalized_event(event_type, repository, ref, base_ref, pr_number, pr_title)
    decision = _evaluate_risk(normalized)
    record_id = save_risk_decision(normalized, decision)
    return {"record_id": record_id, **decision}


@mcp.tool()
def get_recent_risk_decisions(limit: int = 20) -> list[dict]:
    """Return the most recent persisted risk decisions, newest first.

    Args:
        limit: maximum number of records to return (default 20)
    """
    return _get_recent(limit)


@mcp.tool()
def create_mock_servicenow_ticket(decision_id: str) -> dict:
    """Open a mock ServiceNow CHANGE ticket for a stored risk decision.

    All ticket fields come from the stored decision. The decision must have
    been produced by evaluate_github_event_risk and require review
    (review_recommended or manual_review_required). The ticket opens only if
    runtime authority and policy allow it. Repeat calls for the same decision
    return the original ticket (status "already_created") instead of creating
    another. Unknown, ineligible or denied requests return status "rejected"
    or "denied" with a reason, and create nothing.

    Args:
        decision_id: the record_id returned by evaluate_github_event_risk
            (the "id" field of get_recent_risk_decisions results)
    """
    return _open_ticket_for_decision(decision_id)


@mcp.tool()
def get_decisions_requiring_review(limit: int = 10) -> list[dict]:
    """Return decisions that require human review, newest first.

    Args:
        limit: maximum number of records to return (default 10)
    """
    return _get_requiring_review(limit)


@mcp.tool()
def get_risk_trace(identifier: str) -> dict:
    """Retrieve a persisted supplier risk trace by alert_id or trace_id.

    Args:
        identifier: the alert_id or trace_id to look up
    """
    record = _get_risk_trace(identifier)
    if record is None:
        return {
            "found": False,
            "identifier": identifier,
            "message": f"No risk trace found for identifier: {identifier}",
        }
    matched_by = "alert_id" if record.get("alert_id") == identifier else "trace_id"
    return {
        "found": True,
        "identifier": identifier,
        "matched_by": matched_by,
        "trace": record,
    }


def _build_explanation(record: dict) -> str:
    alert_id = record.get("alert_id") or "(unknown)"
    trace_id = record.get("trace_id") or "(unknown)"
    short_trace = trace_id[:12] + "..." if len(trace_id) > 12 else trace_id
    headline = record.get("headline") or "(none)"
    status = trace_display_status(record)
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
        f"Status:      {status}",
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


@mcp.tool()
def explain_risk_trace(identifier: str) -> dict:
    """Return a human-readable explanation of a persisted supplier risk trace.

    Summarises the alert outcome, node sequence, slowest node, and any
    decisions or errors captured during the LangGraph run.

    Args:
        identifier: the alert_id or trace_id to look up
    """
    record = _get_risk_trace(identifier)
    if record is None:
        return {
            "found": False,
            "identifier": identifier,
            "message": f"No risk trace found for identifier: {identifier}",
        }
    matched_by = "alert_id" if record.get("alert_id") == identifier else "trace_id"
    return {
        "found": True,
        "identifier": identifier,
        "matched_by": matched_by,
        "explanation": _build_explanation(record),
    }


if __name__ == "__main__":
    mcp.run()
