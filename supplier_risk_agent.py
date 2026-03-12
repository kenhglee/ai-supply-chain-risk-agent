import json
from openai import OpenAI

# Set your API key in the environment:
# export OPENAI_API_KEY="your_key_here"
from dotenv import load_dotenv, find_dotenv
_ = load_dotenv(find_dotenv()) # read local .env file
openai.api_key = os.environ['OPENAI_API_KEY']

client = OpenAI()

suppliers = ["TSMC", "Samsung Electronics", "Murata", "Foxconn"]

import feedparser

rss_url = "https://news.google.com/rss/search?q=TSMC+OR+Foxconn+OR+Murata"

feed = feedparser.parse(rss_url)

prompt = f"""
You are a supply chain risk analyst.
Given the supplier list and news headlines, identify relevant risks.

Suppliers: {suppliers}
Headlines: {headlines}

Return ONLY valid JSON as a list of objects with:
- supplier
- headline
- risk_level (Low, Medium, High)
- impact
- recommended_action
Do not include explanations, comments, or text before or after the JSON.
"""

response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": prompt}],
    temperature=0.2,
)

content = response.choices[0].message.content

if not content:
    raise ValueError("Model returned empty content")

clean = content.strip()

# Remove markdown code fences if present
if clean.startswith("```"):
    clean = clean.removeprefix("```json").removeprefix("```").strip()
    if clean.endswith("```"):
        clean = clean[:-3].strip()

try:
  items = json.loads(clean)
except json.JSONDecodeError as e:
  print("Failed to parse JSON.")
  print("repr(content) =", repr(clean))
  raise e

print("\nDaily Supply Chain Risk Summary\n" + "-" * 35)
for item in items:
    print(f"Supplier: {item['supplier']}")
    print(f"Headline: {item['headline']}")
    print(f"Risk Level: {item['risk_level']}")
    print(f"Impact: {item['impact']}")
    print(f"Recommended Action: {item['recommended_action']}")
    print("-" * 35)
