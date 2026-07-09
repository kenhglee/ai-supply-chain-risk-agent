# Design Proposal: From Risk Agent to Interactive Enterprise Decision Copilot

*An evolution from reactive risk detection to enterprise decision support.*

> **Status:** Approved architectural design proposal. Future implementation phases should follow this roadmap unless explicitly revised.

## Context

This repo started as a supply-chain risk agent: two independent event-driven pipelines (RSS supplier-risk via LangGraph, GitHub push/PR risk scoring) plus an MCP layer for AI-assistant interaction. A first-draft `PlannerCopilot` CLI was added (`app/copilot/planner_copilot.py`) — single-session, rule-based, answering three fixed questions about supplier risk.

The ask now is broader: turn this into an **Interactive Enterprise Decision Copilot** planners would use daily — not a rebuild, an evolution. This document is a research-grounded assessment, gap analysis, and phased roadmap. It was produced by directly reading the relevant source (registries, retriever, stores, API, web app, MCP server, the copilot) rather than inferring from names or docs alone.

---

## 1. Assessment — what already exists and is reusable as-is

| Component | File(s) | What it gives us |
|---|---|---|
| RSS risk pipeline | `app/workflows/supplier_risk_agent.py` | LangGraph state machine (infer→decide→retrieve→analyze→validate→fallback), full node-level tracing, escalation/suppression diffing against prior state |
| GitHub risk evaluator | `app/workflows/github_risk_evaluator.py` | Deterministic rule-based scoring, no LLM — cheap, predictable |
| MCP server | `mcp_server.py` | 6 tools already exposing risk eval, decision query, ticket creation, trace lookup/explanation to any MCP client |
| Conversational prior art | `.claude/skills/risk_review_triage.md` | A working human-in-the-loop triage workflow over the MCP tools today (group/dedupe, P1/P2/no-action guidance, "ask before creating tickets") — proof the interaction pattern works, just not standalone |
| Prompt & model registries | `app/prompt_registry.py`, `app/model_registry.py` | Versioned, JSON-file-per-version records with a real `status` field; `get_prompt`/`get_model` are **approval-gated by default** (`require_approved=True` blocks non-`"approved"` versions) |
| Evaluation harness | `app/evaluation/risk_classifier_eval.py`, `evals/risk_classifier/golden_set_v1.jsonl` (15 cases) | Golden-dataset scorecard: pass_rate + per-field accuracy; no CI gate or regression tracking today (by design — "no threshold enforced," per README) |
| Retrieval abstraction | `app/retrieval/retriever.py` | Duck-typed `FaissRetriever`/`BedrockKBRetriever`, swappable via `RETRIEVER_PROVIDER`; `BedrockKBRetriever` is confirmed **live** against a real S3-Vectors-backed Bedrock KB, not a stub |
| Corpus publisher | `app/ingestion/publish_supplier_corpus.py` | One-shot batch publisher turning `supplier_profiles.json` into individually addressable docs for KB ingestion — a template for publishing other corpora |
| Trace & decision ledgers | `app/storage/risk_trace_store.py`, `app/storage/risk_state_store.py` | Dual JSONL/DynamoDB backends (env-var selected), append-only, with read APIs (`get_all`, `get_by_identifier`, `get_requiring_review`) and a human-readable explainer (`build_trace_explanation`) |
| Observability API + UI | `app/api/main.py` (FastAPI, GET-only, confirmed), `web/` (React+Vite+TS, confirmed — `App.tsx`, `api.ts`, zod validation) | A real, working local UI shell already fetching from a real API — currently read-only trace viewing, the natural place to surface a browser-based decision workflow |
| Planner Copilot | `app/copilot/planner_copilot.py` | Rule-based intent classification, constructor-injected dependencies (fully testable), reuses supplier graph/profiles, risk ledger, retriever, ServiceNow tool, decision store; logs every turn with `trace_id`/`parent_trace_id` chaining to `copilot_log.jsonl` |

**Architectural strengths worth preserving:**
- Dual-backend (JSONL local / DynamoDB cloud) pattern applied consistently — new capabilities should follow it, not invent a new store type
- Provider-agnostic retrieval already proven across two real backends (FAISS ↔ Bedrock KB) via one env var
- MCP tool pattern is small, uniform, and already the system's interactive surface
- Registries separate *governance* (status/approval) from *runtime* (env-var override resolution) cleanly
- Everything traces — every decision-relevant action already has an identifier that can be looked up and explained

---

## 2. Gap Analysis

