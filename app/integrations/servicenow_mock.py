import uuid
from datetime import datetime, timezone


def create_servicenow_ticket(normalized_event, decision):
    event_type = normalized_event.get("event_type")
    repository = normalized_event.get("repository")

    branch = None
    pr_number = None
    pr_title = None

    if event_type == "pull_request":
        pr = normalized_event.get("pull_request") or {}
        branch = pr.get("base_ref")
        pr_number = pr.get("number")
        pr_title = pr.get("title")

    elif event_type == "push":
        push = normalized_event.get("push") or {}
        branch = push.get("ref")

    return {
        "ticket_id": f"MOCK-CHG-{uuid.uuid4().hex[:8].upper()}",
        "status": "created",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "category": "software_supply_chain",
        "subcategory": "github_event",
        "short_description": "Software supply chain review required",
        "description": decision.get("reason"),
        "repository": repository,
        "event_type": event_type,
        "branch": branch,
        "pull_request_number": pr_number,
        "pull_request_title": pr_title,
        "risk_score": decision.get("risk_score"),
        "decision": decision.get("decision"),
    }
