import feedparser
from dotenv import load_dotenv, find_dotenv

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

_ = load_dotenv(find_dotenv())  # loads OPENAI_API_KEY from .env


suppliers = ["TSMC", "Samsung Electronics", "Murata", "Foxconn"]

rss_url = "https://news.google.com/rss/search?q=TSMC+OR+Foxconn+OR+Murata"

feed = feedparser.parse(rss_url)
headlines = [entry.title for entry in feed.entries]

model = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)

parser = JsonOutputParser()

prompt = ChatPromptTemplate.from_template(
"""
You are a supply chain risk analyst.

Given the supplier list and news headlines, identify only meaningful supply-chain risks.

A meaningful risk includes:
- factory shutdown
- logistics disruption
- port congestion
- labor strike
- sanctions
- export controls
- cyberattack
- natural disaster
- capacity constraints
- financial distress

Do not treat normal hiring, investor commentary, stock movement, or general expansion as high risk unless there is a clear operational threat.

Suppliers: {suppliers}
Headlines: {headlines}

{format_instructions}
"""
)

chain = prompt | model | parser
raw = chain.invoke(
    {
        "suppliers": suppliers,
        "headlines": headlines,
        "format_instructions": parser.get_format_instructions(),
    }
)

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

print("\nDaily Supply Chain Risk Summary\n" + "-" * 35)
for item in items:
    print(f"Supplier: {item['supplier']}")
    print(f"Headline: {item['headline']}")
    print(f"Risk Level: {item['risk_level']}")
    print(f"Impact: {item['impact']}")
    print(f"Recommended Action: {item['recommended_action']}")
    print("-" * 35)
