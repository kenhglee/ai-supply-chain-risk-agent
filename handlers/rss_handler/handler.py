import logging
import os
import json

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

from app.workflows.supplier_risk_agent import run_supplier_risk_flow

def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", None)

    logger.info(json.dumps({
        "stage": "lambda_start",
        "request_id": request_id,
        "event": event,
    }))

    try:
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