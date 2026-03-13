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
Given the supplier list and news headlines, identify relevant risks.

Suppliers: {suppliers}

Return ONLY valid JSON as a list of objects with:
- supplier
- headline
- risk_level (Low, Medium, High)
- impact
- recommended_action
Do not include explanations, comments, or text before or after the JSON.
"""
)

chain = prompt | model | parser
items = chain.invoke(
    {
        "suppliers": suppliers,
        "headlines": headlines,
    }
)

print("\nDaily Supply Chain Risk Summary\n" + "-" * 35)
for item in items:
    print(f"Supplier: {item['supplier']}")
    print(f"Headline: {item['headline']}")
    print(f"Risk Level: {item['risk_level']}")
    print(f"Impact: {item['impact']}")
    print(f"Recommended Action: {item['recommended_action']}")
    print("-" * 35)
