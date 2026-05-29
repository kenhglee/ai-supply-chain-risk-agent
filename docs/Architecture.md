# Architecture

## System Purpose

This project is a **supply chain risk monitoring backend**. It watches two classes of signal and turns them into structured risk decisions:

1. **Supplier disruption news** — Google News RSS headlines about key hardware suppliers (TSMC, Foxconn, Murata) are processed through an LLM-powered LangGraph agent and emitted as enriched risk alerts.
2. **Software supply chain events** — GitHub push and pull-request webhooks are evaluated by a rule-based scorer; high-risk events trigger a mock ServiceNow change ticket.

There is no web UI. The system is a set of Python modules deployed as AWS Lambda functions, plus a local MCP server that exposes the GitHub pipeline to AI assistants.

---

## Repository Layout

```
app/
  ingestion/
    rss_ingestion.py          # Fetches Google News RSS; returns raw headline dicts
    github_webhook_receiver.py # Verifies HMAC, normalizes GitHub payload, calls evaluator
  integrations/
    servicenow_mock.py        # Returns a fake ServiceNow CHANGE ticket dict
  storage/
    risk_state_store.py       # Appends / queries risk_decisions.jsonl
    supplier_graph.json       # Knowledge graph: [source, relation, target] triples
    supplier_profiles.json    # Text profiles for FAISS vectorstore
  workflows/
    supplier_risk_agent.py    # LangGraph agent (RSS pipeline)
    github_risk_evaluator.py  # Rule-based risk scorer (GitHub pipeline)

handlers/
  rss_handler/handler.py          # Lambda entry point for RSS pipeline
  github_webhook_handler/handler.py # Lambda entry point for GitHub webhooks

mcp_server.py     # FastMCP server wrapping the GitHub pipeline tools
```

---

## Pipeline 1 — RSS Supplier Risk

### Trigger

AWS EventBridge invokes `handlers/rss_handler/handler.py` on a schedule. Locally, run `python app/workflows/supplier_risk_agent.py`.

### Request Flow

```
EventBridge (schedule)
  → rss_handler.lambda_handler
    → run_supplier_risk_flow()
      → bootstrap_alerts_csv_from_rss()   # fetch RSS, deduplicate, append to alerts.csv
      → for each "new" alert (up to MAX_ALERTS_PER_RUN):
          → is_actionable_alert()          # keyword pre-filter (no LLM call)
          → LangGraph app.invoke()         # 6-node agent (see below)
          → compare_and_update_risk_state() # detect escalation/suppression
      → write_enriched_alerts()           # append to enriched_alerts.csv
      → risk_store.flush()               # persist risk state to CSV or DynamoDB
  → return { statusCode: 200, body: summary_json }
```

### LangGraph Agent

The agent is a compiled `StateGraph` with six nodes:

```
infer → decide ──retrieve──► analyze → validate ──end──► END
                └──skip──────►         └──fallback──► fallback → END
```

| Node | What it does |
|---|---|
| `infer` | Maps headline to candidate suppliers via graph traversal (`has_risk` / `operates_in` / `depends_on` edges) and direct alias matching |
| `decide` | LLM call — returns `"retrieve"` or `"skip"` to gate context retrieval |
| `retrieve` | FAISS similarity search over `supplier_profiles.json`; filters results to inferred suppliers when possible |
| `analyze` | LLM call — returns structured JSON alert (see schema below) |
| `validate` | Checks required fields; routes to `fallback` on failure |
| `fallback` | Replaces alert with a safe `inconclusive` sentinel |

**Module-level initialization**: `supplier_risk_agent.py` builds the FAISS index and instantiates the LLM at import time. `OPENAI_API_KEY` must be set even when `LLM_PROVIDER=bedrock` because `OpenAIEmbeddings` is always used for FAISS.

### Risk State Change Types

After the agent runs, `compare_and_update_risk_state` compares the new alert against the persisted state for `(supplier, risk_type)`:

