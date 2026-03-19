import feedparser
import json
from pathlib import Path
import re

from dotenv import load_dotenv, find_dotenv
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from collections import Counter


# ============================================================
# Environment initialization
# ============================================================

_ = load_dotenv(find_dotenv())


# ============================================================
# Configuration
# ============================================================

MEMORY_FILE = Path("seen_headlines.json")
RSS_URL = "https://news.google.com/rss/search?q=TSMC+OR+Foxconn+OR+Murata&hl=en-US&gl=US&ceid=US:en"
RISK_TERMS = [
    "shutdown", "delay", "shortage", "strike", "sanction",
    "export", "cyber", "earthquake", "flood", "fire",
    "capacity", "bankruptcy", "disruption",
]
FALLBACK_HEADLINES = [
    "TSMC reports strong Q4 earnings amid chip demand",
    "Foxconn expands Vietnam factory amid supply chain diversification",
    "Murata faces component shortage due to earthquake in Japan",
]

FORMAT_SPEC = """
Return ONLY one valid JSON object with these fields:
- supplier
- headline
- risk_level
- impact
- recommended_action
- relevant_supplier_context

If the headline does not indicate a meaningful supply-chain risk, return:
{"skip": true}

Do not include explanations or any text outside the JSON.
"""

# ============================================================
# Utility helpers
# ============================================================


def detect_risk_terms(headline):
    h = headline.lower()
    return [term for term in RISK_TERMS if term in h]


def get_field(obj, *keys):
    """Get field from dict, trying multiple key names."""
    if not isinstance(obj, dict):
        return "N/A"
    for k in keys:
        if k in obj:
            return obj[k]
    return "N/A"


def fill_supplier_fallback(
    items: list[dict],
    headline: str,
    context_suppliers: list,
    suppliers: list[str],
) -> None:
    """Fill in missing or invalid supplier on each item using headline and context."""
    for item in items:
        s = item.get("supplier") or item.get("Supplier") or item.get("supplier_name")
        if not s or str(s).strip() == "" or s not in suppliers:
            for sup in suppliers:
                if sup.lower() in headline.lower():
                    item["supplier"] = sup
                    break
            if item.get("supplier") is None and context_suppliers:
                item["supplier"] = context_suppliers[0]


def normalize_raw_response(raw) -> list:
    """Turn raw LLM/parser output into a list of risk item dicts. Filters out skip=True."""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        if raw.get("skip") is True:
            return []
        for key in ("items", "risks", "results"):
            if key in raw and isinstance(raw[key], list):
                items = raw[key]
                break
        else:
            items = [raw]
    else:
        items = []
    return [i for i in items if isinstance(i, dict) and i.get("skip") is not True]


def normalize_risk_level(value):
    """Normalize LLM risk_level values into one of: High/Medium/Low/Unknown.

    The LLM may return variants like "High risk", "medium", or even nested dicts.
    """
    if value is None:
        return "Unknown"

    # Handle common nested shapes (e.g., {"risk_level": "High"}).
    if isinstance(value, dict):
        for k in ("risk_level", "level", "value", "severity"):
            if k in value:
                value = value[k]
                break
        else:
            return "Unknown"

    # If the model accidentally returns a list, use the first element.
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
        if value is None:
            return "Unknown"

    s = str(value).strip().lower()
    if not s:
        return "Unknown"

    mapping = {
        "high": "High",
        "medium": "Medium",
        "low": "Low",
    }

    # Fast path for exact matches.
    if s in mapping:
        return mapping[s]

    # Handle variants like "high risk" / "medium severity" etc.
    if re.search(r"\bhigh\b", s) or s.startswith("high"):
        return "High"
    if re.search(r"\bmedium\b", s) or s.startswith("medium"):
        return "Medium"
    if re.search(r"\blow\b", s) or s.startswith("low"):
        return "Low"

    return "Unknown"


# ============================================================
# Data loading / input functions
# ============================================================

def load_supplier_profiles(path: str = "supplier_profiles.json") -> list:
    """Load supplier profiles from JSON file."""
    with open(path, "r") as f:
        return json.load(f)


def build_vectorstore(profiles: list) -> tuple:
    """Build FAISS vectorstore from profiles. Returns (vectorstore, list of supplier names)."""
    docs = [
        Document(
            page_content=f"{item['supplier']} - {item['profile']}",
            metadata={"supplier": item["supplier"]},
        )
        for item in profiles
    ]
    embeddings = OpenAIEmbeddings()
    vectorstore = FAISS.from_documents(docs, embeddings)
    suppliers = [item["supplier"] for item in profiles]
    return vectorstore, suppliers


def fetch_headlines(
    rss_url: str = RSS_URL,
    risk_terms: list[str] | None = None,
    fallback: list[str] | None = None,
) -> list[str]:
    """Fetch headlines from RSS and filter by risk keywords. Use fallback if feed is empty."""
    risk_terms = risk_terms or RISK_TERMS
    fallback = fallback or FALLBACK_HEADLINES
    feed = feedparser.parse(rss_url)
    headlines = [entry.title.rsplit(" - ", 1)[0] for entry in feed.entries]
    headlines = [h for h in headlines if any(k in h.lower() for k in risk_terms)]
    if not headlines:
        print("(Using sample headlines - RSS feed returned empty)\n")
        return fallback
    return headlines


