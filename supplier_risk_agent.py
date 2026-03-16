import feedparser
import json
from pathlib import Path

from dotenv import load_dotenv, find_dotenv
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

_ = load_dotenv(find_dotenv())

MEMORY_FILE = Path("seen_headlines.json")
RSS_URL = "https://news.google.com/rss/search?q=TSMC+OR+Foxconn+OR+Murata&hl=en-US&gl=US&ceid=US:en"
RISK_KEYWORDS = [
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
    risk_keywords: list[str] | None = None,
    fallback: list[str] | None = None,
) -> list[str]:
    """Fetch headlines from RSS and filter by risk keywords. Use fallback if feed is empty."""
    risk_keywords = risk_keywords or RISK_KEYWORDS
    fallback = fallback or FALLBACK_HEADLINES
    feed = feedparser.parse(rss_url)
    headlines = [entry.title.rsplit(" - ", 1)[0] for entry in feed.entries]
    headlines = [h for h in headlines if any(k in h.lower() for k in risk_keywords)]
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


def save_seen_headlines(seen: set, memory_file: Path = MEMORY_FILE) -> None:
    """Persist set of seen headlines to JSON."""
    with open(memory_file, "w") as f:
        json.dump(sorted(seen), f, indent=2)


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

Relevant supplier context:
{context}

{format_spec}
"""
    )
    return prompt | model | parser


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


def get_field(obj, *keys):
    """Get field from dict, trying multiple key names."""
    if not isinstance(obj, dict):
        return "N/A"
    for k in keys:
        if k in obj:
            return obj[k]
    return "N/A"


def print_summary(alerts: list) -> None:
    """Print the daily supply chain risk summary to stdout."""
    print("\nDaily Supply Chain Risk Summary\n" + "-" * 35)
    if not alerts:
        print("No risks identified from the current headlines.")
        return
    for item in alerts:
        if not isinstance(item, dict):
            print(f"Skipping non-dict item: {type(item).__name__} = {repr(item)[:80]}")
            continue
        print(f"Supplier: {get_field(item, 'supplier')}")
        print(f"Headline: {get_field(item, 'headline')}")
        print(f"Risk Level: {get_field(item, 'risk_level')}")
        print(f"Impact: {get_field(item, 'impact')}")
        print(f"Recommended Action: {get_field(item, 'recommended_action')}")
        print(f"Relevant Supplier Context: {get_field(item, 'relevant_supplier_context')}")
        print("-" * 35)


def analyze_headline(chain, vectorstore, headline: str, suppliers: list[str]) -> list[dict]:
    """Run risk analysis for one headline. Returns list of risk item dicts (may be empty)."""
    relevant_docs = vectorstore.similarity_search(headline, k=1)
    context = "\n\n".join(
        f"Supplier: {doc.metadata['supplier']}\nContext: {doc.page_content}"
        for doc in relevant_docs
    )
    context_suppliers = [
        doc.metadata.get("supplier") for doc in relevant_docs if doc.metadata.get("supplier")
    ]
    try:
        raw = chain.invoke({
            "suppliers": suppliers,
            "headline": headline,
            "context": context,
            "format_spec": FORMAT_SPEC,
        })
    except Exception as e:
        print(f"Model call failed: {e}")
        return []
    items = normalize_raw_response(raw)
    fill_supplier_fallback(items, headline, context_suppliers, suppliers)
    return items


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
    alerts = []
    for headline in new_headlines:
        items = analyze_headline(chain, vectorstore, headline, suppliers)
        alerts.extend(items)
        seen_headlines.add(headline)
    print_summary(alerts)
    save_seen_headlines(seen_headlines)


def main() -> None:
    run_pipeline()


if __name__ == "__main__":
    main()
