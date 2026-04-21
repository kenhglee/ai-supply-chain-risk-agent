import os
import sys
import json
import hmac
import hashlib

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ["GITHUB_WEBHOOK_SECRET"] = "my-test-secret"

from handlers.github_webhook_handler.handler import lambda_handler


payload = {
    "action": "opened",
    "repository": {"full_name": "kenhglee/ai-supply-chain-risk-agent"},
    "sender": {"login": "kenhglee"},
    "pull_request": {
        "number": 1,
        "title": "Test PR",
        "state": "open",
        "head": {
            "ref": "feature/github-risk-evaluation"
        },
        "base": {
            "ref": "main"
        }
    },
}

body = json.dumps(payload)
signature = "sha256=" + hmac.new(
    b"my-test-secret",
    body.encode("utf-8"),
    hashlib.sha256,
).hexdigest()

event = {
    "headers": {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "test-delivery-123",
        "X-Hub-Signature-256": signature,
    },
    "body": body,
    "isBase64Encoded": False,
}

print(lambda_handler(event, None))
