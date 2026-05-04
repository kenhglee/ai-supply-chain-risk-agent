---
name: risk_review_triage
description: >
  Use this skill to triage software supply chain risk events, GitHub risk decisions,
  manual-review queues, or mock ServiceNow change tickets. Trigger whenever the user
  asks to review recent risk decisions, check what needs attention, escalate supply
  chain events, or create triage summaries. Also trigger for phrases like "what needs
  review", "any manual review items", "run triage", or "check GitHub risk events".

verbosity: standard
allowed-tools:
  - get_recent_risk_decisions
  - get_decisions_requiring_review
  - evaluate_github_event_risk
  - create_mock_servicenow_ticket
---

# Risk Review Triage Skill

Surface actionable risk, reduce noise, and provide prioritized next steps with reasoning.

---

## Triage Principles

1. **Prioritize by impact, not volume** — direct changes to protected branches rank highest; external dependency changes above internal refactors
2. **Weight recency heavily** — recent events take precedence over older duplicates
3. **Collapse duplicates into patterns** — summarize repeated events as a single pattern
4. **Distinguish real risk vs test/demo activity** — flag repeated identical patterns at short intervals
5. **Be decisive** — prefer a clear recommendation; avoid "monitor" unless justified

---

## Default Workflow

1. Retrieve decisions via `get_decisions_requiring_review` (preferred) or `get_recent_risk_decisions` filtered to `manual_review_required` and `review_recommended`
2. Group by: decision type, event pattern (repeated pushes, same PR)
3. For each pattern summarize: repository, event type, branch/target, risk score range, reason, timestamps, persisted IDs (latest 1–2 only)
4. Recommend **one clear action per pattern** — see Decision Guidance below
5. Ask before creating tickets unless explicitly requested

---

## Decision Guidance

**Escalate (P1 — create mock ticket)** when ANY of the following:
- Direct push to a protected branch (e.g., `main`)
- Bypass of required review or approval controls
- High-risk change with immediate security or production impact
- Suspicious behavior (unexpected actor, unusual change pattern)
- Imminent merge/deploy without review on a high-risk change

**Monitor (P2)** when:
- PR-based event following normal workflow
- Risk score is `review_recommended` (not `manual_review_required`)
- No control bypass observed; awaiting additional context

**No action** when:
- Clearly repetitive test/demo data
- Already understood, low-risk pattern

> **Tie-breaking rule**: When uncertain between Monitor and Escalate —
> - PR-based events → default to **Monitor**
> - Direct changes to protected branches → default to **Escalate**

---

## Confidence Labels

Attach a confidence level to each priority action:

| Confidence | Criteria |
|---|---|
| **High** | Consistent pattern, clear risk signal, recent, no test indicators |
| **Medium** | Partial pattern match, or some ambiguity (e.g., test indicators mixed with real activity) |
| **Low** | Sparse data, unclear actor intent, or high uncertainty about production impact |

---

## Response Format

Lead with **Priority 1 — Immediate Attention** if any exist. Follow with Triage Summary, then Key Observations.

### Triage Summary
- Total events reviewed
- Manual review required
- Review recommended
- No action / informational

### Priority Actions

#### Priority 1 — Immediate Attention
- Pattern, why it matters, recommended action, confidence

#### Priority 2 — Review / Monitor
- Pattern, condition for escalation, confidence

### Key Observations
- Highlight patterns, anomalies, test/demo artifacts

---

## Verbosity Modes

### concise
Single-line header per item:
`P{priority} — {action} | {repository} | {event} | {risk_score} ({decision}) | Ticket: {ticket_status}`

Followed by one-line action-oriented reason and confidence label. Omit long explanations, commit details, repeated metadata.

### standard (default)
Structured output with short reasoning, key metadata, minimal explanation.

### detailed
Full reasoning including:
- Why it matters (security implications, contextual interpretation)
- Pattern explanations with supporting evidence
- Explicit assumptions stated
- Representative event IDs and timestamps

---

## Safety Rules

- Do not mutate GitHub
- Do not call real ServiceNow
- Do not create tickets unless explicitly requested
- Prefer read-only tools first