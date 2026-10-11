import logging
import os
import json
import re

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

from app.storage.checkpoint_store import DynamoCheckpointSaver
from app.storage.supplier_risk_state_store import get_risk_store
from app.workflows.risk_workflow import NOT_FOUND, resume_workflow, start_workflow
from app.workflows.supplier_risk_agent import get_executor_id, run_supplier_risk_flow

# ---- Checkpoint demo actions (Phase 3A3) ----
# Events carrying an "action" key are explicit checkpoint lifecycle requests;
# every other event (scheduled EventBridge, manual) runs the RSS pipeline.

CHECKPOINT_START = "checkpoint_start"
CHECKPOINT_RESUME = "checkpoint_resume"
START_PAUSE_BEFORE = ["analyze"]
THREAD_ID_PATTERN = re.compile(r"thread-[0-9a-f]{32}")

_OUTCOME_FIELDS = (
    "status", "thread_id", "trace_id", "next", "change_type",
    "state_written", "final_status", "supplier",
)


class InvalidCheckpointRequest(ValueError):
    pass


def _response(status_code: int, body: dict) -> dict:
    return {"statusCode": status_code, "body": json.dumps(body)}


def _require_keys(event: dict, expected: set[str]) -> None:
    keys = set(event)
    if keys != expected:
        details = []
        if missing := sorted(expected - keys):
            details.append(f"missing {missing}")
        if unexpected := sorted(keys - expected):
            details.append(f"unexpected {unexpected}")
        raise InvalidCheckpointRequest(
            f"{event['action']} requires exactly {sorted(expected)}: {', '.join(details)}"
        )


def _parse_checkpoint_action(event: dict) -> tuple[str, str]:
    """Validate a checkpoint action event; returns (action, headline or thread_id)."""
    action = event.get("action")

    if action == CHECKPOINT_START:
        _require_keys(event, {"action", "headline"})
        headline = event["headline"]
        if not isinstance(headline, str) or not headline.strip():
            raise InvalidCheckpointRequest("headline must be a non-empty string")
        return action, headline.strip()

    if action == CHECKPOINT_RESUME:
        # RESUME takes only the workflow identity; no new workflow input.
        _require_keys(event, {"action", "thread_id"})
        thread_id = event["thread_id"]
        if not isinstance(thread_id, str) or not THREAD_ID_PATTERN.fullmatch(thread_id):
            raise InvalidCheckpointRequest("thread_id must match thread-<32 lowercase hex>")
        return action, thread_id

    raise InvalidCheckpointRequest(
        f"Unknown action {action!r}; expected {CHECKPOINT_START!r} or {CHECKPOINT_RESUME!r}"
    )


def _checkpoint_dependencies() -> dict:
    """Built fresh for every invocation: continuity comes only from DynamoDB + thread_id."""
    table_name = os.environ.get("CHECKPOINT_TABLE")
    if not table_name:
        raise RuntimeError("CHECKPOINT_TABLE is not configured")
    return {
        "checkpointer": DynamoCheckpointSaver(table_name),
        "risk_store": get_risk_store(),
    }


def _handle_checkpoint_action(event: dict, request_id: str | None) -> dict:
    try:
        action, value = _parse_checkpoint_action(event)
    except InvalidCheckpointRequest as exc:
        logger.info(json.dumps({
            "stage": "checkpoint_action_rejected",
            "request_id": request_id,
            "action": event.get("action"),
            "error": str(exc),
        }))
        return _response(400, {"action": event.get("action"), "error": str(exc), "request_id": request_id})

    logger.info(json.dumps({
        "stage": "checkpoint_action_start",
        "request_id": request_id,
        "action": action,
        "thread_id": value if action == CHECKPOINT_RESUME else None,
    }))

    deps = _checkpoint_dependencies()
    if action == CHECKPOINT_START:
        outcome = start_workflow({"headline": value}, pause_before=list(START_PAUSE_BEFORE), **deps)
    else:
        outcome = resume_workflow(value, **deps)

    body = {
        "action": action,
        **{field: outcome.get(field) for field in _OUTCOME_FIELDS},
        "executor_id": get_executor_id(),
        "request_id": request_id,
    }
    logger.info(json.dumps({"stage": "checkpoint_action_complete", **body}))
    return _response(404 if outcome["status"] == NOT_FOUND else 200, body)


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", None)

    logger.info(json.dumps({
        "stage": "lambda_start",
        "request_id": request_id,
        "event": event,
    }))

    try:
        if isinstance(event, dict) and "action" in event:
            return _handle_checkpoint_action(event, request_id)

        summary = run_supplier_risk_flow(event)

        logger.info(json.dumps({
            "stage": "lambda_complete",
            "request_id": request_id,
            "summary": summary,
        }))

        return {
            "statusCode": 200,
            "body": json.dumps(summary),
        }

    except Exception as exc:
        logger.exception(json.dumps({
            "stage": "lambda_failed",
            "request_id": request_id,
            "error": str(exc),
        }))
        raise