| Type | Condition |
|---|---|
| `new_alert` | No prior state for this supplier/risk_type pair |
| `escalated` | New risk level is higher than prior |
| `suppressed` | Same risk level as prior (duplicate) |
| `downgraded` | New risk level is lower than prior |
| `inconclusive` | Alert status is not `"ok"`, or fields are missing |

---

## Pipeline 2 — GitHub Webhook Risk Evaluator

### Trigger

GitHub sends `push` and `pull_request` webhook events to the Lambda Function URL exposed by `handlers/github_webhook_handler/handler.py`.

### Request Flow

```
GitHub webhook (POST to Lambda Function URL)
  → github_webhook_handler.lambda_handler
    → process_github_webhook(event)
      → verify HMAC-SHA256 signature (X-Hub-Signature-256)
      → normalize payload → { event_type, repository, push|pull_request, ... }
      → evaluate_github_event_risk(normalized)    # rule-based, no LLM
      → if decision ∈ {review_recommended, manual_review_required}:
          → create_servicenow_ticket()             # mock only
  → return { statusCode: 200, normalized_event, decision, ticket }
```

Note: the webhook handler does **not** persist decisions to `risk_decisions.jsonl`. Persistence is handled only by the MCP server.

### Risk Score Rules

| Event | Branch / Target | Decision | Score |
|---|---|---|---|
| `push` | `refs/heads/main` | `manual_review_required` | 80 |
| `push` | `refs/heads/release/*` | `manual_review_required` | 85 |
| `push` | other | `allow` | 20 |
| `pull_request` | `main` | `review_recommended` | 60 |
| `pull_request` | `release/*` | `manual_review_required` | 75 |
| `pull_request` | other | `allow` | 25 |
| other event type | — | `ignore` | 0 |

---

## MCP Server

`mcp_server.py` runs a FastMCP server (`supply-chain-risk`) that exposes the GitHub pipeline as tools for AI assistants. It is a separate process, not called by the Lambda webhook handler.

### Tools

| Tool | Description |
|---|---|
| `evaluate_github_event_risk` | Calls the rule-based evaluator and persists the decision to `risk_decisions.jsonl` |
| `get_recent_risk_decisions` | Returns the N most recent decisions from `risk_decisions.jsonl` (default 20) |
| `create_mock_servicenow_ticket` | Creates a mock ServiceNow ticket for a prior decision and persists the result |
| `get_decisions_requiring_review` | Returns decisions where `decision` is `manual_review_required` or `review_recommended` (default 10) |

Start it with `python mcp_server.py` (blocks on stdin waiting for an MCP client).

---

## Data Models and Storage

### LangGraph State (`RiskState`)

```python
class RiskState(TypedDict):
    headline: str
    candidate_suppliers: List[str]
    context: str
    tool_decision: Optional[Literal["retrieve", "skip"]]
    alert: Optional[dict]
    is_valid: Optional[bool]
```

### LLM Alert Output (from `analyze` node)

```json
{
  "status": "ok" | "inconclusive",
  "supplier": "TSMC" | null,
  "risk_type": "earthquake" | "flood" | "strike" | "sanctions" | "outage" | "export_controls" | ...,
  "risk_level": "High" | "Medium" | "Low" | null,
  "impact": "...",
  "recommended_action": "...",
  "relevant_supplier_context": "..."
}
```

### Enriched Alert Row (`enriched_alerts.csv`)

`processed_at`, `alert_id`, `headline`, `source`, `status`, `tool_decision`, `candidate_suppliers`, `final_status`, `supplier`, `risk_type`, `risk_level`, `change_type`, `change_message`, `impact`, `recommended_action`, `relevant_supplier_context`

### Supplier Risk State

Two backends, selected by `RISK_STATE_BACKEND`:

- **CSV** (`risk_state.csv`): `supplier`, `risk_type`, `current_risk_level`, `last_headline`, `last_seen_at`
- **DynamoDB** (table `supplier_risk_state`): same fields; PK = `{supplier}#{risk_type}`

### Risk Decision Record (`risk_decisions.jsonl`)

One JSON object per line, appended on write:

