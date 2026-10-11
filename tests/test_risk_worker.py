"""Container/CLI worker: argument handling, routing, and moto end-to-end
interop with the Lambda handler (START in one executor, RESUME in the other)."""
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# supplier_risk_agent builds its LLM clients at import time; no request is made.
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
from app.workers import risk_worker  # noqa: E402
from app.workflows import supplier_risk_agent as agent  # noqa: E402
from handlers.rss_handler import handler  # noqa: E402
from tests.test_risk_workflow import (  # noqa: E402,F401  (env is a pytest fixture)
    CHECKPOINTS,
    HEADLINE,
    MOTO_AVAILABLE,
    SUPPLIER_STATE,
    env,
)

THREAD_ID = "thread-" + "b" * 32


def _stdout_json(capsys) -> dict:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert len(lines) == 1, lines
    return json.loads(lines[0])


@pytest.fixture
def stubs(monkeypatch):
    calls = SimpleNamespace(start=[], resume=[], deps=0)

    def fake_deps():
        calls.deps += 1
        return {"checkpointer": object(), "risk_store": object()}

    def fake_start(row, *, checkpointer, risk_store, pause_before=None):
        calls.start.append({"row": row, "pause_before": pause_before})
        return {"status": "paused", "thread_id": THREAD_ID, "trace_id": "trace-1", "next": ["analyze"],
                "change_type": None, "state_written": False, "final_status": None, "supplier": None}

    def fake_resume(thread_id, *, checkpointer, risk_store):
        calls.resume.append(thread_id)
        return calls.resume_outcome

    calls.resume_outcome = {"status": "completed", "thread_id": THREAD_ID, "trace_id": "trace-2", "next": [],
                            "change_type": "new_alert", "state_written": True, "final_status": "ok",
                            "supplier": "TSMC"}
    monkeypatch.delenv("AWS_LAMBDA_LOG_STREAM_NAME", raising=False)
    monkeypatch.setattr(risk_worker, "checkpoint_dependencies_from_env", fake_deps)
    monkeypatch.setattr(risk_worker, "start_workflow", fake_start)
    monkeypatch.setattr(risk_worker, "resume_workflow", fake_resume)
    return calls


# ---- Arguments ----

@pytest.mark.parametrize("argv", [
    [],
    ["start"],
    ["start", "--headline", "   "],
    ["start", "--headline", HEADLINE, "--thread-id", THREAD_ID],
    ["resume"],
    ["resume", "--thread-id", "thread-123"],
    ["resume", "--thread-id", "thread-" + "B" * 32],
    ["resume", "--thread-id", THREAD_ID, "--headline", HEADLINE],
    ["delete", "--thread-id", THREAD_ID],
])
def test_invalid_arguments_exit_2_without_work(stubs, argv):
    assert risk_worker.main(argv) == risk_worker.EXIT_INVALID
    assert stubs.start == [] and stubs.resume == [] and stubs.deps == 0


# ---- Routing ----

def test_start_pauses_before_analyze(stubs, capsys):
    assert risk_worker.main(["start", "--headline", f" {HEADLINE} "]) == 0

    assert stubs.start == [{"row": {"headline": HEADLINE}, "pause_before": ["analyze"]}]
    out = _stdout_json(capsys)
    assert out["action"] == "start"
    assert out["status"] == "paused"
    assert out["thread_id"] == THREAD_ID
    assert out["next"] == ["analyze"]
    assert out["executor_id"] == agent.get_executor_id()
    assert "request_id" not in out


def test_resume_receives_only_thread_id(stubs, capsys):
    assert risk_worker.main(["resume", "--thread-id", THREAD_ID]) == 0

    assert stubs.resume == [THREAD_ID]
    out = _stdout_json(capsys)
    assert out["action"] == "resume"
    assert out["status"] == "completed"
    assert out["state_written"] is True


def test_resume_not_found_exits_3(stubs, capsys):
    stubs.resume_outcome = {"status": "not_found", "thread_id": THREAD_ID, "trace_id": None, "next": [],
                            "change_type": None, "state_written": False, "final_status": None, "supplier": None}
    assert risk_worker.main(["resume", "--thread-id", THREAD_ID]) == risk_worker.EXIT_NOT_FOUND
    assert _stdout_json(capsys)["status"] == "not_found"


