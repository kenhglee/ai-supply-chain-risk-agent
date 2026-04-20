import json
import logging

from app.ingestion.github_webhook_receiver import process_github_webhook

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", None)

    logger.info(json.dumps({
        "stage": "lambda_start",
        "request_id": request_id,
    }))

    try:
        response = process_github_webhook(event)

        logger.info(json.dumps({
            "stage": "lambda_complete",
            "request_id": request_id,
            "status_code": response.get("statusCode"),
        }))

        return response

    except Exception as exc:
        logger.exception(json.dumps({
            "stage": "lambda_failed",
            "request_id": request_id,
            "error": str(exc),
        }))
        raise
