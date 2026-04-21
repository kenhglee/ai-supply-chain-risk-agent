import base64
import hashlib
import hmac
import json
import logging
import os
from app.workflows.github_risk_evaluator import evaluate_github_event_risk

logger = logging.getLogger()
logger.setLevel(logging.INFO)

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "body": json.dumps(body),
    }


def _get_header(headers, name):
    headers = headers or {}
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _get_raw_body(event):
    body = event.get("body", "")
    if event.get("isBase64Encoded", False):
        return base64.b64decode(body)
    return body.encode("utf-8")


def _verify_signature(raw_body, signature_header):
    if not GITHUB_WEBHOOK_SECRET:
        raise ValueError("GITHUB_WEBHOOK_SECRET is not set")

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    received = signature_header.split("=", 1)[1]
    expected = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(received, expected)


def process_github_webhook(event):
    headers = event.get("headers", {}) or {}

    github_event = _get_header(headers, "X-GitHub-Event")
    delivery_id = _get_header(headers, "X-GitHub-Delivery")
    signature = _get_header(headers, "X-Hub-Signature-256")

    raw_body = _get_raw_body(event)

    if not github_event:
        return _response(400, {"error": "Missing X-GitHub-Event header"})

    if not _verify_signature(raw_body, signature):
        return _response(401, {"error": "Invalid signature"})

    payload = json.loads(raw_body.decode("utf-8"))

    normalized = {
        "delivery_id": delivery_id,
        "event_type": github_event,
        "action": payload.get("action"),
        "repository": (payload.get("repository") or {}).get("full_name"),
        "sender": (payload.get("sender") or {}).get("login"),
    }

    if github_event == "pull_request":
        pr = payload.get("pull_request") or {}
        normalized["pull_request"] = {
            "number": pr.get("number"),
            "title": pr.get("title"),
            "state": pr.get("state"),
            "head_ref": (pr.get("head") or {}).get("ref"),
            "base_ref": (pr.get("base") or {}).get("ref"),
        }

    elif github_event == "push":
        normalized["push"] = {
            "ref": payload.get("ref"),
            "before": payload.get("before"),
            "after": payload.get("after"),
            "commits_count": len(payload.get("commits", []) or []),
        }

    decision = evaluate_github_event_risk(normalized)

    logger.info(json.dumps({
        "stage": "github_webhook_received",
        "normalized_event": normalized,
        "decision": decision,
    }))

    return _response(200, {
        "message": "GitHub webhook received",
        "normalized_event": normalized,
        "decision": decision,
    })