def load_seen_headlines(memory_file: Path = MEMORY_FILE) -> set:
    """Load set of already-seen headline strings."""
    if memory_file.exists():
        with open(memory_file, "r") as f:
            return set(json.load(f))
    return set()


# ============================================================
# Graph Traversal helpers
# ============================================================


def load_graph(path="supplier_graph.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_suppliers_exposed_to_risk(graph_edges, risk_term):
    """Return suppliers linked to a risk through simple graph relationships."""
    exposed_entities = {
        source for source, rel, target in graph_edges
        if rel == "has_risk" and target.lower() == risk_term.lower()
    }

    suppliers = {
        source for source, rel, target in graph_edges
        if target in exposed_entities and rel in {"operates_in", "depends_on"}
    }

    return list(suppliers)


# ============================================================
# LLM analysis helpers
# ============================================================

def build_risk_chain():
    """Build the LangChain prompt | model | parser chain for risk analysis."""
    model = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)
    parser = JsonOutputParser()
    prompt = ChatPromptTemplate.from_template(
        """
You are a supply chain risk analyst.

Given the supplier list, one news headline, and relevant supplier context, identify whether the headline indicates a meaningful supply-chain risk.

Include only risks with a plausible operational, logistics, regulatory, financial, or capacity impact on supply continuity, lead time, or cost. Use Low risk when the signal is weak but still relevant.

Meaningful risks include: factory shutdown, logistics disruption, port congestion, labor strike, sanctions, export controls, cyberattack, natural disaster, capacity constraints, and financial distress.

You must set "supplier" to exactly one of: {suppliers}. Choose the single supplier most relevant to the headline and context, and use the supplier name exactly as listed.

Supplier list: {suppliers}
Headline: {headline}
Graph-inferred candidate suppliers:
{candidate_suppliers}
Relevant supplier context:
{context}

{format_spec}
"""
    )
    return prompt | model | parser

def analyze_headline(chain, vectorstore, headline: str, suppliers: list[str]) -> list[dict]:
    """Run risk analysis for one headline. Returns list of risk item dicts (may be empty)."""
    risk_terms = detect_risk_terms(headline)
    graph_edges = load_graph()
    candidate_suppliers = set()
    for term in risk_terms:
        candidate_suppliers.update(find_suppliers_exposed_to_risk(graph_edges, term))

    relevant_docs = vectorstore.similarity_search(
        " ".join(list(candidate_suppliers) + [headline]), 
        k=1
    )
    context = "\n\n".join(doc.page_content for doc in relevant_docs)
    
    try:
        raw = chain.invoke({
            "suppliers": suppliers,
            "headline": headline,
            "candidate_suppliers": ", ".join(candidate_suppliers) if candidate_suppliers else "None",
            "context": context,
            "format_spec": FORMAT_SPEC,
        })
    except Exception as e:
        print(f"Model call failed: {e}")
        return []
    items = normalize_raw_response(raw)
    fill_supplier_fallback(items, headline, context, suppliers)
    return items

# ============================================================
# Reporting / output functions
# ============================================================

def save_seen_headlines(seen: set, memory_file: Path = MEMORY_FILE) -> None:
    """Persist set of seen headlines to JSON."""
    with open(memory_file, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def print_summary(alerts: list) -> None:
    """Print the daily supply chain risk summary to stdout."""
    print("\nDaily Supply Chain Risk Summary\n" + "-" * 35)
    if not alerts:
        print("No risks identified from the current headlines.")
        return
    risk_counts = Counter(
        normalize_risk_level(
            get_field(alert, "risk_level", "riskLevel", "severity", "level", "risk")
        )
        for alert in alerts
    )
    supplier_counts = Counter(alert.get("supplier", "Unknown") for alert in alerts)

    print(f"New alerts: {len(alerts)}")
    print(f"High risk: {risk_counts.get('High', 0)}")
    print(f"Medium risk: {risk_counts.get('Medium', 0)}")
    print(f"Low risk: {risk_counts.get('Low', 0)}")
    print(f"Affected suppliers: {', '.join(supplier_counts.keys())}")

    print("\nDetailed Alerts")
    print("-" * 35)

    for item in alerts:
        print(f"Supplier: {get_field(item, 'supplier')}")
        print(f"Headline: {get_field(item, 'headline')}")
        print(
            f"Risk Level: {get_field(item, 'risk_level', 'riskLevel', 'severity', 'level', 'risk')}"
        )
        print(f"Impact: {get_field(item, 'impact')}")
        print(f"Recommended Action: {get_field(item, 'recommended_action')}")
        print(f"Relevant Supplier Context: {get_field(item, 'relevant_supplier_context')}")
        print("-" * 35)

# ============================================================
# Main pipeline
# ============================================================

def run_pipeline() -> None:
    """Load data, fetch headlines, analyze risks, print summary, and persist seen headlines."""

    profiles = load_supplier_profiles()
    vectorstore, suppliers = build_vectorstore(profiles)
    headlines = fetch_headlines()
    seen_headlines = load_seen_headlines()
    new_headlines = [h for h in headlines if h not in seen_headlines]
    if not new_headlines:
        print("No new headlines to analyze.")
        return
    chain = build_risk_chain()
    results = []
    for headline in new_headlines:
        items = analyze_headline(chain, vectorstore, headline, suppliers)
        results.extend(items)
        seen_headlines.add(headline)
    print_summary(results)
    save_seen_headlines(seen_headlines)


def main() -> None:
    run_pipeline()


if __name__ == "__main__":
    main()