| Dimension | Current state | Gap | Why it matters |
|---|---|---|---|
| **Conversational planning** | `classify_intent` is keyword matching over 3 fixed phrasings; no dialogue beyond `last_recommendation` in memory | No LLM-based understanding, no paraphrase tolerance, no multi-turn chaining beyond one back-reference | Planners phrase things loosely ("what about the EU ones," "compare to last week") — rigid keyword rules break immediately outside the 3 scripted questions |
| **Decision support** | Copilot only reads `risk_state.csv` (RSS/supplier side) | Never touches `get_recent_risk_decisions`/`get_decisions_requiring_review` (GitHub side) — the two pipelines' outputs are never reasoned about together | A planner can't ask one question spanning "supplier risk + code risk" today, even though both ledgers already exist |
| **Semantic enterprise retrieval** | Retriever is real but hard-scoped: `"supplier"` metadata key, top-2 truncation, 3-document corpus | Not domain-parameterized; no broader corpus (policies, past decisions, ticket history) | "Why" answers are capped by 3 supplier profiles — can't ground answers in anything beyond that |
| **Organizational memory** | `CopilotLogStore.append` is write-only — no `get_recent`/`get_by_trace_id` counterpart, unlike the trace store which already has one | No read-back API; session state lives only in memory, gone on restart | Nothing lets a planner or the copilot itself recall "what did we discuss about TSMC last week" |
| **Explainability** | Strong per-pipeline (`build_trace_explanation`, `explain_risk_trace`, copilot's `parent_trace_id` chain) but fragmented across 3 stores with 3 different access patterns | No single explain surface spanning RSS trace store + GitHub decision store + copilot log | A planner has to already know which pipeline produced an answer before they can ask "why" |
| **Governance** | Registries have a real `status`/approval gate | No workflow to *use* it — status is hand-edited JSON; no promotion/rollback function or tool | Governance infrastructure exists but is inaccessible without manually editing files |
| **Human approval** | `create_mock_servicenow_ticket` fires immediately on call; "ask before creating tickets" is prose in a skill file, not enforced code. Also: the GitHub **webhook path never persists decisions** — only the MCP path does (existing asymmetry) | No pending/blocked state before an action executes; one whole ingestion path skips the ledger | Approval is advisory, not systemic — nothing is technically ever blocked |
| **Extensibility** | Clean seams (duck-typed retriever, uniform store pattern, uniform MCP tool pattern) | Copilot isn't exposed via MCP or the API — it's a disconnected CLI, a second "brain" alongside the `risk_review_triage` skill | Two competing interactive surfaces answering from different logic instead of one extensible one |

---

## 3. Design Principles

These principles govern every phase of the roadmap below and every future extension of the copilot:

1. **AI augments human decisions rather than replacing them.** The copilot recommends; planners and reviewers decide.
2. **Every recommendation must be grounded in enterprise evidence.** No answer without a traceable source — a ledger row, a retrieved document, a graph edge.
3. **Consequential actions require human approval.** Anything that changes external state (tickets, promotions) passes through an explicit approval step, not an automatic one.
4. **Every decision should be traceable and explainable.** Every recommendation, action, and approval carries an identifier that can be looked up and explained after the fact.
5. **Components should remain modular, provider-agnostic, and incrementally replaceable.** New capability slots into existing seams (retriever interface, dual-backend stores, MCP tool pattern) rather than introducing new frameworks or coupling.

---

## 4. Roadmap — 8 incremental, independently demoable phases

Each phase ships a capability a planner can actually try, reuses named existing components, and adds no new frameworks/infra/databases.

1. **Copilot as an MCP tool** — add `PlannerCopilot.handle()` as a 7th tool in `mcp_server.py`, same `@mcp.tool()` pattern as the existing 6. *Demo:* any MCP client (Claude Code, Claude Desktop) can converse with the copilot, not just the CLI.

2. **Read-back for the copilot log** — add `get_recent_copilot_turns`/`get_turns_by_trace_id` to `app/copilot/`, mirroring `risk_trace_store.get_all`/`get_by_identifier`; expose as an MCP tool. *Demo:* "What did we discuss about TSMC earlier?" returns prior turns via the `parent_trace_id` chain — first slice of memory.

3. **Cross-pipeline reasoning** — extend the copilot's intents to also call `get_recent_risk_decisions`/`get_decisions_requiring_review` (GitHub decision store) alongside the RSS ledger read, so a single recommendation can synthesize signals from both pipelines. *Demo:* "Anything elevated today — suppliers or code?" returns one merged, source-attributed answer. This is prioritized ahead of the web UI because it proves the copilot can synthesize multiple enterprise signals into one grounded recommendation — the core value claim of the project — before investing in another interaction surface.

4. **Web decision workflow** — add `POST /api/copilot` to `app/api/main.py` (same FastAPI app/CORS pattern) calling `PlannerCopilot.handle`; add a decision-workflow panel to `web/` beside the existing trace viewer, reusing `web/src/api.ts`'s zod-validated fetch pattern. *Demo:* planner opens the existing local React app, sees a new tab, and works the same conversational decision workflow in-browser — including the cross-pipeline answers already proven in Phase 3.

5. **Human-in-the-loop approval gate** — one milestone, delivered as two independent implementation workstreams that land together because the approval gate is only meaningful once every ingestion path actually writes to the ledger it governs:
   - **Workstream A — Human approval workflow:** add a `pending_approval` state to the decision-store record shape (parallel to the registries' existing `status` gate), plus `approve_decision`/`reject_decision` functions and an MCP tool; `_handle_create_ticket` writes `pending_approval` instead of ticketing immediately.
   - **Workstream B — Webhook/ledger consistency:** fix the pre-existing asymmetry where the GitHub webhook path never persists decisions to the ledger (only the MCP path does today), so both ingestion paths feed the same governed record shape.

   *Demo:* requesting a ticket produces a pending record; a reviewer approves it via a separate call; only then does the ticket appear.

6. **Prompt/model governance workflow** — add promotion/rollback functions operating on the existing `status` field in both registries, exposed as an MCP tool; add a copilot intent for "what's live for X." *Demo:* ask which model version is approved for risk analysis; promote a new version without hand-editing JSON.

7. **Enterprise Semantic Knowledge Layer** — evolve the retrieval abstraction from a single supplier-profile lookup into a general layer the copilot can reason across: generalize the retriever's metadata filtering beyond the hardcoded `"supplier"` key, and generalize `publish_supplier_corpus.py` into a reusable ingestion path for enterprise knowledge sources (policies, historical decisions, ticket history, and eventually other enterprise systems). The emphasis is architectural — the copilot gains a durable interface to organizational knowledge, not just a wider search — built on the same `RETRIEVER_PROVIDER` abstraction already proven across FAISS and Bedrock KB. *Demo:* "why" answers cite grounded evidence from multiple enterprise knowledge sources, not just the 3 supplier profiles, through the same swap mechanism already in production.

8. **Regression-aware evaluation gate** — extend the eval harness to persist and diff against a stored baseline (still no hard threshold, consistent with existing "that decision belongs to the team" stance); surface the delta at promotion time (Phase 6). *Demo:* before promoting a prompt/model version, see the pass-rate delta vs. the last approved baseline.

---

## 5. Demo Story

A planner opens a Claude Code / MCP session Tuesday morning: *"Anything I should be worried about this week?"* The copilot (Phase 3) checks both ledgers and returns a ranked, source-attributed list — TSMC escalated (RSS), plus a manual-review-required push-to-main (GitHub, now persisted thanks to the Phase 5 fix).

They drill in: *"Why is TSMC escalated?"* The copilot pulls graph exposure, profile text, and retrieved context (broadened in Phase 7) — the same reasoning `_handle_why` does today, just better grounded — and links back to the full LangGraph trace via `explain_risk_trace`.

They act: *"Open a review ticket for this."* Instead of firing immediately, the copilot logs `pending_approval` (Phase 5) and says a reviewer needs to sign off — the "ask before creating tickets" convention, now enforced structurally instead of living only in a skill's prose. A risk manager approves via the same MCP surface; the ticket appears, logged next to the original recommendation.

By now the same decision workflow is also available from the browser (Phase 4) — a planner who prefers a UI gets the identical merged, cross-pipeline answers without needing an MCP client.

The following Monday: *"What did we do about TSMC last week?"* The copilot replays the full chain via the log's read API (Phase 2) and the decision store — question, evidence, recommendation, approval, ticket — without the planner tracking trace IDs themselves.

Separately, an ML engineer asks which model is live for risk analysis (Phase 6), sees it's `approved`, and promotes a new version only after checking its eval delta against baseline (Phase 8) — a governance action that used to mean hand-editing JSON now happens conversationally.

---

## 6. Presentation Alignment — why this reads as enterprise AI, not a chatbot wrapper

- **Auditability is structural, not bolted on.** The same dual JSONL/DynamoDB, append-only pattern is reused three times (traces, decisions, and — once given read APIs — the copilot log), not three different logging schemes.
- **Governed model/prompt lifecycle.** `resolve_model_runtime` layers env overrides on top of a versioned, status-gated registry; `get_prompt`/`get_model` block unapproved versions by default. The roadmap exposes this gate as a workflow — it doesn't invent governance.
- **Human-in-the-loop as a state transition, not a suggestion.** Today's "ask before creating tickets" is prose in a skill file. Phase 5 turns it into an actual `pending_approval → approved` transition on the same record shape the GitHub pipeline already uses.
- **Separation of concerns is structural.** Retrieval, reasoning (LangGraph nodes / copilot intents), and action (ticket creation, decision persistence) are already distinct modules — new capability slots into existing seams.
- **Provider-agnostic by design, proven twice.** The FAISS↔Bedrock-KB swap and the JSONL↔DynamoDB dual backend are the same abstraction pattern applied consistently — any new capability inherits it for free.

**Net argument:** this is a state machine with governed, swappable components and an audit trail at every decision point, where the LLM is one interchangeable piece behind a registry — not a chat UI in front of an LLM. That distinction, together with the Design Principles above, is what should read as credible to a technical stakeholder evaluating "chatbot wrapper" vs. "decision infrastructure."

---

## Notes on scope

- Two pre-existing items surfaced during research, independent of this roadmap: (1) `risk_state.csv` at the repo root had an uncommitted modification not caused by copilot work — worth checking `git diff risk_state.csv` if still relevant; (2) the GitHub webhook handler path not persisting decisions to the ledger (only the MCP path does) is addressed above as Phase 5, Workstream B, but is a pre-existing asymmetry independent of the rest of this roadmap.
