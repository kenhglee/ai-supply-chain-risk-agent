import csv
import json
import feedparser
import re
from pathlib import Path
from typing import TypedDict, List, Optional, Literal

from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document


load_dotenv()

SEEN_FILE = Path("seen_headlines.json")
ALERT_FILE = Path("alerts.csv")
ENRICHED_ALERT_FILE = Path("enriched_alerts.csv")

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

GRAPH_FILE = Path("supplier_graph.json")
PROFILES_FILE = Path("supplier_profiles.json")


# ---- Alert Dictionary ----
class AlertDict(TypedDict, total=False):
    status: Literal["ok", "inconclusive"]
    supplier: Optional[str]
    risk_level: Optional[str]
    impact: str
    recommended_action: str
    relevant_supplier_context: str


# ---- State ----
class RiskState(TypedDict):
    headline: str
    candidate_suppliers: List[str]
    context: str
    tool_decision: Optional[Literal["retrieve", "skip"]]
    alert: Optional[dict]
    is_valid: Optional[bool]


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


def load_vectorstore(path: Path = PROFILES_FILE):
    with open(path, "r", encoding="utf-8") as f:
        profiles = json.load(f)

    docs = [
        Document(
            page_content=item["profile"],
            metadata={"supplier": item["supplier"]}
        )
        for item in profiles
    ]

    embeddings = OpenAIEmbeddings()
    return FAISS.from_documents(docs, embeddings)


def load_headlines_from_rss() -> list[str]:
    rss_url = "https://news.google.com/rss/search?q=TSMC+OR+Foxconn+OR+Murata&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(rss_url)

    headlines = [entry.title for entry in feed.entries[:10]]

    if not headlines:
        headlines = [
            "Murata faces disruption due to earthquake in Japan",
            "Taiwan earthquake disrupts semiconductor operations",
            "Flooding affects factories in Europe",
        ]
        print("(Using sample headlines - RSS feed returned empty)\n")

    return headlines


def bootstrap_alerts_csv_from_rss(alerts_file: Path = ALERT_FILE) -> int:
    """
    Writes new, unseen RSS headlines into alerts.csv as raw alerts.
    Returns the number of rows appended.
    """
    seen = load_seen_headlines()
    headlines = load_headlines_from_rss()

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

        for headline in headlines:
            norm = normalize_headline(headline)
            if norm in seen:
                continue

            writer.writerow({
                "alert_id": f"rss-{next_id}",
                "headline": headline,
                "source": "google_news_rss",
                "status": "new",
            })
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


def write_enriched_alerts(rows: list[dict], path: Path = ENRICHED_ALERT_FILE) -> None:
    if not rows:
        return

    fieldnames = [
        "alert_id",
        "headline",
        "source",
        "status",
        "tool_decision",
        "candidate_suppliers",
        "final_status",
        "supplier",
        "risk_level",
        "impact",
        "recommended_action",
        "relevant_supplier_context",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def detect_risk_terms(headline: str) -> List[str]:
    h = headline.lower()
    return [term for term in RISK_TERMS if term in h]


def headline_mentions(text: str, term: str) -> bool:
    return term.lower() in text.lower()


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


# ---- Graph ----
graph_edges = load_graph_edges()
# ---- Vectorstore ----
vectorstore = load_vectorstore()
# ---- Model ----
model = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)


# ---- Node 1: infer suppliers (your existing graph/rule logic placeholder) ----
def infer_suppliers(state: RiskState) -> RiskState:
    headline = state["headline"]
    suppliers = infer_candidate_suppliers_from_graph(headline, graph_edges, SUPPLIER_ALIASES)

    #print("\nDEBUG infer_suppliers")
    #print("headline:", headline)
    #print("suppliers:", suppliers)

    return {**state, "candidate_suppliers": suppliers}


# ---- Node 2: controlled "reason" step ----
def decide_tool_use(state: RiskState) -> RiskState:
    headline = state["headline"]
    suppliers = state["candidate_suppliers"]

    prompt = f"""
            You are a supply chain risk analyst.

            Headline:
            {headline}

            Candidate suppliers:
            {", ".join(suppliers) if suppliers else "None"}

            Decide whether supplier-context retrieval is needed before risk analysis.

            Return exactly one word:
            - retrieve
            - skip
            """

    response = model.invoke(prompt)
    decision = response.content.strip().lower()

    if decision not in {"retrieve", "skip"}:
        decision = "skip"

    return {**state, "tool_decision": decision}


# ---- Node 3: retrieval tool step (mock for now) ----
def retrieve_context(state: RiskState) -> RiskState:
    #print("\nDEBUG retrieve_context reached")
    #print("candidate_suppliers:", state["candidate_suppliers"])
    headline = state["headline"]
    suppliers = state["candidate_suppliers"]
    
    query_parts = [headline]
    if suppliers:
        query_parts.extend(suppliers)

    query = " ".join(query_parts)
    docs = vectorstore.similarity_search(query, k=4)

    # Prefer docs whose metadata supplier matches inferred suppliers
    if suppliers:
        filtered = [
            d for d in docs
            if d.metadata.get("supplier") in suppliers
        ]
        docs = filtered[:2] if filtered else docs[:2]
    else:
        docs = docs[:2]

    context = "\n\n".join(doc.page_content for doc in docs) if docs else "No context found"
    
    #print("\nDEBUG retrieved context:")
    #print(context)
    print("-" * 50)

    return {**state, "context": context}


