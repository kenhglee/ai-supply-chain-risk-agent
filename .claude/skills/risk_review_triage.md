---
name: risk_review_triage
description: Identify, prioritize, and recommend actions for software supply chain risk events requiring operator attention.

allowed-tools:
  - get_recent_risk_decisions
  - get_decisions_requiring_review
  - evaluate_github_event_risk
  - create_mock_servicenow_ticket
---

# Risk Review Triage Skill

Use this skill when the user asks to review recent software supply chain risk events, GitHub risk decisions, manual-review items, or mock ServiceNow change tickets.

---

## Goal

Surface **actionable risk**, reduce noise, and provide **clear, prioritized next steps** with reasoning.

Focus on:
- What requires attention *now*
- What can be safely deferred
- What is likely noise or test activity

---

## Available MCP Tools

- get_recent_risk_decisions
- get_decisions_requiring_review (if available)
- evaluate_github_event_risk
- create_mock_servicenow_ticket

---

## Triage Principles

1. **Prioritize by impact, not volume**
   - Direct changes to protected branches (e.g., `main`) are highest priority
   - External dependency changes rank above internal refactors

2. **Weight recency heavily**
   - Recent events take precedence over older duplicates

3. **Collapse duplicates into patterns**
   - Identify repeated events and summarize them as a single pattern
   - Avoid listing redundant entries unless necessary

4. **Distinguish real risk vs test/demo activity**
   - Call out repeated identical patterns across short intervals
   - Flag likely non-production signals

5. **Be decisive**
   - Prefer a clear recommendation over neutral language
   - Avoid overusing “monitor” unless justified

---

## Default Workflow

1. Retrieve relevant decisions:
   - Prefer `get_decisions_requiring_review`
   - Otherwise use `get_recent_risk_decisions` and filter:
     - `manual_review_required`
     - `review_recommended`

2. Group by:
   - decision type
   - event pattern (e.g., repeated pushes, same PR)

3. Identify:
   - most recent instances
   - representative samples for repeated patterns

4. For each pattern, summarize:
   - repository
   - event type
   - branch / target branch
   - risk score range
   - reason
   - timestamps (latest + range if repeated)
   - persisted IDs (latest 1–2 only)

5. Recommend a **single clear action per pattern**:
   - escalate (create mock ServiceNow ticket)
   - monitor with condition
   - no action (with justification)

6. Ask before creating tickets unless explicitly requested

---

## Decision Guidance

Use the following bias:

- **Escalate (ticket)**:
  - repeated high-risk actions on protected branches
  - unexplained or unexpected behavior
  - recent activity with potential production impact

- **Monitor**:
  - known patterns with low immediate risk
  - activity pending additional context (e.g., PR review)

- **No action**:
  - clearly repetitive test/demo data
  - already understood and low-risk patterns

---

## Decision Bias (Add)

When uncertainty exists between "monitor" and "escalate", prefer escalation for recent, high-risk events affecting protected branches.

Avoid neutral recommendations when potential production impact exists.

## Safety Rules

- Do not mutate GitHub
- Do not call real ServiceNow
- Do not create tickets unless explicitly requested
- Treat MCP as an interaction layer, not the webhook path
- Prefer read-only tools first

---

## Response Format

Start with **Priority 1 — Immediate Attention** (if any), then Triage Summary, then Key Observations.

Do not lead with analysis when actionable risk exists.

### Triage Summary

- Total events reviewed
- Manual review required
- Review recommended
- No action / informational

---

### Key Observations

- Highlight patterns (not individual events)
- Call out anomalies or unusual concentration
- Identify likely test/demo artifacts

---

### Priority Actions

Organize by priority:

#### Priority 1 — Immediate Attention
- Pattern description
- Why it matters
- Recommended action

#### Priority 2 — Review / Monitor
- Pattern description
- Condition for escalation

---

## Confidence (Add)

For each priority action, include:

- Confidence: High / Medium / Low

Base confidence on:
- pattern consistency
- presence of test indicators
- recency and impact signals

### Optional Actions

- Suggested ticket creation (explicit IDs)
- Follow-up checks if needed
