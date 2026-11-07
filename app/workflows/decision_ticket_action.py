"""Governed mock ServiceNow ticket for a stored GitHub risk decision.

The MCP tool create_mock_servicenow_ticket takes only a decision ID. Every
ticket field comes from the stored decision record, never from the caller,
and the ticket is opened through the production action gate (ActionLedger).

Checks, in order (each rejection creates and claims nothing):

  1. the ID must name a stored decision (invalid_decision_id,
     decision_not_found)
  2. the stored decision must be consistent with the stored event:
     re-running the deterministic GitHub evaluator on the record's
     normalized_event must reproduce its decision, risk_score and reason
     (decision_inconsistent_with_event). This rejects records whose decision
     fields were written independently of their event, such as records from
     the old tool signature or copilot records. It does NOT show the record
     is genuine: anyone who can write the decision store can write a
     consistent record (an event plus the evaluator's output for it) and so
     obtain a ticket for any event they choose.
  3. the record must not already carry a ticket (decision_already_ticketed)
  4. the decision must be review_recommended or manual_review_required
     (decision_not_eligible)
  5. the ledger admits the action only if the emergency authority item
     agent#supplier-risk-agent is ACTIVE and the materialized policy item
     policy#supplier-risk-agent#open_review_ticket allows it, together with
     the claim in one transaction. Unset AUTHORITY_TABLE / ACTIONS_TABLE
     denies every request.

Trust boundary: the decision store (app/storage/risk_decisions.jsonl, or the
RISK_DECISIONS_TABLE DynamoDB table) is trusted for event content. Whoever
can write it, including any MCP client through evaluate_github_event_risk,
which persists caller-supplied event fields, decides which events become
eligible for a ticket. Authority and policy decide whether tickets may be
opened at all, not for which events.

Idempotency, per decision ID (ActionLedger semantics; action ID
action#decision#<decision_id>#open_review_ticket):

  - At most one ticket and at most one ticket record per decision.
    The effect (create the ticket, then append the ticket record to the
    decision store) runs only after this call claims the ledger item, and a
    claimed item is never claimed again.
  - COMPLETED: every later request replays the stored ticket
    (status "already_created"), including after a completion whose
    acknowledgment was lost.
  - Claim with unknown outcome (connection error): the ledger re-reads the
    item and proceeds only if the claim names this call. Otherwise the
    error propagates and nothing ran.
  - A concurrent request while another holds the claim gets
    action_in_flight.

Limitations:

  - At most once, not exactly once. If the call fails after the claim and
    before completion is recorded (ticket creation, the ticket-record write
    or the completion write fails, or the process dies), the error
    propagates and the item stays IN_FLIGHT. Every retry then returns
    action_in_flight and the effect is never re-run. A ticket may then exist
    with no ticket record (failure after creation, before the record write),
    or a ticket record may exist with no completed ledger item. There is no
    reconciliation; resolving it is a manual step, and resetting the ledger
    item would run the effect again.
  - Idempotency is per decision ID, not per event. Calling
    evaluate_github_event_risk twice for one event stores two decisions,
    and each can get its own ticket.
  - The decision store is not transactional with the ledger: the ticket
    record is a plain append, written once only because the effect runs
    once.
"""
import os
import re
import socket
import uuid

from app.governance.action_ledger import ActionInFlight, action_gate_from_env
from app.governance.authority import AGENT_SCOPE, AuthorityDenied
from app.integrations.servicenow_mock import create_servicenow_ticket
from app.storage.risk_state_store import (
    REVIEW_REQUIRED_DECISIONS,
    get_risk_decision,
    save_risk_decision,
)
from app.workflows.github_risk_evaluator import evaluate_github_event_risk

# Same capability as the supplier workflow's act node (supplier_risk_agent.REVIEW_TICKET_ACTION):
# one emergency switch and one policy item govern both kinds of review ticket.
REVIEW_TICKET_ACTION = "open_review_ticket"
DECISION_ID_PATTERN = re.compile(r"[0-9a-f]{32}")  # uuid4().hex, as the decision store assigns
DECISION_FIELDS = ("decision", "risk_score", "reason")

