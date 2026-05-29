from mcp.server.fastmcp import FastMCP

from app.integrations.servicenow_mock import create_servicenow_ticket as _create_ticket
from app.storage.risk_state_store import (
    get_decisions_requiring_review as _get_requiring_review,
    get_recent_risk_decisions as _get_recent,
    save_risk_decision,
)
from app.storage.risk_trace_store import get_risk_trace_by_identifier as _get_risk_trace
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
def create_mock_servicenow_ticket(
    event_type: str,
    repository: str,
    decision: str,
    risk_score: int,
    reason: str,
    ref: str = "",
    base_ref: str = "",
    pr_number: int = 0,
    pr_title: str = "",
) -> dict:
    """Create a mock ServiceNow CHANGE ticket for a risk decision and persist the result.

    Args:
        event_type: "push" or "pull_request"
        repository: full repo name, e.g. "org/repo"
        decision: risk decision string, e.g. "manual_review_required"
        risk_score: integer risk score 0-100
        reason: human-readable reason for the decision
        ref: branch ref for push events
        base_ref: target branch for pull_request events
        pr_number: pull request number (pull_request events only)
        pr_title: pull request title (pull_request events only)
    """
    normalized = _build_normalized_event(event_type, repository, ref, base_ref, pr_number, pr_title)
    decision_dict = {"decision": decision, "risk_score": risk_score, "reason": reason}
    ticket = _create_ticket(normalized, decision_dict)
    save_risk_decision(normalized, decision_dict, ticket)
    return ticket


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


if __name__ == "__main__":
    mcp.run()
