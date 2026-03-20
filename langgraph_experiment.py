from typing import TypedDict, List, Optional, Literal
from langgraph.graph import StateGraph, END
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

# ---- Model ----
model = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)


# ---- State ----
class RiskState(TypedDict):
    headline: str
    candidate_suppliers: List[str]
    context: str
    tool_decision: Optional[Literal["retrieve", "skip"]]
    alert: Optional[str]


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
    suppliers = state["candidate_suppliers"]
    context = f"Context for: {', '.join(suppliers)}" if suppliers else "No context found"
    return {**state, "context": context}


# ---- Node 4: final analysis ----
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

    WWrite a short risk assessment in 2-4 sentences.

    If no supplier is clearly relevant, do not speculate about impact.
    Explicitly state that the signal is inconclusive because no clearly relevant supplier was identified.
    """

    response = model.invoke(prompt)
    alert = response.content

    return {**state, "alert": alert}


# ---- Conditional router ----
def route_after_decision(state: RiskState) -> str:
    return "retrieve" if state["tool_decision"] == "retrieve" else "analyze"


# ---- Graph ----
graph = StateGraph(RiskState)

graph.add_node("infer", infer_suppliers)
graph.add_node("decide", decide_tool_use)
graph.add_node("retrieve", retrieve_context)
graph.add_node("analyze", analyze_risk)

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
graph.add_edge("analyze", END)

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
        print("\nFinal Alert:")
        print(result["alert"])
        #print(result)
        print("-" * 50)
