#!/usr/bin/env python3
"""
Command-driven Bedrock authority probe (Phase 3E).

One long-lived process with one Bedrock client and one unchanged STS session.
Each input command causes exactly one Converse request and emits one JSON
result line, so an operator can revoke and restore the executor's Bedrock
authority in IAM (attach / detach an explicit-Deny policy) and observe real
AWS authorization results from the same live process.

USAGE
    python scripts/bedrock_authority_probe.py < commands

    stdin, one JSON object per line:
        {"probe": "<label>"}     one Converse request; label is free text
        {"act": "<label>"}       one governed open_review_ticket admission
                                 through ActionLedger (Phase 3F): emergency
                                 authority + materialized policy + claim in one
                                 DynamoDB transaction, mock ticket effect.
                                 No Converse request.
    blank lines are ignored; anything else gets an invalid_command line and
    no request. EOF ends the session (exit 0).

OUTPUT (stdout, one JSON object per line)
    start          process identity, STS caller identity, credential fingerprint
    probe          seq, label, timestamps, outcome ok | denied | error,
                   AWS error code/message, RequestId, latency, token usage
    invalid_command
    end            STS caller identity and credential fingerprint again

    A start-identity failure exits 2 with no probes.

CREDENTIALS
    Standard boto3 credential chain, resolved once. Intended to run under
    env -i with only an assumed role session's AWS_ACCESS_KEY_ID /
    AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN. Credentials are never printed;
    only a SHA-256 fingerprint of the access key ID is recorded.

ENV
    AWS_DEFAULT_REGION   default us-west-2
    BEDROCK_MODEL_ID     default us.anthropic.claude-haiku-4-5-20251001-v1:0
    AUTHORITY_TABLE, ACTIONS_TABLE   for act; if unset, every act is denied
                                     (fail closed, UNCONFIGURED)
"""
import hashlib
import json
import os
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_REGION = "us-west-2"
PROMPT = "Reply with OK."
MAX_TOKENS = 5
TEMPERATURE = 0.0

# total_max_attempts counts the initial call: 1 means no retries, so every
# authorization result is the result of exactly one request. (botocore's
# "max_attempts" key counts retries only; max_attempts=1 would allow two.)
RETRIES = {"total_max_attempts": 1, "mode": "standard"}
CLIENT_CONFIG = Config(
    retries=RETRIES,
    connect_timeout=10,
    read_timeout=60,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ACT_CAPABILITY = "open_review_ticket"

EXIT_OK = 0
EXIT_NO_IDENTITY = 2

_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def credential_fingerprint(session) -> str | None:
    """Non-secret fingerprint of the session's access key ID (never the key)."""
    credentials = session.get_credentials()
    if credentials is None:
        return None
    access_key = credentials.get_frozen_credentials().access_key
    return "sha256:" + hashlib.sha256(f"aws-access-key-id:{access_key}".encode()).hexdigest()[:16]


def process_identity() -> dict:
    return {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "hostname": socket.gethostname(),
        "started_at": _PROCESS_STARTED_AT,
    }


def caller_identity(sts) -> dict:
    response = sts.get_caller_identity()
    return {"arn": response["Arn"], "account": response["Account"], "user_id": response["UserId"]}


def _error_fields(exc: ClientError) -> dict:
    error = exc.response.get("Error", {})
    meta = exc.response.get("ResponseMetadata", {})
    message = error.get("Message", "")
    return {
        "error_code": error.get("Code"),
        "error_message": message,
        "explicit_deny": "explicit deny" in message.lower(),
        "request_id": meta.get("RequestId"),
        "http_status": meta.get("HTTPStatusCode"),
    }


def converse_once(bedrock, model_id: str) -> dict:
    """Exactly one Converse request; never raises."""
    started = _now()
    t0 = time.monotonic()
    try:
        response = bedrock.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": PROMPT}]}],
            inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
        )
    except ClientError as exc:
        fields = _error_fields(exc)
        outcome = "denied" if fields["error_code"] == "AccessDeniedException" else "error"
        result = {"outcome": outcome, **fields}
    except BotoCoreError as exc:
        result = {"outcome": "error", "error_code": type(exc).__name__, "error_message": str(exc)}
    else:
        content = response.get("output", {}).get("message", {}).get("content", [])
        usage = response.get("usage", {})
        meta = response.get("ResponseMetadata", {})
        result = {
            "outcome": "ok",
            "request_id": meta.get("RequestId"),
            "http_status": meta.get("HTTPStatusCode"),
            "output_text": "".join(block.get("text", "") for block in content),
            "stop_reason": response.get("stopReason"),
            "input_tokens": usage.get("inputTokens"),
            "output_tokens": usage.get("outputTokens"),
            "bedrock_latency_ms": response.get("metrics", {}).get("latencyMs"),
        }
    return {
        "started_at": started,
        "ended_at": _now(),
        "latency_ms": round((time.monotonic() - t0) * 1000, 1),
        **result,
    }


def parse_command(line: str) -> tuple[str, str]:
    """Return (command, label) for {"probe": "<label>"} or {"act": "<label>"}; raise ValueError otherwise."""
    try:
        command = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON: {exc.msg}") from exc
    if not isinstance(command, dict) or len(command) != 1 or not set(command) <= {"probe", "act"}:
        raise ValueError('expected exactly {"probe": "<label>"} or {"act": "<label>"}')
    [(name, label)] = command.items()
    if not isinstance(label, str):
        raise ValueError(f"{name} label must be a string")
    return name, label


