"""Real compiled LangGraph tests with fake LLM and retriever dependencies.

The graph from build_app() runs end to end; only the external calls are
stubbed (triage LLM, risk LLM, retriever). No model or network calls.
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from langgraph.checkpoint.memory import InMemorySaver

# supplier_risk_agent builds its LLM clients at import time; no request is made.
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
from app.workflows import supplier_risk_agent as agent  # noqa: E402

HEADLINE = "TSMC halts production after earthquake in Taiwan"
RETRIEVED_CONTEXT = "TSMC operates fabs in Taiwan with earthquake exposure."

OK_ALERT = {
    "status": "ok",
    "supplier": "TSMC",
    "risk_type": "Earthquake",
    "risk_level": "high",
    "impact": "Wafer output delayed",
    "recommended_action": "Contact supplier",
    "relevant_supplier_context": "Fabs in Taiwan",
}


class FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.response


class FakeRetriever:
    retriever_id = "fake_retriever"
    retriever_version = "v0"
    embedding_provider = "fake"
    top_k = 2

    def __init__(self):
        self.calls = 0

    def retrieve(self, query, suppliers):
        self.calls += 1
        return SimpleNamespace(context=RETRIEVED_CONTEXT)


@pytest.fixture
def fakes(monkeypatch):
    """Install fake LLMs/retriever; returns a configurator for their responses."""
    retriever = FakeRetriever()
    monkeypatch.setattr(agent, "_retriever", None)
    monkeypatch.setattr(agent, "_get_retriever", lambda: retriever)

    def configure(tool_decision: str, risk_response) -> SimpleNamespace:
        triage = FakeLLM(tool_decision)
        risk = FakeLLM(risk_response if isinstance(risk_response, str) else json.dumps(risk_response))
        monkeypatch.setattr(agent, "_triage_llm", triage)
        monkeypatch.setattr(agent, "_risk_llm", risk)
        return SimpleNamespace(triage=triage, risk=risk, retriever=retriever)

    return configure


def _initial_state(thread_id: str = "thread-test", trace_id: str = "trace-test") -> dict:
    return {
        "alert_id": "alert-1",
        "thread_id": thread_id,
        "trace_id": trace_id,
        "headline": HEADLINE,
        "candidate_suppliers": [],
        "context": "",
        "tool_decision": None,
        "alert": None,
        "is_valid": None,
        "trace_steps": [],
        "retriever_metadata": None,
    }


def _nodes(state: dict) -> list[str]:
    return [s["node_name"] for s in state["trace_steps"]]


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


# ---- Routing through the real graph (no checkpointer) ----

def test_skip_path(fakes):
    f = fakes("skip", OK_ALERT)
    result = agent.build_app().invoke(_initial_state())

    assert _nodes(result) == ["infer", "decide", "analyze", "validate"]
    assert result["candidate_suppliers"] == ["TSMC"]
    assert result["tool_decision"] == "skip"
    assert result["is_valid"] is True
    assert result["alert"]["risk_type"] == "earthquake"
    assert result["alert"]["risk_level"] == "High"
    assert result["retriever_metadata"] is None
    assert f.retriever.calls == 0
    assert len(f.triage.prompts) == 1
    assert len(f.risk.prompts) == 1


def test_retrieve_path(fakes):
    f = fakes("retrieve", OK_ALERT)
    result = agent.build_app().invoke(_initial_state())

    assert _nodes(result) == ["infer", "decide", "retrieve", "analyze", "validate"]
    assert result["context"] == RETRIEVED_CONTEXT
    assert RETRIEVED_CONTEXT in f.risk.prompts[0]
    assert result["retriever_metadata"] == {
        "retriever_id": "fake_retriever",
        "retriever_version": "v0",
        "embedding_provider": "fake",
        "top_k": 2,
    }
    assert f.retriever.calls == 1


def test_invalid_ok_alert_routes_to_fallback(fakes):
    fakes("skip", {**OK_ALERT, "impact": ""})
    result = agent.build_app().invoke(_initial_state())

    assert _nodes(result) == ["infer", "decide", "analyze", "validate", "fallback"]
    assert result["is_valid"] is False
    assert result["alert"]["status"] == "inconclusive"
    assert result["alert"]["impact"] == "Alert could not be validated reliably."


def test_unparseable_output_is_inconclusive_without_fallback(fakes):
    fakes("skip", "this is not json")
    result = agent.build_app().invoke(_initial_state())

    assert _nodes(result) == ["infer", "decide", "analyze", "validate"]
    assert result["is_valid"] is True
    assert result["alert"]["status"] == "inconclusive"
    assert result["alert"]["supplier"] is None


def test_default_app_has_no_checkpointer():
    assert agent.app.checkpointer is None
    assert agent.build_app().checkpointer is None
    with pytest.raises(ValueError):
        agent.build_app().get_state(_config("thread-x"))


# ---- InMemorySaver checkpoint / resume ----

def test_interrupt_before_analyze_then_resume(fakes, monkeypatch):
    f = fakes("retrieve", OK_ALERT)
    saver = InMemorySaver()
    cfg = _config("thread-A")

    # Invocation 1: runs up to analyze, then pauses.
    partial = agent.build_app(saver).invoke(
        _initial_state(thread_id="thread-A", trace_id="trace-1"),
        cfg,
        interrupt_before=["analyze"],
    )
    assert _nodes(partial) == ["infer", "decide", "retrieve"]

    snapshot = agent.build_app(saver).get_state(cfg)
    assert snapshot.next == ("analyze",)
    assert snapshot.values["alert"] is None
    assert snapshot.values["tool_decision"] == "retrieve"
    assert snapshot.values["context"] == RETRIEVED_CONTEXT
    assert snapshot.values["retriever_metadata"]["retriever_id"] == "fake_retriever"
    assert (len(f.triage.prompts), f.retriever.calls, len(f.risk.prompts)) == (1, 1, 0)

    # Invocation 2: fresh compiled graph, no in-process retriever; only the
    # thread_id (and the saver standing in for durable storage) carries over.
    monkeypatch.setattr(agent, "_retriever", None)
    resumed_app = agent.build_app(saver)
    resumed_app.update_state(cfg, {"trace_id": "trace-2"})
    assert resumed_app.get_state(cfg).next == ("analyze",)

    final = resumed_app.invoke(None, cfg)

    assert _nodes(final) == ["infer", "decide", "retrieve", "analyze", "validate"]
    assert (len(f.triage.prompts), f.retriever.calls, len(f.risk.prompts)) == (1, 1, 1)
    assert final["is_valid"] is True
    assert final["alert"]["supplier"] == "TSMC"
    assert final["retriever_metadata"]["retriever_id"] == "fake_retriever"
    assert resumed_app.get_state(cfg).next == ()

    # Workflow identity is stable; the execution attempt id changed.
    assert final["thread_id"] == "thread-A"
    assert final["trace_id"] == "trace-2"
    assert final["thread_id"] != final["trace_id"]


def test_resume_after_completion_is_noop(fakes):
    f = fakes("skip", OK_ALERT)
    saver = InMemorySaver()
    cfg = _config("thread-done")

    first = agent.build_app(saver).invoke(_initial_state(thread_id="thread-done"), cfg)
    again = agent.build_app(saver).invoke(None, cfg)

    assert again == first
    assert (len(f.triage.prompts), len(f.risk.prompts)) == (1, 1)


def test_threads_are_isolated(fakes):
    f = fakes("skip", OK_ALERT)
    saver = InMemorySaver()
    app = agent.build_app(saver)

    app.invoke(_initial_state(thread_id="thread-A", trace_id="trace-a"),
               _config("thread-A"), interrupt_before=["analyze"])
    result_b = app.invoke(_initial_state(thread_id="thread-B", trace_id="trace-b"),
                          _config("thread-B"))

    assert _nodes(result_b) == ["infer", "decide", "analyze", "validate"]
    assert app.get_state(_config("thread-A")).next == ("analyze",)
    assert app.get_state(_config("thread-B")).next == ()
    assert app.get_state(_config("thread-A")).values["trace_id"] == "trace-a"
    assert (len(f.triage.prompts), len(f.risk.prompts)) == (2, 1)
