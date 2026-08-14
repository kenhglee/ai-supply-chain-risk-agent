import csv
import json
import feedparser
import re
import os
import uuid
import boto3
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, List, Optional, Literal
from dotenv import load_dotenv, find_dotenv
from langchain_aws import ChatBedrockConverse
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from app.ingestion.rss_ingestion import load_headlines_from_rss
from app.retrieval.retriever import get_retriever
from app.storage.risk_trace_store import append_risk_trace
from app.prompt_registry import get_prompt
from app.model_registry import get_model, ModelRecord, ModelRuntime, resolve_model_runtime

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

BASE_DIR = Path(__file__).resolve().parent

SEEN_FILE = BASE_DIR / "seen_headlines.json"
ALERT_FILE = BASE_DIR / "alerts.csv"
ENRICHED_ALERT_FILE = BASE_DIR / "enriched_alerts.csv"
GRAPH_FILE = BASE_DIR / "../storage/supplier_graph.json"
PROFILES_FILE = BASE_DIR / "../storage/supplier_profiles.json"

RISK_RANK = {
    "Low": 1,
    "Medium": 2,
    "High": 3,
}

RISK_TERMS = [
    "earthquake",
    "flooding",
    "flood",
    "typhoon",
    "congestion",
    "drought",
    "outage",
    "strike",
    "sanctions",
    "export controls",
]

SUPPLIER_ALIASES = {
    "TSMC": ["tsmc", "taiwan semiconductor", "taiwan semiconductor manufacturing"],
    "Murata": ["murata"],
    "Foxconn": ["foxconn", "hon hai"],
    "Samsung Electronics": ["samsung electronics", "samsung"],
}


# ---- Risk Store ----
class RiskStateStore:
    def get(self, supplier: str, risk_type: str) -> dict | None:
        raise NotImplementedError

    def put(self, supplier: str, risk_type: str, row: dict) -> None:
        raise NotImplementedError


class CsvRiskStateStore:
    def __init__(self, path: Path):
        self.path = path
        self.state = self._load()

    def _load(self) -> dict[tuple[str, str], dict]:
        if not self.path.exists():
            return {}

        state = {}
        with open(self.path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                supplier = (row.get("supplier") or "").strip()
                risk_type = (row.get("risk_type") or "").strip()
                if supplier and risk_type:
                    state[(supplier, risk_type)] = row
        return state

    def get(self, supplier: str, risk_type: str) -> dict | None:
        return self.state.get((supplier, risk_type))

    def put(self, supplier: str, risk_type: str, row: dict) -> None:
        self.state[(supplier, risk_type)] = row

    def flush(self) -> None:
        fieldnames = [
            "supplier",
            "risk_type",
            "current_risk_level",
            "last_headline",
            "last_seen_at",
        ]
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in self.state.values():
                writer.writerow(row)


class DynamoRiskStateStore:
    def __init__(self, table_name: str, region_name: str | None = None):
        self.dynamodb = boto3.resource(
            "dynamodb",
            region_name=region_name or os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
        )
        self.table = self.dynamodb.Table(table_name)
        

    @staticmethod
    def _pk(supplier: str, risk_type: str) -> str:
        return f"{supplier}#{risk_type}"

    def get(self, supplier: str, risk_type: str) -> dict | None:
        response = self.table.get_item(
            Key={"pk": self._pk(supplier, risk_type)}
        )
        return response.get("Item")

    def put(self, supplier: str, risk_type: str, row: dict) -> None:
        item = dict(row)
        item["pk"] = self._pk(supplier, risk_type)
        item["supplier"] = supplier
        item["risk_type"] = risk_type


        response = self.table.put_item(Item=item)

        verify = self.table.get_item(Key={"pk": item["pk"]})


    def flush(self) -> None:
        pass


# ---- State ----
class RiskState(TypedDict):
    alert_id: str
    trace_id: str
    headline: str
    candidate_suppliers: List[str]
    context: str
    tool_decision: Optional[Literal["retrieve", "skip"]]
    alert: Optional[dict]
    is_valid: Optional[bool]
    trace_steps: List[dict]


def get_risk_store():
    backend = os.getenv("RISK_STATE_BACKEND", "csv").lower()

    if backend == "csv":
        path = Path(os.getenv("RISK_STATE_FILE", "risk_state.csv"))
        return CsvRiskStateStore(path)

    if backend == "dynamodb":
        return DynamoRiskStateStore(
            table_name=os.getenv("RISK_STATE_TABLE", "supplier_risk_state"),
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
        )

    raise ValueError(f"Unsupported RISK_STATE_BACKEND: {backend}")


def save_risk_state(risk_store) -> None:
    risk_store.flush()


def normalize_risk_type(risk_type: str | None) -> str | None:
    if not risk_type:
        return None

    value = risk_type.strip().lower().replace(" ", "_")

    aliases = {
        "earthquake": "earthquake",
        "flood": "flood",
        "flooding": "flood",
        "strike": "strike",
        "labor_strike": "strike",
        "outage": "outage",
        "power_outage": "outage",
        "sanction": "sanctions",
        "sanctions": "sanctions",
        "export_control": "export_controls",
        "export_controls": "export_controls",
        "typhoon": "typhoon",
        "drought": "drought",
        "congestion": "congestion",
        "geopolitics": "geopolitical_tension",
        "geopolitical_tensions": "geopolitical_tension",
        "geopolitical_risk": "geopolitical_tension",
    }

    return aliases.get(value, value)


def normalize_headline(h: str) -> str:
    return h.rsplit(" - ", 1)[0].strip()


def load_seen_headlines() -> set:
    if not SEEN_FILE.exists():
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_headlines(seen: set):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, indent=2)


