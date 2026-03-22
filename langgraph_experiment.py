import json
import feedparser
from pathlib import Path
from typing import TypedDict, List, Optional, Literal

from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document


load_dotenv()

SEEN_FILE = Path("seen_headlines.json")

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

def load_seen_headlines() -> set:
    if not SEEN_FILE.exists():
        return set()
    with open(SEEN_FILE, "r") as f:
        return set(json.load(f))


def save_seen_headlines(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f, indent=2)

def normalize_headline(h: str) -> str:
    return h.rsplit(" - ", 1)[0].strip()

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

GRAPH_FILE = Path("supplier_graph.json")
PROFILES_FILE = Path("supplier_profiles.json")


def load_graph_edges(path: Path = GRAPH_FILE):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


graph_edges = load_graph_edges()

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

# ---- Vectorstore ----
vectorstore = load_vectorstore()

# ---- Model ----
model = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)

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
    alert: Optional[str]
    is_valid: Optional[bool]


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
    '''
    suppliers = state["candidate_suppliers"]
    decision = "retrieve" if suppliers else "skip"
    print("DEBUG decide_tool_use ->", decision)
    return {**state, "tool_decision": decision}
    '''
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


# ---- Run ----
if __name__ == "__main__":
    headlines = load_headlines_from_rss()
    seen = load_seen_headlines()

    new_headlines = []
    for h in headlines:
        norm = normalize_headline(h)
        if norm not in seen:
            new_headlines.append(h)

    print(f"Total headlines: {len(headlines)}")
    print(f"New headlines: {len(new_headlines)}")

    for h in new_headlines:
        result = app.invoke({
            "headline": h,
            "candidate_suppliers": [],
            "context": "",
            "tool_decision": None,
            "alert": None,
            "is_valid": None,
        })
        alert = result["alert"]
        
        print("\nHeadline:")
        print(h)
        print("\nTool decision:")
        print(result["tool_decision"])
        print("\nStructured Alert:")
        print(f"Status: {alert.get('status')}")
        print(f"Supplier: {alert.get('supplier')}")
        print(f"Risk Level: {alert.get('risk_level')}")
        print(f"Impact: {alert.get('impact')}")
        print(f"Recommended Action: {alert.get('recommended_action')}")
        print(f"Relevant Supplier Context: {alert.get('relevant_supplier_context')}")
        print("-" * 50)

        seen.add(normalize_headline(h))

    # Save after processing
    save_seen_headlines(seen)