CREATED = "created"
ALREADY_CREATED = "already_created"
REJECTED = "rejected"
DENIED = "denied"


def decision_ticket_action_id(decision_id: str) -> str:
    """Deterministic: every request for one decision maps to one ledger item."""
    return f"action#decision#{decision_id}#{REVIEW_TICKET_ACTION}"


def _executor_id() -> str:
    """Audit only, as supplier_risk_agent.get_executor_id."""
    return os.getenv("AWS_LAMBDA_LOG_STREAM_NAME") or f"{socket.gethostname()}:{os.getpid()}"


def _rejected(decision_id, reason: str, message: str) -> dict:
    return {"status": REJECTED, "reason": reason, "decision_id": decision_id, "message": message}


def check_decision(decision_id) -> tuple[dict | None, dict | None]:
    """(record, None) if the decision may be ticketed, else (None, rejection).

    Read-only. Trusts the stored event; see the module docstring.
    """
    if not isinstance(decision_id, str) or not DECISION_ID_PATTERN.fullmatch(decision_id):
        return None, _rejected(decision_id, "invalid_decision_id",
                               "decision_id must be a 32-character lowercase hex record id")
    record = get_risk_decision(decision_id)
    if record is None:
        return None, _rejected(decision_id, "decision_not_found", f"no stored decision {decision_id}")
    stored = record.get("decision") or {}
    recomputed = evaluate_github_event_risk(record.get("normalized_event") or {})
    if any(stored.get(k) != recomputed.get(k) for k in DECISION_FIELDS):
        # Consistency with the stored event only; the event itself is trusted
        # because the decision store is (see the module docstring).
        return None, _rejected(decision_id, "decision_inconsistent_with_event",
                               "stored decision does not match the GitHub risk evaluator's result "
                               "for its stored event")
    if record.get("ticket") is not None:
        return None, _rejected(decision_id, "decision_already_ticketed",
                               "record already carries a ticket; pass the decision record's id")
    if stored.get("decision") not in REVIEW_REQUIRED_DECISIONS:
        return None, _rejected(decision_id, "decision_not_eligible",
                               f"decision {stored.get('decision')!r} does not require review")
    return record, None


def open_ticket_for_decision(decision_id, *, gate=None) -> dict:
    """Open (or replay) the governed review ticket for one stored decision."""
    record, rejection = check_decision(decision_id)
    if rejection:
        return rejection
    gate = gate if gate is not None else action_gate_from_env()
    normalized_event, decision = record["normalized_event"], record["decision"]
    action_id = decision_ticket_action_id(decision_id)

    def effect() -> dict:
        ticket = {**create_servicenow_ticket(normalized_event, decision),
                  "correlation_id": action_id, "decision_id": decision_id}
        save_risk_decision(normalized_event, decision, ticket)
        return ticket

    try:
        result = gate.execute(
            action_id,
            scope=AGENT_SCOPE,
            context={
                "action_type": REVIEW_TICKET_ACTION,
                "thread_id": f"decision#{decision_id}",  # the ledger's grouping key; no workflow thread here
                "trace_id": uuid.uuid4().hex,
                "executor_id": _executor_id(),
                "subject": {
                    "decision_id": decision_id,
                    "repository": normalized_event.get("repository"),
                    "event_type": normalized_event.get("event_type"),
                    "decision": decision.get("decision"),
                },
            },
            effect=effect,
        )
    except AuthorityDenied as denied:
        return {"status": DENIED, "reason": "authority_denied", "decision_id": decision_id,
                "action_id": action_id, "layer": denied.layer,
                "authority_status": denied.authority_status, "policy_status": denied.policy_status,
                "message": str(denied)}
    except ActionInFlight as in_flight:
        return _rejected(decision_id, "action_in_flight", str(in_flight))

    return {
        "status": ALREADY_CREATED if result["replayed"] else CREATED,
        "decision_id": decision_id,
        "action_id": action_id,
        "ticket": result["effect"],
        "authority_version": result["authority_version"],
        "policy_version": result["policy_version"],
    }
