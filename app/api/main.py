from fastapi import FastAPI, HTTPException

from app.storage.risk_trace_store import (
    build_trace_explanation,
    get_all_traces,
    get_risk_trace_by_identifier,
)

app = FastAPI(title="Supply Chain Risk Trace API")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/traces")
def list_traces():
    return [
        {
            "alert_id": r.get("alert_id"),
            "trace_id": r.get("trace_id"),
            "headline": r.get("headline"),
            "final_status": r.get("final_status"),
            "created_at": r.get("created_at"),
            "run_duration_ms": r.get("run_duration_ms"),
        }
        for r in get_all_traces()
    ]


@app.get("/api/traces/{identifier}")
def get_trace(identifier: str):
    record = get_risk_trace_by_identifier(identifier)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No risk trace found for identifier: {identifier}")
    return record


@app.get("/api/traces/{identifier}/explanation")
def get_trace_explanation(identifier: str):
    record = get_risk_trace_by_identifier(identifier)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No risk trace found for identifier: {identifier}")
    return {"explanation": build_trace_explanation(record)}