def test_resume_authority_denied_exits_4(stubs, capsys):
    stubs.resume_outcome = {"status": "authority_denied", "thread_id": THREAD_ID, "trace_id": "trace-3",
                            "next": ["act"], "change_type": None, "state_written": False,
                            "final_status": "ok", "supplier": "TSMC", "action_status": "DENIED"}
    assert risk_worker.main(["resume", "--thread-id", THREAD_ID]) == risk_worker.EXIT_AUTHORITY_DENIED
    out = _stdout_json(capsys)
    assert out["next"] == ["act"]
    assert out["action_status"] == "DENIED"


def test_session_reads_commands_from_stdin_with_one_dependency_set(stubs, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"start": HEADLINE}) + "\n" + json.dumps({"resume": THREAD_ID}) + "\n"
    ))
    assert risk_worker.main(["session"]) == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(line["action"], line["status"]) for line in lines] == [("start", "paused"), ("resume", "completed")]
    assert stubs.deps == 1
    assert stubs.start == [{"row": {"headline": HEADLINE}, "pause_before": ["analyze"]}]
    assert stubs.resume == [THREAD_ID]


def test_missing_checkpoint_table_is_configuration_error(monkeypatch):
    monkeypatch.delenv("CHECKPOINT_TABLE", raising=False)
    with pytest.raises(RuntimeError, match="CHECKPOINT_TABLE"):
        risk_worker.main(["resume", "--thread-id", THREAD_ID])


# ---- End to end under moto (fake LLMs) ----

@pytest.fixture
def e2e(env, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_TABLE", CHECKPOINTS)
    monkeypatch.setenv("RISK_STATE_BACKEND", "dynamodb")
    monkeypatch.setenv("RISK_STATE_TABLE", SUPPLIER_STATE)
    return env


@pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")
def test_worker_start_then_worker_resume(e2e, monkeypatch, capsys):
    monkeypatch.delenv("AWS_LAMBDA_LOG_STREAM_NAME", raising=False)
    assert risk_worker.main(["start", "--headline", HEADLINE]) == 0
    started = _stdout_json(capsys)
    assert started["status"] == "paused"

    monkeypatch.setattr(agent, "_retriever", None)
    assert risk_worker.main(["resume", "--thread-id", started["thread_id"]]) == 0
    resumed = _stdout_json(capsys)
    assert resumed["status"] == "completed"
    assert resumed["thread_id"] == started["thread_id"]
    assert resumed["trace_id"] != started["trace_id"]
    assert (e2e.triage.calls, e2e.retriever.calls, e2e.risk.calls) == (1, 1, 1)


@pytest.mark.skipif(not MOTO_AVAILABLE, reason="moto not installed")
def test_handler_start_then_worker_resume_interop(e2e, monkeypatch, capsys):
    """Lambda-shaped START, container-shaped RESUME: only thread_id + tables carry over."""
    monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "2026/10/11/[$LATEST]lambda-stream")
    started = json.loads(handler.lambda_handler(
        {"action": "checkpoint_start", "headline": HEADLINE}, SimpleNamespace(aws_request_id="req-A")
    )["body"])
    assert started["status"] == "paused"
    assert e2e.supplier_state.scan()["Items"] == []

    # Container executor: no Lambda env, no in-process retriever.
    monkeypatch.delenv("AWS_LAMBDA_LOG_STREAM_NAME")
    monkeypatch.setattr(agent, "_retriever", None)
    assert risk_worker.main(["resume", "--thread-id", started["thread_id"]]) == 0
    resumed = _stdout_json(capsys)

    assert resumed["status"] == "completed"
    assert resumed["thread_id"] == started["thread_id"]
    assert resumed["trace_id"] != started["trace_id"]
    assert resumed["executor_id"] != started["executor_id"]
    assert not resumed["executor_id"].startswith("2026/")
    assert resumed["state_written"] is True
    assert len(e2e.supplier_state.scan()["Items"]) == 1
    assert (e2e.triage.calls, e2e.retriever.calls, e2e.risk.calls) == (1, 1, 1)

    # Repeated RESUME is a no-op from either runtime.
    assert risk_worker.main(["resume", "--thread-id", started["thread_id"]]) == 0
    assert _stdout_json(capsys)["status"] == "already_completed"
    again = json.loads(handler.lambda_handler(
        {"action": "checkpoint_resume", "thread_id": started["thread_id"]}, SimpleNamespace(aws_request_id="req-C")
    )["body"])
    assert again["status"] == "already_completed"
    assert e2e.risk.calls == 1
    assert len(e2e.supplier_state.scan()["Items"]) == 1
    assert len(e2e.traces.scan()["Items"]) == 2
