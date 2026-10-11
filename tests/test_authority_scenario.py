"""Phase 3D1: runtime authority against one live executor (moto, fake LLMs).

One risk_worker session (one process, dependencies built once) handles two
workflows while an "operator" changes the durable authority item between
commands. The command stream is a generator, so operator actions happen
deterministically between the executor's privileged actions:

  a. the session executor stays alive throughout (same executor_id/pid)
  b. authority ACTIVE v1: T1's act opens a ticket
  c. operator revokes (v2); the executor is not touched
  d. the same executor runs T2 up to act
  e. act is denied by authority: no ticket, no business write, thread at act
  f. operator restores (v3)
  g. the same executor resumes T2: only act re-runs, one ticket, completed
"""
import io
import json
import os
import socket
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
from app.governance.action_ledger import UnconfiguredActionGate  # noqa: E402
from app.storage.checkpoint_store import DynamoCheckpointSaver  # noqa: E402
from app.workers import risk_worker  # noqa: E402
from app.workflows import supplier_risk_agent as agent  # noqa: E402
from app.workflows.risk_workflow import checkpoint_dependencies_from_env  # noqa: E402
from tests.test_risk_workflow import (  # noqa: E402,F401  (env is a pytest fixture)
    CHECKPOINTS,
    HEADLINE,
    MOTO_AVAILABLE,
    REGION,
    SUPPLIER_STATE,
    _trace,
    env,
)

pytestmark = pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")

HEADLINE_T2 = f"{HEADLINE} [second alert]"


def _steps(trace: dict) -> dict:
    return {s["node_name"]: s for s in trace["trace_steps"]}