# ---- Node 4: Analysis ----
def analyze_risk(state: RiskState) -> RiskState:
    headline = state["headline"]
    candidate_suppliers = state["candidate_suppliers"]
    context = state["context"] or "No additional context retrieved."

    prompt = f"""
    You are a supply chain risk analyst.

    Headline:
    {headline}

    Candidate suppliers:
    {", ".join(candidate_suppliers) if candidate_suppliers else "None"}

    Relevant supplier context:
    {context}

    Return ONLY valid JSON with this exact schema:

    {{
        "status": "ok" or "inconclusive",
        "supplier": string or null,
        "risk_level": "High" or "Medium" or "Low" or null,
        "impact": string,
        "recommended_action": string,
        "relevant_supplier_context": string
    }}

    Rules:
    - If at least one candidate supplier is provided and the retrieved context is relevant, use status = "ok".
    - If no supplier is clearly relevant, use status = "inconclusive".
    - If status = "inconclusive":
        - supplier must be null
        - risk_level must be null
        - impact must explain that no clearly relevant supplier could be identified
        - recommended_action must focus on monitoring or gathering more supplier-specific information
        - do not speculate about concrete supplier impact
    - Do not include any text outside the JSON.
    """

    response = model.invoke(prompt)
    content = response.content.strip()

    #print("\nDEBUG type(content):", type(content))
    #print("DEBUG raw content:", repr(content))
    print("-" * 50)

    try:
        alert = json.loads(content)

        # Enforce consistent output for inconclusive alerts in case the model still produces speculative fields.
        if alert.get("status") == "inconclusive":
            alert["supplier"] = None
            alert["risk_level"] = None
            alert["impact"] = "No clearly relevant supplier could be identified from the current signal."
            alert["recommended_action"] = "Monitor for more supplier-specific information before taking action."
            alert["relevant_supplier_context"] = ""
    except Exception as e:
        print("DEBUG parse error:", e)
        alert = {
            "status": "inconclusive",
            "supplier": None,
            "risk_level": None,
            "impact": "Model output could not be parsed reliably.",
            "recommended_action": "Review the headline and prompt logic.",
            "relevant_supplier_context": ""
        }
    return {**state, "alert": alert}

# ---- Node 5: Validation ----
def validate_alert(state: RiskState) -> RiskState:
    alert = state.get("alert")
    is_valid = False

    if isinstance(alert, dict):
        status = alert.get("status")
        if status == "inconclusive":
            is_valid = True
        elif status == "ok":
            required = ["supplier", "risk_level", "impact", "recommended_action", "relevant_supplier_context"]
            is_valid = all(alert.get(k) for k in required)

    return {**state, "is_valid": is_valid}


# ---- Node 6: Fallback ----
def fallback_alert(state: RiskState) -> RiskState:
    fallback = {
        "status": "inconclusive",
        "supplier": None,
        "risk_level": None,
        "impact": "Alert could not be validated reliably.",
        "recommended_action": "Review the model output and upstream prompt or retrieval logic.",
        "relevant_supplier_context": ""
    }
    return {**state, "alert": fallback}


# ---- Conditional router for tool decision ----
def route_after_decision(state: RiskState) -> str:
    #print("\nDEBUG route_after_decision")
    #print("tool_decision:", state["tool_decision"])
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


def process_alert_row(row: dict) -> dict:
    headline = row["headline"]

    result = app.invoke({
        "headline": headline,
        "candidate_suppliers": [],
        "context": "",
        "tool_decision": None,
        "alert": None,
        "is_valid": None,
    })

    alert = result["alert"]

    if not is_actionable_alert(headline):
        return {
            "alert_id": row.get("alert_id", ""),
            "headline": headline,
            "source": row.get("source", ""),
            "status": row.get("status", ""),
            "tool_decision": result.get("tool_decision"),
            "candidate_suppliers": ", ".join(result.get("candidate_suppliers", [])),
            "final_status": alert.get("status"),
            "supplier": alert.get("supplier"),
            "risk_level": alert.get("risk_level"),
            "impact": alert.get("impact"),
            "recommended_action": alert.get("recommended_action"),
            "relevant_supplier_context": alert.get("relevant_supplier_context"),
        }


# ---- Run ----
if __name__ == "__main__":
     # optional bootstrap step
    bootstrap_alerts_csv_from_rss()

    alerts = load_alerts_csv()
    new_alerts = [row for row in alerts if row.get("status") == "new"]

    print(f"Alerts loaded: {len(alerts)}")
    print(f"Alerts to process: {len(new_alerts)}")

    enriched_rows = [process_alert_row(row) for row in new_alerts]
    write_enriched_alerts(enriched_rows)

    print(f"Enriched alerts written: {len(enriched_rows)}")