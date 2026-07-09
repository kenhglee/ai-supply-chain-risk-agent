"""Interactive planner copilot CLI.

Answers a planner's supplier-risk questions by reusing the existing
supplier graph/profiles, the RSS pipeline's risk state ledger, the FAISS
retriever, and the mock ServiceNow ticket tool. Keeps a small amount of
conversational state per session so follow-up questions like "Why?" can
refer back to the last recommendation.
"""
import csv
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.copilot.presentation import (
    format_approval_confirmation,
    format_monitor_briefing,
    format_why_response,
)
from app.integrations.servicenow_mock import create_servicenow_ticket
from app.storage.risk_state_store import save_risk_decision

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
PROFILES_FILE = STORAGE_DIR / "supplier_profiles.json"
GRAPH_FILE = STORAGE_DIR / "supplier_graph.json"
COPILOT_LOG_FILE = STORAGE_DIR / "copilot_log.jsonl"

RISK_RANK = {"Low": 1, "Medium": 2, "High": 3}
TICKET_RISK_SCORE = {"High": 85, "Medium": 60, "Low": 30}
REVIEW_APPROVED_DECISION = "review_approved"
RECOMMENDED_ACTION = {
    "High": "Escalate for manual review",
    "Medium": "Monitor closely; review if signal count increases",
    "Low": "Continue routine monitoring",
}


# ---- Data loading (reuses the same env vars / files as the RSS pipeline) ----