def build_gate_from_env():
    """ActionLedger from AUTHORITY_TABLE / ACTIONS_TABLE, or a fail-closed gate."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from app.governance.action_ledger import action_gate_from_env
    return action_gate_from_env()


def act_once(gate, *, action_id: str, thread_id: str, executor_id: str) -> dict:
    """Exactly one governed admission attempt for open_review_ticket; never raises."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from app.governance.action_ledger import ActionInFlight
    from app.governance.authority import AGENT_SCOPE, AuthorityDenied
    from app.integrations.servicenow_mock import create_servicenow_ticket

    def effect() -> dict:
        ticket = create_servicenow_ticket(
            {"event_type": "governance_probe", "repository": None},
            {"decision": "review_recommended", "risk_score": 50, "reason": f"policy probe {action_id}"},
        )
        return {**ticket, "correlation_id": action_id}

    started = _now()
    t0 = time.monotonic()
    try:
        result = gate.execute(
            action_id,
            scope=AGENT_SCOPE,
            context={"action_type": ACT_CAPABILITY, "thread_id": thread_id, "trace_id": uuid.uuid4().hex,
                     "executor_id": executor_id, "subject": {"probe": True}},
            effect=effect,
        )
        fields = {
            "outcome": "ok",
            "action_status": result["status"],
            "replayed": result["replayed"],
            "authority_version": result.get("authority_version"),
            "policy_version": result.get("policy_version"),
            "policy_sha256": result.get("policy_sha256"),
            "ticket_id": (result.get("effect") or {}).get("ticket_id"),
        }
    except AuthorityDenied as denial:
        fields = {
            "outcome": "denied",
            "action_status": "DENIED",
            "denied_layer": denial.layer,
            "denial_status": denial.status,
            "authority_status": denial.authority_status,
            "authority_version": denial.authority_version,
            "policy_status": denial.policy_status,
            "policy_version": denial.policy_version,
            "policy_sha256": denial.policy_sha256,
        }
    except ActionInFlight as exc:
        fields = {"outcome": "error", "error_code": "ActionInFlight", "error_message": str(exc)}
    except (ClientError, BotoCoreError) as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        fields = {"outcome": "error", "error_code": code, "error_message": str(exc)}
    return {
        "action_id": action_id,
        "capability": ACT_CAPABILITY,
        "started_at": started,
        "ended_at": _now(),
        "latency_ms": round((time.monotonic() - t0) * 1000, 1),
        **fields,
    }


def run(lines, *, bedrock, sts, fingerprint, model_id: str, region: str, out, gate=None) -> int:
    def emit(record: dict) -> None:
        print(json.dumps(record), file=out, flush=True)

    try:
        identity = caller_identity(sts)
    except (ClientError, BotoCoreError) as exc:
        emit({"event": "start_failed", "at": _now(), "process": process_identity(),
              "error_code": getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__),
              "error_message": str(exc)})
        return EXIT_NO_IDENTITY

    emit({
        "event": "start",
        "at": _now(),
        "process": process_identity(),
        "identity": identity,
        "credential_fingerprint": fingerprint(),
        "model_id": model_id,
        "region": region,
        "request": {"prompt": PROMPT, "max_tokens": MAX_TOKENS, "temperature": TEMPERATURE},
        "retries": RETRIES,
    })

    seq = probes = acts = 0
    # Unique per run() so act action_ids never collide with an earlier session's
    # (a collision would correctly replay that session's COMPLETED action).
    session_tag = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            command, label = parse_command(line)
        except ValueError as exc:
            emit({"event": "invalid_command", "at": _now(), "error": str(exc)})
            continue
        seq += 1
        base = {"seq": seq, "label": label, "pid": os.getpid(), "credential_fingerprint": fingerprint()}
        if command == "probe":
            probes += 1
            emit({"event": "probe", **base, "model_id": model_id, **converse_once(bedrock, model_id)})
        else:
            acts += 1
            if gate is None:
                gate = build_gate_from_env()
            emit({"event": "act", **base, **act_once(
                gate,
                action_id=f"action#probe-{session_tag}-{seq}#{ACT_CAPABILITY}",
                thread_id=f"probe-{session_tag}",
                executor_id=f"{socket.gethostname()}:{os.getpid()}",
            )})

    end = {"event": "end", "at": _now(), "process": process_identity(), "probes": probes, "acts": acts,
           "credential_fingerprint": fingerprint()}
    try:
        end["identity"] = caller_identity(sts)
    except (ClientError, BotoCoreError) as exc:
        end["identity_error"] = str(exc)
    emit(end)
    return EXIT_OK


def main() -> int:
    region = os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)
    model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    # One session, one credential resolution, one client per service for the
    # life of the process.
    session = boto3.Session(region_name=region)
    bedrock = session.client("bedrock-runtime", config=CLIENT_CONFIG)
    sts = session.client("sts", config=CLIENT_CONFIG)
    return run(
        sys.stdin,
        bedrock=bedrock,
        sts=sts,
        fingerprint=lambda: credential_fingerprint(session),
        model_id=model_id,
        region=region,
        out=sys.stdout,
    )


if __name__ == "__main__":
    sys.exit(main())