def test_revoke_and_restore_authority_against_one_live_executor(env, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_TABLE", CHECKPOINTS)
    monkeypatch.setenv("RISK_STATE_BACKEND", "dynamodb")
    monkeypatch.setenv("RISK_STATE_TABLE", SUPPLIER_STATE)
    monkeypatch.delenv("AWS_LAMBDA_LOG_STREAM_NAME", raising=False)

    tickets = []
    real_ticket = agent.create_servicenow_ticket

    def counting_ticket(normalized_event, decision):
        ticket = real_ticket(normalized_event, decision)
        tickets.append(ticket)
        return ticket

    monkeypatch.setattr(agent, "create_servicenow_ticket", counting_ticket)

    out = io.StringIO()
    observed = {}

    def last() -> dict:
        return json.loads(out.getvalue().splitlines()[-1])

    def supplier_item() -> dict:
        [item] = env.supplier_state.scan()["Items"]
        return item

    def calls() -> tuple[int, int, int]:
        return env.triage.calls, env.retriever.calls, env.risk.calls

    def commands():
        # b. authority ACTIVE (v1): T1 runs through act.
        yield json.dumps({"start": HEADLINE})
        t1 = last()["thread_id"]
        yield json.dumps({"resume": t1})
        observed["t1"] = last()

        # c. operator revokes; the session is not told.
        observed["revoked"] = env.authority.revoke(reason="incident: stop ticketing", actor="operator")

        # d/e. same executor: T2 up to act, which is denied.
        yield json.dumps({"start": HEADLINE_T2})
        t2 = last()["thread_id"]
        yield json.dumps({"resume": t2})
        observed["t2_denied"] = last()
        observed["after_denial"] = {
            "next": agent.build_app(
                DynamoCheckpointSaver(CHECKPOINTS, REGION), action_gate=UnconfiguredActionGate()
            ).get_state({"configurable": {"thread_id": t2}}).next,
            "calls": calls(),
            "tickets": len(tickets),
            "supplier": supplier_item(),
        }

        # f. operator restores.
        observed["restored"] = env.authority.restore(reason="incident resolved", actor="operator")

        # g. same executor resumes T2; then a repeat RESUME is a no-op.
        yield json.dumps({"resume": t2})
        observed["t2_completed"] = last()
        yield json.dumps({"resume": t2})
        observed["t2_again"] = last()

    deps = checkpoint_dependencies_from_env()
    assert risk_worker.run_session(commands(), deps, out=out) == 0

    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert all("error" not in line for line in lines), lines

    # a. one live executor: same process for every command.
    executor_id = f"{socket.gethostname()}:{os.getpid()}"
    assert {line["executor_id"] for line in lines} == {executor_id}

    # b. T1 allowed under v1.
    t1 = observed["t1"]
    assert t1["status"] == "completed"
    assert (t1["action_status"], t1["authority_version"]) == ("COMPLETED", 1)

    # c. revocation is durable and versioned.
    assert (observed["revoked"]["status"], observed["revoked"]["version"]) == ("REVOKED", 2)

    # e. denied because of authority state; nothing else changed.
    denied = observed["t2_denied"]
    assert denied["status"] == "authority_denied"
    assert denied["next"] == ["act"]
    assert denied["state_written"] is False
    assert (denied["action_status"], denied["authority_version"]) == ("DENIED", 2)
    after = observed["after_denial"]
    assert after["next"] == ("act",)  # durable thread remains at act
    assert after["calls"] == (2, 2, 2)  # T2 ran analyze once
    assert after["tickets"] == 1  # only T1's ticket
    assert after["supplier"]["last_headline"] == HEADLINE  # no business write for T2

    t2_id = denied["thread_id"]
    action_id = agent.review_ticket_action_id(t2_id)
    denial_trace = _trace(denied["trace_id"])
    assert denial_trace["workflow_event"] == "authority_denied"
    assert (denial_trace["authority_status"], denial_trace["authority_version"]) == ("REVOKED", 2)
    assert denial_trace["action_id"] == action_id
    assert [s["node_name"] for s in denial_trace["trace_steps"]] == [
        "infer", "decide", "retrieve", "analyze", "validate",
    ]

    # f/g. restored; only act re-ran.
    assert (observed["restored"]["status"], observed["restored"]["version"]) == ("ACTIVE", 3)
    completed = observed["t2_completed"]
    assert completed["status"] == "completed"
    assert completed["thread_id"] == t2_id
    assert completed["trace_id"] != denied["trace_id"]
    assert (completed["action_status"], completed["authority_version"]) == ("COMPLETED", 3)
    assert completed["state_written"] is True
    assert calls() == (2, 2, 2)  # no infer/decide/retrieve/analyze re-run

    completed_trace = _trace(completed["trace_id"])
    assert completed_trace["workflow_event"] == "resumed_completed"
    before, after_steps = _steps(denial_trace), _steps(completed_trace)
    assert list(after_steps) == ["infer", "decide", "retrieve", "analyze", "validate", "act"]
    for node in ("infer", "decide", "retrieve", "analyze", "validate"):
        assert after_steps[node] == before[node]
    assert after_steps["act"]["started_at"] >= completed_trace["created_at"]
    assert supplier_item()["last_headline"] == HEADLINE_T2

    # Exactly one side effect for T2's deterministic action_id.
    t2_tickets = [t for t in tickets if f"\"{HEADLINE_T2}\"" in t["description"]]
    assert len(t2_tickets) == 1
    assert len(tickets) == 2
    ledger_item = env.actions.get_item(Key={"action_id": action_id}, ConsistentRead=True)["Item"]
    assert ledger_item["status"] == "COMPLETED"
    assert ledger_item["effect"]["ticket_id"] == t2_tickets[0]["ticket_id"]
    assert ledger_item["effect"]["correlation_id"] == action_id
    assert (ledger_item["denied_count"], ledger_item["attempt_count"]) == (1, 1)
    assert (ledger_item["denied_authority_version"], ledger_item["authority_version"]) == (2, 3)

    # Repeat RESUME: already completed, no new effect.
    again = observed["t2_again"]
    assert again["status"] == "already_completed"
    assert again["action_status"] == "COMPLETED"
    assert len(tickets) == 2
    assert len(env.actions.scan()["Items"]) == 2


def test_session_survives_bad_lines(env, monkeypatch):
    out = io.StringIO()
    lines = ["not json", json.dumps({"resume": "thread-123"}), json.dumps({"delete": "x"}), "",
             json.dumps({"resume": "thread-" + "d" * 32})]
    deps = {"checkpointer": DynamoCheckpointSaver(CHECKPOINTS, REGION), "risk_store": None,
            "action_gate": UnconfiguredActionGate()}

    assert risk_worker.run_session(lines, deps, out=out) == 0

    results = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(results) == 4
    assert all("error" in r for r in results[:3])
    assert results[3]["status"] == "not_found"
    assert len({r["executor_id"] for r in results}) == 1
