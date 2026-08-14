# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the RSS supplier risk pipeline locally (requires OPENAI_API_KEY)
python app/workflows/supplier_risk_agent.py

# Run individual test scripts
python tests/test_rss_locally.py
python tests/test_github_webhook_locally.py
python tests/test_lambda_handler_locally.py

# Start the MCP server (blocks on stdin waiting for a client)
python mcp_server.py

# Test MCP tools programmatically via the client
python tests/test_mcp.py

# Docker build for Lambda deployment
docker buildx build --platform linux/amd64 --provenance=false -t supplier-risk-agent:latest .
```

## Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | — | Required for local RSS pipeline (also needed for FAISS embeddings even when `LLM_PROVIDER=bedrock`) |
| `LLM_PROVIDER` | `openai` | `openai`, `bedrock`, or `bifrost` |
| `OPENAI_MODEL` | `gpt-4o-mini` | |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | |
| `BIFROST_MODEL_ID` | — | Model name passed through the Bifrost gateway (e.g. `bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0`); used when `LLM_PROVIDER=bifrost` |
| `BIFROST_BASE_URL` | `http://localhost:8080/langchain` | Bifrost LangChain-compatible gateway endpoint |
| `BIFROST_API_KEY` | `dummy-key` | Bifrost does not validate this; any placeholder value works |
| `RISK_STATE_BACKEND` | `csv` | `csv` or `dynamodb` |
| `RISK_STATE_FILE` | `risk_state.csv` | Used when backend is `csv` |
| `RISK_STATE_TABLE` | `supplier_risk_state` | DynamoDB table name |
| `OUTPUT_MODE` | `csv` | `csv` (local) or `lambda` (cloud) |
| `MAX_ALERTS_PER_RUN` | `1` | Limits LLM calls per run |
| `GITHUB_WEBHOOK_SECRET` | — | Required for webhook handler signature verification |
| `AWS_DEFAULT_REGION` | `us-west-2` | |
| `TRACE_STORE_BACKEND` | `jsonl` | `jsonl` (local/dev) or `dynamodb` (Lambda) |
| `RISK_TRACES_TABLE` | `risk_traces` | DynamoDB table name for trace records |
| `DECISION_STORE_BACKEND` | `jsonl` | `jsonl` (local/dev) or `dynamodb` (Lambda) |
| `RISK_DECISIONS_TABLE` | `risk_decisions` | DynamoDB table name for risk decisions |
| `RETRIEVER_PROVIDER` | `faiss` | `faiss` (default) or `bedrock_kb` |
| `BEDROCK_KB_ID` | — | Required when `RETRIEVER_PROVIDER=bedrock_kb` |
| `BEDROCK_KB_TOP_K` | `4` | Number of KB results to request (bedrock_kb only) |

## Architecture

The project contains two independent event-driven pipelines and an MCP server layer:

### 1. RSS Supplier Risk Pipeline (`app/workflows/supplier_risk_agent.py`)

A LangGraph state machine that processes news headlines into structured supplier risk alerts. The graph has six nodes:

```
infer → decide → [retrieve] → analyze → validate → [fallback] → END
```

- **infer**: maps headlines to candidate suppliers via `app/storage/supplier_graph.json` (graph traversal) and alias matching
- **decide**: LLM call — returns `retrieve` or `skip` to gate context retrieval
- **retrieve**: FAISS similarity search over `app/storage/supplier_profiles.json`
- **analyze**: LLM call — returns structured JSON alert with supplier, risk_type, risk_level, impact
- **validate**: checks required fields; routes to fallback on failure

**Important**: `supplier_risk_agent.py` has module-level initialization that runs at import time — it loads the graph JSON, builds the FAISS index (calling `OpenAIEmbeddings()`), and instantiates the LLM. `OPENAI_API_KEY` must be set even when using Bedrock as the LLM provider.

Supplier risk state is persisted across runs to detect escalations/suppressions. State change types: `new_alert`, `escalated`, `suppressed`, `downgraded`, `inconclusive`.

### 2. GitHub Risk Evaluator (`app/workflows/github_risk_evaluator.py`)

Pure rule-based, no LLM. Scores push and pull_request events by branch:
- Direct push to `main` → `manual_review_required` (80)
- Push to `release/*` → `manual_review_required` (85)
- PR targeting `main` → `review_recommended` (60)
- PR targeting `release/*` → `manual_review_required` (75)

### 3. MCP Server (`mcp_server.py`)

Wraps the GitHub pipeline as three MCP tools via FastMCP (`supply-chain-risk` server):
- `evaluate_github_event_risk` 
    → stateless evaluation + persists decision
- `get_recent_risk_decisions` 
    → read-only query over decision history
- `create_mock_servicenow_ticket` 
    → workflow action tied to a prior decision

GitHub risk decisions are stored in `app/storage/risk_decisions.jsonl` (appended, newest-first on read).

**Important**: The MCP server is not invoked by the GitHub webhook handler. 
It is a separate interaction layer intended for AI assistants and manual workflows.

### 4. Lambda Handlers

- `handlers/rss_handler/handler.py` — wraps the RSS pipeline; triggered by EventBridge on a schedule
- `handlers/github_webhook_handler/handler.py` — wraps the GitHub pipeline; exposed via Lambda Function URL; verifies HMAC signature before processing

### Data Flow

```
Google News RSS → rss_handler → LangGraph workflow → Bedrock/OpenAI → DynamoDB / risk_state.csv
GitHub Webhook  → github_webhook_handler → github_risk_evaluator → ServiceNow mock → risk_decisions.jsonl
MCP client      → mcp_server → github_risk_evaluator + servicenow_mock → risk_decisions.jsonl
```

### Why MCP

The MCP server exposes risk evaluation and workflow actions as tools that can be invoked by AI assistants. 
This enables conversational investigation and orchestration on top of the existing event-driven pipelines, 
without coupling assistant interactions to the webhook execution path.

### Risk State Stores

The system maintains two independent state stores:

- Supplier risk state (RSS pipeline) → DynamoDB / CSV
- Software supply chain risk decisions (GitHub pipeline) → JSONL

These are intentionally separate due to different data models and workflows.

## Claude Code Working Preferences

You may proceed without confirmation for:
- Normal code edits
- Creating new source files
- Refactoring existing code
- Frontend development under web/
- Tests and documentation updates

Ask before:
- Installing new packages
- Modifying .env files
- Deleting files
- Git operations (commit, reset, rebase, clean)
- Infrastructure or deployment changes
- Database schema changes

Prefer:
- Small incremental changes
- Show implementation summary after completion
- Validate changes before declaring success