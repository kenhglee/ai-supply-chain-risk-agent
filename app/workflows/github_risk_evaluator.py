def evaluate_github_event_risk(normalized_event):
    event_type = normalized_event.get("event_type")

    if event_type == "push":
        push = normalized_event.get("push") or {}
        ref = push.get("ref", "")

        if ref == "refs/heads/main":
            return {
                "decision": "manual_review_required",
                "risk_score": 80,
                "reason": "direct push to main branch",
            }

        if ref.startswith("refs/heads/release/"):
            return {
                "decision": "manual_review_required",
                "risk_score": 85,
                "reason": "push to release branch",
            }

        return {
            "decision": "allow",
            "risk_score": 20,
            "reason": "push to non-critical branch",
        }

    if event_type == "pull_request":
        pr = normalized_event.get("pull_request") or {}
        base_ref = pr.get("base_ref", "")

        if base_ref == "main":
            return {
                "decision": "review_recommended",
                "risk_score": 60,
                "reason": "pull request targets main branch",
            }

        if base_ref.startswith("release/"):
            return {
                "decision": "manual_review_required",
                "risk_score": 75,
                "reason": "pull request targets release branch",
            }

        return {
            "decision": "allow",
            "risk_score": 25,
            "reason": "pull request targets non-critical branch",
        }

    return {
        "decision": "ignore",
        "risk_score": 0,
        "reason": f"unsupported event type: {event_type}",
    }