def load_graph_edges(path: Path = GRAPH_FILE):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def bootstrap_alerts_csv_from_rss(alerts_file: Path = ALERT_FILE) -> int:
    """
    Fetch RSS alerts and append only unseen headlines to alerts.csv.

    Returns:
        int: number of rows appended
    """
    seen = load_seen_headlines()
    alerts = load_headlines_from_rss()

    file_exists = alerts_file.exists()
    rows_written = 0
    next_id = get_next_alert_number(alerts_file)

    with open(alerts_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["alert_id", "headline", "source", "status"]
        )

        if not file_exists:
            writer.writeheader()

        for alert in alerts:
            headline = (alert.get("headline") or "").strip()
            if not headline:
                continue

            norm = normalize_headline(headline)
            if norm in seen:
                continue

            writer.writerow(
                {
                    "alert_id": f"rss-{next_id}",
                    "headline": headline,
                    "source": alert.get("source", "google_news_rss"),
                    "status": alert.get("status", "new"),
                }
            )

            seen.add(norm)
            rows_written += 1
            next_id += 1

    save_seen_headlines(seen)
    return rows_written


def load_alerts_csv(alerts_file: Path = ALERT_FILE) -> list[dict]:
    if not alerts_file.exists():
        return []

    with open(alerts_file, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)   


def is_actionable_alert(headline: str) -> bool:
    h = headline.lower()

    has_risk_term = any(term in h for term in RISK_TERMS)
    has_supplier_alias = any(
        alias in h
        for aliases in SUPPLIER_ALIASES.values()
        for alias in aliases
    )

    # For now, require at least one concrete signal
    return has_risk_term or has_supplier_alias


