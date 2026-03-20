from typing import TypedDict, List, Optional
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
    alert: Optional[str]


# ---- Nodes ----
def infer_suppliers(state: RiskState) -> RiskState:
    headline = state["headline"].lower()
    suppliers = []

    if "japan" in headline:
        suppliers.append("Murata")
    if "taiwan" in headline:
        suppliers.append("TSMC")

    print("DEBUG infer_suppliers ->", suppliers)

    return {**state, "candidate_suppliers": suppliers}


def retrieve_context(state: RiskState) -> RiskState:
    print("DEBUG retrieve_context input ->", state["candidate_suppliers"])
    suppliers = state["candidate_suppliers"]
    context = f"Context for: {', '.join(suppliers)}" if suppliers else "No context found"
    return {**state, "context": context}


def analyze_risk(state: RiskState) -> RiskState:
    print("DEBUG analyze_risk suppliers ->", state["candidate_suppliers"])
    print("DEBUG analyze_risk context ->", state["context"])
    headline = state["headline"]
    candidate_suppliers = state["candidate_suppliers"]
    context = state["context"]

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

# ---- Conditional Routing ----
def route_after_infer(state: RiskState) -> str:
    if state["candidate_suppliers"]:
        return "retrieve"
    return "analyze"


# ---- Graph ----
graph = StateGraph(RiskState)

graph.add_node("infer", infer_suppliers)
graph.add_node("retrieve", retrieve_context)
graph.add_node("analyze", analyze_risk)

graph.set_entry_point("infer")
graph.add_conditional_edges(
    "infer",
    route_after_infer,
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
            "alert": None,
        })

        print("\nHeadline:")
        print(h)
        print("\nFinal Alert:")
        #print(result["alert"])
        print(result)
        print("-" * 40)
