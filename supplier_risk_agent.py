import feedparser
import json
from dotenv import load_dotenv, find_dotenv

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

_ = load_dotenv(find_dotenv())  # loads OPENAI_API_KEY from .env

from pathlib import Path

suppliers = ["TSMC", "Samsung Electronics", "Murata", "Foxconn"]

rss_url = "https://news.google.com/rss/search?q=TSMC+OR+Foxconn+OR+Murata&hl=en-US&gl=US&ceid=US:en"

feed = feedparser.parse(rss_url)
headlines = [entry.title.rsplit(" - ", 1)[0] for entry in feed.entries]
risk_keywords = [
    "shutdown", "delay", "shortage", "strike", "sanction"
    "export", "cyber", "earthquake", "flood", "fire",
    "capacity", "bankruptcy", "disruption"
]

headlines = [
    h for h in headlines
    if any(k in h.lower() for k in risk_keywords)
]

MEMORY_FILE = Path("seen_headlines.json")

def load_seen_headlines():
    if MEMORY_FILE.exists():
        with open(MEMORY_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen_headlines(seen):
    with open(MEMORY_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)

seen_headlines = load_seen_headlines()

new_headlines = [h for h in headlines if h not in seen_headlines]

if not new_headlines:
    print("No new headlines to analyze.")
    exit()

headlines = new_headlines


# Fallback if RSS returns empty (common with Google News)
if not headlines:
    headlines = [
        "TSMC reports strong Q4 earnings amid chip demand",
        "Foxconn expands Vietnam factory amid supply chain diversification",
        "Murata faces component shortage due to earthquake in Japan",
    ]
    print("(Using sample headlines - RSS feed returned empty)\n")

model = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)

parser = JsonOutputParser()

# Explicit format: we want a JSON array, not generic instructions
format_spec = """Return ONLY a valid JSON array of objects. Each object must have: supplier, headline, risk_level, impact, recommended_action. If no risks, return []. No other text."""

prompt = ChatPromptTemplate.from_template(
"""
You are a supply chain risk analyst.

Given the supplier list and news headlines, identify supply-chain risks. Include only headlines with a plausible operational, logistics, regulatory, financial, or capacity impact on supply continuity, lead time, or cost. Use Low risk when the signal is weak but still relevant.

Meaningful risks: factory shutdown, logistics disruption, port congestion, labor strike, sanctions, export controls, cyberattack, natural disaster, capacity constraints, financial distress.

Suppliers: {suppliers}
Headlines: {headlines}

{format_spec}
"""
)

chain = prompt | model | parser
try:
    raw = chain.invoke(
        {
            "suppliers": suppliers,
            "headlines": headlines,
            "format_spec": format_spec,
        }
    )
except Exception as e:
    print(f"Model call failed: {e}")
    raw = []

# Normalize: LLM may return a list, a single dict, or a dict with a list inside
if isinstance(raw, list):
    items = raw
elif isinstance(raw, dict):
    for key in ("items", "risks", "results"):
        if key in raw and isinstance(raw[key], list):
            items = raw[key]
            break
    else:
        items = [raw]  # single risk object
else:
    items = []

if not items:
    import json
    print("(No items - raw output below for debugging)\n")
    print("Type:", type(raw).__name__)
    print("Raw:", json.dumps(raw, indent=2, default=str)[:800])
    print()

def get_field(obj, *keys):
    """Get field from dict, trying multiple key names."""
    if not isinstance(obj, dict):
        return "N/A"
    for k in keys:
        if k in obj:
            return obj[k]
    return "N/A"

print("\nDaily Supply Chain Risk Summary\n" + "-" * 35)
if not items:
    print("No risks identified from the current headlines.")
else:
    for item in items:
        if not isinstance(item, dict):
            print(f"Skipping non-dict item: {type(item).__name__} = {repr(item)[:80]}")
            continue
        print(f"Supplier: {get_field(item, 'supplier', 'Supplier')}")
        print(f"Headline: {get_field(item, 'headline', 'Headline')}")
        print(f"Risk Level: {get_field(item, 'risk_level', 'riskLevel', 'Risk Level')}")
        print(f"Impact: {get_field(item, 'impact', 'Impact')}")
        print(f"Recommended Action: {get_field(item, 'recommended_action', 'recommendedAction', 'Recommended Action')}")
        print("-" * 35)

seen_headlines.update(headlines)
save_seen_headlines(seen_headlines)