```json
{
  "id": "<uuid hex>",
  "timestamp": "<ISO-8601 UTC>",
  "normalized_event": { "event_type": "...", "repository": "...", ... },
  "decision": { "decision": "...", "risk_score": 0, "reason": "..." },
  "ticket": { "ticket_id": "MOCK-CHG-...", ... } | null
}
```

### Supplier Knowledge Base

- `supplier_graph.json` — list of `[source, relation, target]` triples. Relations: `operates_in`, `depends_on`, `supplies`, `has_risk`. Covers TSMC, Murata, Foxconn.
- `supplier_profiles.json` — free-text profiles for each supplier; loaded into FAISS at startup.

---

## Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | — | Always required (used for FAISS embeddings even when `LLM_PROVIDER=bedrock`) |
| `LLM_PROVIDER` | `openai` | `openai` or `bedrock` |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model name for OpenAI provider |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Model ID for Bedrock provider |
| `RISK_STATE_BACKEND` | `csv` | `csv` or `dynamodb` |
| `RISK_STATE_FILE` | `risk_state.csv` | Path when backend is `csv` |
| `RISK_STATE_TABLE` | `supplier_risk_state` | DynamoDB table name |
| `OUTPUT_MODE` | `csv` | `csv` (writes local files) or `lambda` (skips file writes) |
| `MAX_ALERTS_PER_RUN` | `1` | Limits LLM calls per invocation |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC secret for GitHub webhook signature verification |
| `AWS_DEFAULT_REGION` | `us-west-2` | AWS region for DynamoDB and Bedrock |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## Current Limitations

- **Hard-coded supplier set**: the knowledge base covers only TSMC, Murata, and Foxconn. Adding suppliers requires editing `supplier_graph.json`, `supplier_profiles.json`, and `SUPPLIER_ALIASES` in `supplier_risk_agent.py`.
- **OPENAI_API_KEY always required**: `supplier_risk_agent.py` calls `OpenAIEmbeddings()` unconditionally at import time for the FAISS vectorstore, regardless of `LLM_PROVIDER`.
- **Module-level side effects**: the graph JSON, FAISS index, and LLM client are constructed at import time, making the module slow to load and difficult to test in isolation.
- **Local file storage only**: `risk_decisions.jsonl` and `enriched_alerts.csv` are local files on the Lambda instance. They are not shared across invocations or instances and are lost on cold-start container recycling.
- **`seen_headlines.json` grows unbounded**: no TTL or pruning; over time the file will grow large.
- **No real ServiceNow integration**: `create_servicenow_ticket` returns a mock dict; nothing is sent externally.
- **Webhook handler does not persist decisions**: the Lambda GitHub webhook handler calls the rule-based evaluator and creates a mock ticket, but does not write to `risk_decisions.jsonl`. Only the MCP server persists decisions.
- **RSS feed is fixed**: `rss_ingestion.py` hard-codes a single Google News query for `TSMC OR Foxconn OR Murata`.
- **No retry or dead-letter handling**: Lambda failures surface as unhandled exceptions with no DLQ configured.
- **No observability layer**: structured JSON logs are emitted to CloudWatch, but there is no trace ID threading across nodes, no per-node latency, and no way to replay or inspect individual agent runs.

---

## Next Roadmap Item: Risk Trace / Observability Panel

The immediate next work item is a **Risk Trace panel** — a per-alert trace view that surfaces what happened inside each LangGraph run.

**Motivation**: when an alert is emitted as `inconclusive` or an unexpected `risk_level`, there is currently no way to see which node made which decision, what the LLM was given, or how long each node took. All diagnostic data lives in `enriched_alerts.csv` at the row level; there is no node-level record.

**Planned shape**:
- Capture a trace record alongside each enriched alert: node sequence, per-node inputs/outputs, LLM prompts and raw responses, timing.
- Store traces in a structured file (e.g. `risk_traces.jsonl`) keyed by `alert_id`.
- Surface traces via a new MCP tool (`get_risk_trace`) so an AI assistant can explain a specific alert's reasoning chain on demand.
- Longer term: a lightweight local UI (or Jupyter notebook) that renders the trace as a step-by-step panel — node name, decision, context snippet, latency.
