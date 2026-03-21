from typing import TypedDict, List, Optional, Literal
from langgraph.graph import StateGraph, END
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings

import json

load_dotenv()

def load_vectorstore():
    with open("supplier_profiles.json") as f:
        data = json.load(f)

    texts = [item["profile"] for item in data]

    embeddings = OpenAIEmbeddings()
    vectorstore = FAISS.from_texts(texts, embeddings)

    return vectorstore

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
    headline = state["headline"].lower()
    suppliers = []

    if "japan" in headline:
        suppliers.append("Murata")
    if "taiwan" in headline:
        suppliers.append("TSMC")

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
    headline = state["headline"]
    suppliers = state["candidate_suppliers"]
    
    # Build query (simple but effective)
    query = headline
    if suppliers:
        query += " " + " ".join(suppliers)

    docs = vectorstore.similarity_search(query, k=4)

    filtered = [
        d for d in docs
        if any(s.lower() in d.page_content.lower() for s in suppliers)
    ]

    docs = filtered[:2] if filtered else docs[:2]

    context = "\n\n".join([doc.page_content for doc in docs]) if docs else "No context found"

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
        "recommended_action": string
    }}

    Rules:
    - If at least one candidate supplier is provided and the retrieved context is relevant, do not return "inconclusive".
    - Use the single most relevant supplier from the candidate suppliers.
    - Only return "inconclusive" if no candidate supplier is identified or the signal cannot reasonably be connected to any supplier.
    - Do not include any text outside the JSON.
    """

    response = model.invoke(prompt)
    content = response.content.strip()

    #print("\nDEBUG type(content):", type(content))
    #print("DEBUG raw content:", repr(content))
    print("-" * 50)

    try:
        alert = json.loads(content)
    except Exception as e:
        print("DEBUG parse error:", e)
        alert = {
            "status": "inconclusive",
            "supplier": None,
            "risk_level": None,
            "impact": "Model output could not be parsed reliably.",
            "recommended_action": "Review the headline and prompt logic."
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
            required = ["supplier", "risk_level", "impact", "recommended_action"]
            is_valid = all(alert.get(k) for k in required)

    return {**state, "is_valid": is_valid}


# ---- Node 6: Fallback ----
def fallback_alert(state: RiskState) -> RiskState:
    fallback = {
        "status": "inconclusive",
        "supplier": None,
        "risk_level": None,
        "impact": "Alert could not be validated reliably.",
        "recommended_action": "Review the model output and upstream prompt or retrieval logic."
    }
    return {**state, "alert": fallback}


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


# ---- Run ----
if __name__ == "__main__":
    test_headlines = [
        "Murata faces disruption due to earthquake in Japan",
        "Flooding affects factories in Europe",
    ]

    for h in test_headlines:
        result = app.invoke({
            "headline": h,
            "candidate_suppliers": [],
            "context": "",
            "tool_decision": None,
            "alert": None,
        })

        print("\nHeadline:")
        print(h)
        print("\nTool decision:")
        print(result["tool_decision"])
        print("\nStructured Alert:")
        alert = result["alert"]

        print(f"Status: {alert.get('status')}")
        print(f"Supplier: {alert.get('supplier')}")
        print(f"Risk Level: {alert.get('risk_level')}")
        print(f"Impact: {alert.get('impact')}")
        print(f"Recommended Action: {alert.get('recommended_action')}")
        print("-" * 50)