def load_supplier_risk_rows() -> list[dict]:
    """Read the supplier risk ledger written by the RSS pipeline."""
    backend = os.getenv("RISK_STATE_BACKEND", "csv").lower()

    if backend == "csv":
        path = Path(os.getenv("RISK_STATE_FILE", "risk_state.csv"))
        if not path.exists():
            return []
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    if backend == "dynamodb":
        import boto3
        table = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_DEFAULT_REGION", "us-west-2")
        ).Table(os.getenv("RISK_STATE_TABLE", "supplier_risk_state"))
        items: list[dict] = []
        kwargs: dict = {}
        while True:
            response = table.scan(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return items

    raise ValueError(f"Unsupported RISK_STATE_BACKEND: '{backend}'")


def load_supplier_profiles(path: Path = PROFILES_FILE) -> dict[str, str]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {item["supplier"]: item["profile"] for item in data}


def load_supplier_graph(path: Path = GRAPH_FILE) -> list[list[str]]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def graph_exposures(graph_edges: list[list[str]], supplier: str) -> list[str]:
    """Risk terms a supplier is structurally exposed to via operates_in/depends_on -> has_risk."""
    linked = {
        target for source, rel, target in graph_edges
        if source == supplier and rel in {"operates_in", "depends_on"}
    }
    return sorted(
        target for source, rel, target in graph_edges
        if rel == "has_risk" and source in linked
    )


def _confidence(risk_signal_count: int) -> str:
    """Deterministic evidence-volume bucket, not a model-derived score."""
    if risk_signal_count >= 3:
        return "High"
    if risk_signal_count == 2:
        return "Medium"
    return "Low"


def _business_impact(supplier: str, risk_type: str | None, exposures: list[str]) -> str:
    exposure_text = ", ".join(exposures) if exposures else "no structural exposure on record"
    risk_label = risk_type or "unspecified"
    return f"{risk_label} risk could disrupt {supplier}'s ability to deliver, given exposure to {exposure_text}."


def _match_supplier(text: str, suppliers: list[dict]) -> dict | None:
    """Case-insensitive substring match of a supplier name within free text."""
    t = text.lower()
    for s in suppliers:
        if s["supplier"].lower() in t:
            return s
    return None


def _retriever_context(query: str, suppliers: list[str]) -> str | None:
    """Best-effort FAISS retrieval for extra narrative context. Degrades to None
    if the retriever can't be built (e.g. no OPENAI_API_KEY configured)."""
    try:
        from app.retrieval.retriever import get_retriever
        retriever = get_retriever(PROFILES_FILE)
        return retriever.retrieve(query, suppliers).context
    except Exception:
        return None


# ---- Ranking ----

def _rank_key(row: dict) -> tuple:
    level = (row.get("current_risk_level") or "").strip()
    return (RISK_RANK.get(level, 0), row.get("last_seen_at") or "")


def top_suppliers(rows: list[dict], limit: int = 3) -> list[dict]:
    """Group risk rows by supplier and return the highest-risk, most-recent
    signal per supplier, ranked descending."""
    by_supplier: dict[str, list[dict]] = {}
    for row in rows:
        supplier = (row.get("supplier") or "").strip()
        if not supplier:
            continue
        by_supplier.setdefault(supplier, []).append(row)

    summaries = []
    for supplier, supplier_rows in by_supplier.items():
        best = max(supplier_rows, key=_rank_key)
        summaries.append({
            "supplier": supplier,
            "risk_level": (best.get("current_risk_level") or "").strip(),
            "risk_type": best.get("risk_type"),
            "last_headline": best.get("last_headline"),
            "last_seen_at": best.get("last_seen_at"),
            "risk_signal_count": len(supplier_rows),
        })

    summaries.sort(key=lambda s: (RISK_RANK.get(s["risk_level"], 0), s["last_seen_at"] or ""), reverse=True)
    return summaries[:limit]


# ---- Intent classification (simple rule-based, no LLM required) ----

def classify_intent(text: str) -> str:
    t = text.strip().lower()
    if not t:
        return "unknown"
    if "approve" in t and "review" in t:
        return "approve_review"
    if "ticket" in t and any(w in t for w in ("create", "open", "file")):
        return "create_ticket"
    if t.startswith("why"):
        return "why"
    if "monitor" in t or "which supplier" in t or "top supplier" in t:
        return "monitor"
    return "unknown"


# ---- Session state ----

@dataclass
class Recommendation:
    trace_id: str
    query: str
    suppliers: list[dict]
    context: str | None
    created_at: str


@dataclass
class CopilotLogStore:
    path: Path = COPILOT_LOG_FILE

    def append(self, record: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


class PlannerCopilot:
    """Holds conversational state for a single planner session."""

    def __init__(
        self,
        risk_rows_loader=load_supplier_risk_rows,
        profiles_loader=load_supplier_profiles,
        graph_loader=load_supplier_graph,
        context_retriever=_retriever_context,
        ticket_creator=create_servicenow_ticket,
        decision_saver=save_risk_decision,
        log_store: CopilotLogStore | None = None,
        top_n: int = 3,
    ) -> None:
        self._risk_rows_loader = risk_rows_loader
        self._profiles_loader = profiles_loader
        self._graph_loader = graph_loader
        self._context_retriever = context_retriever
        self._ticket_creator = ticket_creator
        self._decision_saver = decision_saver
        self._log_store = log_store or CopilotLogStore()
        self._top_n = top_n

        self.session_id = uuid.uuid4().hex
        self.last_recommendation: Recommendation | None = None

    def handle(self, text: str) -> str:
        intent = classify_intent(text)
        if intent == "monitor":
            return self._handle_monitor(text)
        if intent == "why":
            return self._handle_why(text)
        if intent == "create_ticket":
            return self._handle_create_ticket(text)
        if intent == "approve_review":
            return self._handle_approve_review(text)
        return self._handle_unknown(text)

    def _handle_monitor(self, text: str) -> str:
        rows = self._risk_rows_loader()
        ranked = top_suppliers(rows, limit=self._top_n)

        profiles = self._profiles_loader()
        graph_edges = self._graph_loader()
        for s in ranked:
            s["profile"] = profiles.get(s["supplier"], "")
            s["exposures"] = graph_exposures(graph_edges, s["supplier"])
            s["business_impact"] = _business_impact(s["supplier"], s["risk_type"], s["exposures"])
            s["confidence"] = _confidence(s["risk_signal_count"])
            s["recommended_action"] = RECOMMENDED_ACTION.get(s["risk_level"], "Continue routine monitoring")

        context = self._context_retriever(text, [s["supplier"] for s in ranked]) if ranked else None

        trace_id = uuid.uuid4().hex
        self.last_recommendation = Recommendation(
            trace_id=trace_id,
            query=text,
            suppliers=ranked,
            context=context,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        answer = format_monitor_briefing(ranked)

        self._log(
            trace_id=trace_id,
            question=text,
            intent="monitor",
            evidence=ranked,
            recommendation=[s["supplier"] for s in ranked],
            action=None,
        )
        return answer

    def _handle_why(self, text: str) -> str:
        rec = self.last_recommendation
        if rec is None:
            answer = "I don't have a prior recommendation yet. Ask \"Which suppliers should I monitor this week?\" first."
            self._log(trace_id=uuid.uuid4().hex, question=text, intent="why", evidence=None, recommendation=None, action=None)
            return answer

        matched = _match_supplier(text, rec.suppliers)
        targets = [matched] if matched else rec.suppliers
        others = [s for s in rec.suppliers if s is not matched] if matched else []

        answer = format_why_response(matched, targets, others, rec.created_at, rec.context)

        self._log(
            trace_id=uuid.uuid4().hex,
            question=text,
            intent="why",
            evidence=targets,
            recommendation=[s["supplier"] for s in targets],
            action=None,
            parent_trace_id=rec.trace_id,
        )
        return answer

    def _handle_create_ticket(self, text: str) -> str:
        rec = self.last_recommendation
        if rec is None or not rec.suppliers:
            answer = "No recommendation to act on yet. Ask \"Which suppliers should I monitor this week?\" first."
            self._log(trace_id=uuid.uuid4().hex, question=text, intent="create_ticket", evidence=None, recommendation=None, action=None)
            return answer

        top = rec.suppliers[0]
        risk_level = top["risk_level"]
        risk_score = TICKET_RISK_SCORE.get(risk_level, 50)
        decision_type = "manual_review_required" if risk_level == "High" else "review_recommended"
        reason = (
            f"{top['supplier']} flagged for {decision_type.replace('_', ' ')}: "
            f"{top['risk_type']} risk, latest signal: \"{top['last_headline']}\""
        )

        normalized_event = {
            "event_type": "supplier_risk_review",
            "repository": None,
            "supplier": top["supplier"],
        }
        decision = {"decision": decision_type, "risk_score": risk_score, "reason": reason}

        ticket = self._ticket_creator(normalized_event, decision)
        record_id = self._decision_saver(normalized_event, decision, ticket)

        answer = (
            f"Created ticket {ticket['ticket_id']} for {top['supplier']} "
            f"({decision_type}, risk score {risk_score}). Logged as decision {record_id}."
        )

        trace_id = uuid.uuid4().hex
        self._log(
            trace_id=trace_id,
            question=text,
            intent="create_ticket",
            evidence=top,
            recommendation=[top["supplier"]],
            action={"ticket": ticket, "decision_record_id": record_id},
            parent_trace_id=rec.trace_id,
        )
        return answer

    def _handle_approve_review(self, text: str) -> str:
        rec = self.last_recommendation
        if rec is None or not rec.suppliers:
            answer = "No recommendation to act on yet. Ask \"Which suppliers should I monitor this week?\" first."
            self._log(trace_id=uuid.uuid4().hex, question=text, intent="approve_review", evidence=None, recommendation=None, action=None)
            return answer

        top = rec.suppliers[0]
        risk_level = top["risk_level"]
        risk_score = TICKET_RISK_SCORE.get(risk_level, 50)
        reason = (
            f"Review approved for {top['supplier']}: {top['risk_type']} risk, "
            f"latest signal: \"{top['last_headline']}\""
        )

        normalized_event = {
            "event_type": "supplier_risk_review",
            "repository": None,
            "supplier": top["supplier"],
        }
        decision = {"decision": REVIEW_APPROVED_DECISION, "risk_score": risk_score, "reason": reason}

        record_id = self._decision_saver(normalized_event, decision, ticket=None)

        answer = format_approval_confirmation(top, record_id)

        trace_id = uuid.uuid4().hex
        self._log(
            trace_id=trace_id,
            question=text,
            intent="approve_review",
            evidence=top,
            recommendation=[top["supplier"]],
            action={"decision_record_id": record_id},
            parent_trace_id=rec.trace_id,
        )
        return answer

    def _handle_unknown(self, text: str) -> str:
        answer = (
            "I can help with:\n"
            "  - \"Which suppliers require attention today?\"\n"
            "  - \"Why?\" or \"Why <Supplier>?\"\n"
            "  - \"Create a review ticket for the top supplier.\"\n"
            "  - \"Approve review\""
        )
        self._log(trace_id=uuid.uuid4().hex, question=text, intent="unknown", evidence=None, recommendation=None, action=None)
        return answer

    def _log(
        self,
        *,
        trace_id: str,
        question: str,
        intent: str,
        evidence,
        recommendation,
        action,
        parent_trace_id: str | None = None,
    ) -> None:
        self._log_store.append({
            "trace_id": trace_id,
            "parent_trace_id": parent_trace_id,
            "session_id": self.session_id,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "question": question,
            "intent": intent,
            "evidence": evidence,
            "recommendation": recommendation,
            "action": action,
        })


# ---- CLI ----

def run_repl() -> None:
    copilot = PlannerCopilot()
    print("Planner Copilot — ask about supplier risk. Type 'exit' to quit.")
    while True:
        try:
            text = input("planner> ").strip()
        except EOFError:
            break
        if text.lower() in {"exit", "quit"}:
            break
        if not text:
            continue
        print(copilot.handle(text))


if __name__ == "__main__":
    run_repl()