def get_next_alert_number(alerts_file: Path = ALERT_FILE) -> int:
    """
    Scan existing alerts.csv and return the next available numeric suffix
    for IDs like rss-1, rss-2, ...
    """
    if not alerts_file.exists():
        return 1

    max_n = 0
    with open(alerts_file, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            alert_id = (row.get("alert_id") or "").strip()
            m = re.fullmatch(r"rss-(\d+)", alert_id)
            if m:
                max_n = max(max_n, int(m.group(1)))

    return max_n + 1


def write_alerts_csv(rows: list[dict], alerts_file: Path = ALERT_FILE) -> None:
    if not rows:
        return

    fieldnames = ["alert_id", "headline", "source", "status"]

    with open(alerts_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_enriched_alerts(rows: list[dict], path: Path = ENRICHED_ALERT_FILE) -> None:
    if not rows:
        return

    fieldnames = [
        "processed_at",
        "alert_id",
        "trace_id",
        "trace_steps_count",
        "headline",
        "source",
        "status",
        "tool_decision",
        "candidate_suppliers",
        "final_status",
        "supplier",
        "risk_type",
        "risk_level",
        "change_type",
        "change_message",
        "impact",
        "recommended_action",
        "relevant_supplier_context",
    ]

    file_exists = path.exists()

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        for row in rows:
            row = dict(row)
            row["processed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            writer.writerow(row)


def detect_risk_terms(headline: str) -> List[str]:
    h = headline.lower()
    return [term for term in RISK_TERMS if term in h]


def headline_mentions(text: str, term: str) -> bool:
    return term.lower() in text.lower()


def compare_and_update_risk_state(alert: dict, headline: str, state) -> tuple[str, str]:
    """
    Returns:
        change_type: new_alert | suppressed | escalated | downgraded
        change_message: human-friendly explanation
    """

    supplier = (alert.get("supplier") or "").strip()
    risk_type = normalize_risk_type(alert.get("risk_type"))
    risk_level = (alert.get("risk_level") or "").strip()
    status = (alert.get("status") or "").strip()

    if alert.get("status") != "ok":
        return "inconclusive", "No supplier-specific alert state change could be determined."

    if not supplier or not risk_type or not risk_level:
        return "inconclusive", "Missing supplier, risk type, or risk level for state comparison."

    prior = state.get(supplier, risk_type)
    now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if prior is None:
        row = {
            "supplier": supplier,
            "risk_type": risk_type,
            "current_risk_level": risk_level,
            "last_headline": headline,
            "last_seen_at": now_ts,
        }
        state.put(supplier, risk_type, row)
        return "new_alert", f"New disruption detected for {supplier} ({risk_type}) at {risk_level} risk."

    old_level = prior.get("current_risk_level")
    old_rank = RISK_RANK.get(old_level, 0)
    new_rank = RISK_RANK.get(risk_level, 0)

    updated_row = {
        **prior,
        "supplier": supplier,
        "risk_type": risk_type,
        "current_risk_level": risk_level,
        "last_headline": headline,
        "last_seen_at": now_ts,
    }
    
    if new_rank > old_rank:
        state.put(supplier, risk_type, updated_row)
        return "escalated", f"Risk escalation detected for {supplier}: {old_level} → {risk_level} ({risk_type})."

    if new_rank == old_rank:
        state.put(supplier, risk_type, updated_row)
        return "suppressed", f"Same supplier and risk level as prior alert for {supplier} ({risk_type}); suppressing duplicate."

    state.put(supplier, risk_type, updated_row)
    return "downgraded", f"Risk level decreased for {supplier}: {old_level} → {risk_level} ({risk_type})."


def find_suppliers_exposed_to_risk(graph_edges, risk_term: str, headline: str) -> List[str]:
    exposed_nodes = {
        source for source, rel, target in graph_edges
        if rel == "has_risk" and target.lower() == risk_term.lower()
    }

    suppliers = set()

    for source, rel, target in graph_edges:
        if rel in {"operates_in", "depends_on"} and target in exposed_nodes:
            # Only keep if the headline mentions the exposed node or supplier
            if headline_mentions(headline, target) or headline_mentions(headline, source):
                suppliers.add(source)

    return list(suppliers)


def infer_candidate_suppliers_from_graph(headline: str, graph_edges, supplier_aliases: dict[str, list[str]]) -> list[str]:
    candidate_suppliers = set()

    # 1. Graph-based inference from risk terms
    for term in detect_risk_terms(headline):
        candidate_suppliers.update(
            find_suppliers_exposed_to_risk(graph_edges, term, headline)
            )

    # 2. Direct supplier mention fallback
    h = headline.lower()
    for supplier, aliases in supplier_aliases.items():
        if any(alias in h for alias in aliases):
            candidate_suppliers.add(supplier)

    return list(candidate_suppliers)


def get_llm(model_record: ModelRecord):
    """Build a LangChain LLM from a ModelRecord.

    Delegates to resolve_model_runtime() so LLM construction and trace metadata
    are guaranteed to use identical provider/model values.
    """
    rt = resolve_model_runtime(model_record)

    if rt.runtime_provider == "openai":
        return ChatOpenAI(model=rt.runtime_model_name, temperature=0.2)

    if rt.runtime_provider == "bedrock":
        return ChatBedrockConverse(
            model=rt.runtime_model_name,
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
            temperature=0.2,
        )

    if rt.runtime_provider == "bifrost":
        return ChatOpenAI(
            model=rt.runtime_model_name,
            temperature=0.2,
            base_url=os.getenv("BIFROST_BASE_URL", "http://localhost:8080/langchain"),
            api_key=os.getenv("BIFROST_API_KEY", "dummy-key"),
        )

    raise ValueError(f"Unsupported LLM_PROVIDER: '{rt.runtime_provider}'")


def make_pk(supplier: str, risk_type: str) -> str:
    return f"{supplier}#{risk_type}"


def get_risk_state_item(supplier: str, risk_type: str) -> dict | None:
    response = risk_table.get_item(
        Key={"pk": make_pk(supplier, risk_type)}
    )
    return response.get("Item")


def put_risk_state_item(
    supplier: str,
    risk_type: str,
    current_risk_level: str,
    last_headline: str,
    last_seen_at: str | None = None,
) -> None:
    if last_seen_at is None:
        last_seen_at = datetime.now(timezone.utc).isoformat()

    risk_table.put_item(
        Item={
            "pk": make_pk(supplier, risk_type),
            "supplier": supplier,
            "risk_type": risk_type,
            "current_risk_level": current_risk_level,
            "last_headline": last_headline,
            "last_seen_at": last_seen_at,
        }
    )


def model_text(response) -> str:
    content = getattr(response, "content", response)

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            else:
                parts.append(str(item))
        return "\n".join(parts)

    return str(content)


def parse_alert_response(raw_text: str) -> dict:
    text = raw_text.strip()

    # Remove ```json ... ``` or ``` ... ``` wrappers
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except Exception as e:
        return {
            "status": "inconclusive",
            "supplier": None,
            "risk_type": None,
            "risk_level": None,
            "impact": "Model output could not be parsed reliably.",
            "recommended_action": "Review the headline and prompt logic.",
            "relevant_supplier_context": "",
        }


# ---- DynamoDB ----
dynamodb = boto3.resource(
    "dynamodb",
    region_name=os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
)

risk_table = dynamodb.Table("supplier_risk_state")

# ---- Graph ----
graph_edges = load_graph_edges()
# ---- Retriever ----
_retriever = None


def _get_retriever():
    global _retriever
    if _retriever is None:
        _retriever = get_retriever(PROFILES_FILE)
    return _retriever


# ---- Model Registry ----
_triage_model_record = get_model("triage_primary")
_risk_model_record = get_model("risk_analysis_primary")
# ---- Runtime Resolution (captures env overrides once at startup) ----
_triage_runtime = resolve_model_runtime(_triage_model_record)
_risk_runtime = resolve_model_runtime(_risk_model_record)
# ---- Models ----
_triage_llm = get_llm(_triage_model_record)
_risk_llm = get_llm(_risk_model_record)
# ---- Prompt Registry ----
_triage_prompt = get_prompt("triage_agent")
_risk_prompt = get_prompt("risk_classifier")


# ---- Trace helper ----
def record_trace_step(
    steps: list,
    node_name: str,
    t0: float,
    decision: str | None = None,
    error: str | None = None,
) -> list:
    t1 = time.time()
    step = {
        "node_name": node_name,
        "started_at": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(timespec="milliseconds"),
        "ended_at": datetime.fromtimestamp(t1, tz=timezone.utc).isoformat(timespec="milliseconds"),
        "duration_ms": round((t1 - t0) * 1000, 1),
    }
    if decision is not None:
        step["decision"] = decision
    if error is not None:
        step["error"] = error
    return steps + [step]


# ---- Node 1: infer suppliers (your existing graph/rule logic placeholder) ----
def infer_suppliers(state: RiskState) -> RiskState:
    t0 = time.time()
    headline = state["headline"]
    suppliers = infer_candidate_suppliers_from_graph(headline, graph_edges, SUPPLIER_ALIASES)
    return {
        **state,
        "candidate_suppliers": suppliers,
        "trace_steps": record_trace_step(state["trace_steps"], "infer", t0),
    }


# ---- Node 2: controlled "reason" step ----
def decide_tool_use(state: RiskState) -> RiskState:
    t0 = time.time()
    headline = state["headline"]
    suppliers = state["candidate_suppliers"]

    prompt = _triage_prompt.template.format(
        headline=headline,
        suppliers=", ".join(suppliers) if suppliers else "None",
    )

    response = _triage_llm.invoke(prompt)
    raw_text = model_text(response)
    decision = raw_text.strip().lower()

    if decision not in {"retrieve", "skip"}:
        decision = "skip"

    return {
        **state,
        "tool_decision": decision,
        "trace_steps": record_trace_step(state["trace_steps"], "decide", t0, decision=decision),
    }


# ---- Node 3: retrieval ----
def retrieve_context(state: RiskState) -> RiskState:
    t0 = time.time()
    headline = state["headline"]
    suppliers = state["candidate_suppliers"]

    query_parts = [headline]
    if suppliers:
        query_parts.extend(suppliers)
    query = " ".join(query_parts)

    result = _get_retriever().retrieve(query, suppliers)
    print("-" * 50)

    return {
        **state,
        "context": result.context,
        "trace_steps": record_trace_step(state["trace_steps"], "retrieve", t0),
    }


# ---- Node 4: Analysis ----
def analyze_risk(state: RiskState) -> RiskState:
    t0 = time.time()
    headline = state["headline"]
    candidate_suppliers = state["candidate_suppliers"]
    context = state["context"] or "No additional context retrieved."

    prompt = _risk_prompt.template.format(
        headline=headline,
        candidate_suppliers=", ".join(candidate_suppliers) if candidate_suppliers else "None",
        context=context,
    )

    response = _risk_llm.invoke(prompt)
    raw_text = model_text(response)

    try:
        alert = parse_alert_response(raw_text)
        print("-" * 50)

        # normalize model output before downstream comparison
        alert["risk_type"] = normalize_risk_type(alert.get("risk_type"))

        # normalize risk level capitalization too, for safety
        if alert.get("risk_level"):
            alert["risk_level"] = alert["risk_level"].strip().title()

        # Enforce consistent output for inconclusive alerts in case the model still produces speculative fields.
        if alert.get("status") == "inconclusive":
            alert["supplier"] = None
            alert["risk_type"] = None
            alert["risk_level"] = None
            alert["impact"] = "No clearly relevant supplier could be identified from the current signal."
            alert["recommended_action"] = "Monitor for more supplier-specific information before taking action."
            alert["relevant_supplier_context"] = ""
    except Exception as exc:
        alert = {
            "status": "inconclusive",
            "supplier": None,
            "risk_level": None,
            "impact": "Model output could not be parsed reliably.",
            "recommended_action": "Review the headline and prompt logic.",
            "relevant_supplier_context": ""
        }
        return {
            **state,
            "alert": alert,
            "trace_steps": record_trace_step(state["trace_steps"], "analyze", t0, error=str(exc)),
        }
    return {
        **state,
        "alert": alert,
        "trace_steps": record_trace_step(state["trace_steps"], "analyze", t0),
    }

# ---- Node 5: Validation ----
def validate_alert(state: RiskState) -> RiskState:
    t0 = time.time()
    alert = state.get("alert")
    is_valid = False

    if isinstance(alert, dict):
        status = alert.get("status")
        if status == "inconclusive":
            is_valid = True
        elif status == "ok":
            required = [
                "supplier",
                "risk_type",
                "risk_level",
                "impact",
                "recommended_action",
                "relevant_supplier_context"]
            is_valid = all(alert.get(k) for k in required)

    return {
        **state,
        "is_valid": is_valid,
        "trace_steps": record_trace_step(
            state["trace_steps"], "validate", t0,
            decision="valid" if is_valid else "fallback",
        ),
    }


# ---- Node 6: Fallback ----
def fallback_alert(state: RiskState) -> RiskState:
    t0 = time.time()
    fallback = {
        "status": "inconclusive",
        "supplier": None,
        "risk_level": None,
        "impact": "Alert could not be validated reliably.",
        "recommended_action": "Review the model output and upstream prompt or retrieval logic.",
        "relevant_supplier_context": ""
    }
    return {
        **state,
        "alert": fallback,
        "trace_steps": record_trace_step(state["trace_steps"], "fallback", t0),
    }


# ---- Conditional router for tool decision ----
def route_after_decision(state: RiskState) -> str:
    return "retrieve" if state["tool_decision"] == "retrieve" else "analyze"

# ---- Conditional router for validation ----
def route_after_validation(state: RiskState) -> str:
    return "end" if state.get("is_valid") else "fallback"

# ---- Graph ----
graph = StateGraph(RiskState)

graph.add_node("infer", infer_suppliers)
graph.add_node("decide", decide_tool_use)
graph.add_node("retrieve", retrieve_context)
graph.add_node("analyze", analyze_risk)
graph.add_node("validate", validate_alert)
graph.add_node("fallback", fallback_alert)

graph.set_entry_point("infer")
graph.add_edge("infer", "decide")
graph.add_conditional_edges(
    "decide",
    route_after_decision,
    {
        "retrieve": "retrieve",
        "analyze": "analyze",
    },
)
graph.add_edge("retrieve", "analyze")
graph.add_edge("analyze", "validate")
graph.add_conditional_edges(
    "validate",
    route_after_validation,
    {
        "end": END,
        "fallback": "fallback",
    },
)

graph.add_edge("fallback", END)

app = graph.compile()


def process_alert_row(row: dict, risk_store) -> dict:
    headline = row["headline"]
    alert_id = row.get("alert_id") or uuid.uuid4().hex
    trace_id = uuid.uuid4().hex

    if not is_actionable_alert(headline):
        append_risk_trace({
            "alert_id": alert_id,
            "trace_id": trace_id,
            "headline": headline,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_duration_ms": 0,
            "tool_decision": "skip",
            "final_status": "inconclusive",
            "supplier": None,
            "risk_type": None,
            "risk_level": None,
            "change_type": "ignored",
            "trace_steps": [],
        })
        return {
            "alert_id": alert_id,
            "trace_id": trace_id,
            "trace_steps_count": 0,
            "headline": headline,
            "source": row.get("source", ""),
            "status": row.get("status", ""),
            "tool_decision": "skip",
            "candidate_suppliers": "",
            "final_status": "inconclusive",
            "supplier": "",
            "risk_type": "",
            "risk_level": "",
            "change_type": "ignored",
            "change_message": "Headline appears informational rather than a concrete disruption signal.",
            "impact": "No actionable supply disruption signal detected from the headline.",
            "recommended_action": "Ignore for now or monitor for more specific operational disruption news.",
            "relevant_supplier_context": "",
        }

    t_run_start = time.time()
    result = app.invoke({
        "alert_id": alert_id,
        "trace_id": trace_id,
        "headline": headline,
        "candidate_suppliers": [],
        "context": "",
        "tool_decision": None,
        "alert": None,
        "is_valid": None,
        "trace_steps": [],
    })
    run_duration_ms = round((time.time() - t_run_start) * 1000, 1)

    alert = result["alert"]

    change_type, change_message = compare_and_update_risk_state(
        alert=alert,
        headline=headline,
        state=risk_store,
    )

    _r = _retriever  # None when retrieve node was skipped; instance otherwise
    append_risk_trace({
        "alert_id": alert_id,
        "trace_id": trace_id,
        "headline": headline,
        "created_at": datetime.fromtimestamp(t_run_start, tz=timezone.utc).isoformat(timespec="milliseconds"),
        "run_duration_ms": run_duration_ms,
        "tool_decision": result.get("tool_decision"),
        "final_status": alert.get("status"),
        "supplier": alert.get("supplier"),
        "risk_type": alert.get("risk_type"),
        "risk_level": alert.get("risk_level"),
        "change_type": change_type,
        "trace_steps": result.get("trace_steps", []),
        "retriever_metadata": {
            "retriever_id": _r.retriever_id if _r is not None else None,
            "retriever_version": _r.retriever_version if _r is not None else None,
            "embedding_provider": _r.embedding_provider if _r is not None else None,
            "top_k": _r.top_k if _r is not None else None,
            "latency_ms": next(
                (s["duration_ms"] for s in result.get("trace_steps", []) if s["node_name"] == "retrieve"),
                None,
            ),
        },
        "prompt_metadata": [
            {
                "prompt_id": _triage_prompt.prompt_id,
                "prompt_version": _triage_prompt.version,
                "prompt_status": _triage_prompt.status,
                "prompt_description": _triage_prompt.description,
            },
            {
                "prompt_id": _risk_prompt.prompt_id,
                "prompt_version": _risk_prompt.version,
                "prompt_status": _risk_prompt.status,
                "prompt_description": _risk_prompt.description,
            },
        ],
        "model_metadata": [
            {
                "model_id": _triage_runtime.model_id,
                "model_version": _triage_runtime.model_version,
                "model_status": _triage_runtime.model_status,
                "model_provider": _triage_runtime.model_provider,
                "model_name": _triage_runtime.model_name,
                "model_description": _triage_runtime.model_description,
                "runtime_provider": _triage_runtime.runtime_provider,
                "runtime_model_name": _triage_runtime.runtime_model_name,
                "runtime_overridden": _triage_runtime.runtime_overridden,
            },
            {
                "model_id": _risk_runtime.model_id,
                "model_version": _risk_runtime.model_version,
                "model_status": _risk_runtime.model_status,
                "model_provider": _risk_runtime.model_provider,
                "model_name": _risk_runtime.model_name,
                "model_description": _risk_runtime.model_description,
                "runtime_provider": _risk_runtime.runtime_provider,
                "runtime_model_name": _risk_runtime.runtime_model_name,
                "runtime_overridden": _risk_runtime.runtime_overridden,
            },
        ],
    })

    return {
        "alert_id": alert_id,
        "trace_id": trace_id,
        "trace_steps_count": len(result.get("trace_steps", [])),
        "headline": headline,
        "source": row.get("source", ""),
        "status": row.get("status", ""),
        "tool_decision": result.get("tool_decision"),
        "candidate_suppliers": ", ".join(result.get("candidate_suppliers", [])),
        "final_status": alert.get("status"),
        "supplier": alert.get("supplier") or "",
        "risk_type": alert.get("risk_type") or "",
        "risk_level": alert.get("risk_level") or "",
        "change_type": change_type,
        "change_message": change_message,
        "impact": alert.get("impact"),
        "recommended_action": alert.get("recommended_action", ""),
        "relevant_supplier_context": alert.get("relevant_supplier_context", ""),
    }

def run_pipeline() -> dict:
    OUTPUT_MODE = os.getenv("OUTPUT_MODE", "csv")
    alerts = []

    if OUTPUT_MODE == "csv":
        bootstrap_alerts_csv_from_rss()
        alerts = load_alerts_csv()

    elif OUTPUT_MODE == "lambda":
        alerts = load_headlines_from_rss()
        for idx, alert in enumerate(alerts, start=1):
            if not alert.get("alert_id"):
                alert["alert_id"] = f"runtime-{idx}"

    else:
        raise ValueError(f"Unsupported OUTPUT_MODE: {OUTPUT_MODE}")

    logger.info(json.dumps({
        "stage": "alerts_loaded",
        "output_mode": OUTPUT_MODE,
        "alerts_loaded": len(alerts),
    }))

    risk_store = get_risk_store()
    enriched_rows = []
    alerts_processed = 0

    max_alerts = int(os.getenv("MAX_ALERTS_PER_RUN", "1"))
    new_alerts = [
        row for row in alerts 
        if (row.get("status") or "new").strip().lower() == "new"
    ]
    alerts_to_process = new_alerts[:max_alerts]

    logger.info(json.dumps({
        "stage": "alerts_selected",
        "max_alerts_per_run": max_alerts,
        "new_alerts_found": len(new_alerts),
        "alerts_selected_for_processing": len(alerts_to_process),
    }))

    for idx, row in enumerate(alerts_to_process, start=1):
        alert_id = row.get("alert_id", f"unknown-{idx}")
        supplier = row.get("supplier", "unknown")

        logger.info(json.dumps({
            "stage": "alert_processing_start",
            "sequence": idx,
            "alert_id": alert_id,
            "supplier": supplier,
        }))

        processed = process_alert_row(row, risk_store)
        time.sleep(1.5)

        if processed is not None:
            enriched_rows.append(processed)

        row["status"] = "processed"
        alerts_processed += 1

        logger.info(json.dumps({
            "stage": "alert_processing_complete",
            "sequence": idx,
            "alert_id": alert_id,
            "supplier": supplier,
            "processed_result": processed is not None,
        }))

    logger.info(json.dumps({
        "stage": "alerts_processed",
        "alerts_processed": alerts_processed,
    }))

    save_risk_state(risk_store)

    if OUTPUT_MODE == "csv":
        write_enriched_alerts(enriched_rows)
        write_alerts_csv(alerts)
    elif OUTPUT_MODE == "lambda":
        pass
    else:
        raise ValueError(f"Unsupported OUTPUT_MODE: {OUTPUT_MODE}")

    summary = {
        "alerts_loaded": len(alerts),
        "alerts_processed": alerts_processed,
        "enriched_alerts": len(enriched_rows),
        "llm_provider": os.getenv("LLM_PROVIDER", "openai"),
        "risk_state_backend": os.getenv("RISK_STATE_BACKEND", "csv"),
    }

    logger.info(json.dumps({
        "stage": "pipeline_complete",
        **summary,
    }))

    return summary

def run_supplier_risk_flow(event=None):
    summary = run_pipeline()
    return summary

# ---- Run ----
if __name__ == "__main__":
     result = run_supplier_risk_flow()
     print(json.dumps(result, indent=2))