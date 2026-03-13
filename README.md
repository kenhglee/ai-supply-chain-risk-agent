# AI Supplier Risk Monitoring Agent

An AI agent that monitors global news signals and identifies potential supply chain disruptions affecting key suppliers.

## Why this project matters
Supply chain teams often learn about disruptions too late. This prototype monitors external signals and turns them into structured risk alerts with recommended actions.

## Architecture
```mermaid
flowchart TD
    A[Google News RSS]
    B[Headline ingestion]
    C[LLM risk analysis]
    D[Structured JSON alerts]
    E[Daily risk summary]

    A --> B --> C --> D --> E

## Features
- Google News RSS ingestion
- AI risk classification
- Supply chain impact analysis
- Recommended mitigation actions

## Tech Stack
- Python
- OpenAI API
- Feedparser
- python-dotenv
- LangChain

## Setup
```bash
pip install -r requirements.txt