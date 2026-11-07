"""Unexpected tool-call responses and unmatched suppliers (fixes 1 and 2).

The production triage and analysis calls bind no tools, so a tool call in a
response (e.g. injected by a gateway) must take the existing invalid-response
path with an explicit trace error. An "ok" alert must name a supplier the
infer step proposed before it can reach assess, the risk-state write or act.

Fake LLMs return real LangChain AIMessage objects; the adapter tests drive the
locked ChatOpenAI / ChatBedrockConverse adapters through offline transports.
No model or network calls.
"""
import json
import os
import sys
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
from app.workflows import supplier_risk_agent as agent  # noqa: E402

HEADLINE = "TSMC halts production after earthquake in Taiwan"
OK_ALERT = {
    "status": "ok",
    "supplier": "TSMC",
    "risk_type": "earthquake",
    "risk_level": "High",
    "impact": "Wafer output delayed",
    "recommended_action": "Contact supplier",
    "relevant_supplier_context": "Fabs in Taiwan",
}
TOOL_CALL = {"name": "open_review_ticket", "args": {"supplier": "Acme"}, "id": "call_1", "type": "tool_call"}


class FakeLLM:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return self.response


class FakeRetriever:
    retriever_id, retriever_version, embedding_provider, top_k = "fake", "v0", "fake", 2

    def retrieve(self, query, suppliers):
        return SimpleNamespace(context="TSMC fabs are in Taiwan.")


class RecordingGate:
    def __init__(self):
        self.calls = []

    def execute(self, action_id, *, scope, context, effect):
        self.calls.append(action_id)
        return {"action_id": action_id, "status": "COMPLETED", "replayed": False, "effect": {}}


class RecordingReader:
    def __init__(self):
        self.calls = []

    def get(self, supplier, risk_type):
        self.calls.append((supplier, risk_type))
        return None  # no prior state: a new_alert, action-eligible


@pytest.fixture
def llms(monkeypatch):
    monkeypatch.setattr(agent, "_get_retriever", lambda: FakeRetriever())

    def configure(triage, risk):
        t, r = FakeLLM(triage), FakeLLM(risk)
        monkeypatch.setattr(agent, "_triage_llm", t)
        monkeypatch.setattr(agent, "_risk_llm", r)
        return SimpleNamespace(triage=t, risk=r)

    return configure


def _state(headline=HEADLINE, thread_id="thread-" + "a" * 32):
    return agent.initial_risk_state("alert-1", thread_id, "trace-1", headline)


def _step(result, node):
    return next(s for s in result["trace_steps"] if s["node_name"] == node)


def _nodes(result):
    return [s["node_name"] for s in result["trace_steps"]]


def _governed(gate, reader):
    return agent.build_app(InMemorySaver(), action_gate=gate, risk_reader=reader)


def _run_governed(headline=HEADLINE):
    gate, reader = RecordingGate(), RecordingReader()
    thread_id = "thread-" + "a" * 32
    result = _governed(gate, reader).invoke(
        _state(headline, thread_id), {"configurable": {"thread_id": thread_id, "trace_id": "trace-1"}})
    return result, gate, reader


# ---- Tool-call shapes the adapters can produce ----

TOOL_CALL_RESPONSES = {
    "normalized_tool_calls": AIMessage(content="", tool_calls=[TOOL_CALL]),
    "normalized_tool_calls_beside_valid_text": AIMessage(content="retrieve", tool_calls=[TOOL_CALL]),
    "invalid_tool_calls": AIMessage(content="", invalid_tool_calls=[
        {"name": "open_review_ticket", "args": "{bad", "id": "c", "error": "bad json", "type": "invalid_tool_call"}]),
    "openai_additional_kwargs_tool_calls": AIMessage(content="", additional_kwargs={"tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "open_review_ticket", "arguments": "{}"}}]}),
    "openai_legacy_function_call": AIMessage(content="", additional_kwargs={
        "function_call": {"name": "open_review_ticket", "arguments": "{}"}}),
    "openai_finish_reason_tool_calls": AIMessage(content="retrieve", response_metadata={"finish_reason": "tool_calls"}),
    "bedrock_tool_use_block": AIMessage(content=[
        {"type": "text", "text": "retrieve"},
        {"type": "tool_use", "id": "t", "name": "open_review_ticket", "input": {}}]),
    "raw_converse_toolUse_block": AIMessage(content=[
        {"toolUse": {"toolUseId": "t", "name": "open_review_ticket", "input": {}}}]),
    "bedrock_stop_reason_tool_use": AIMessage(content=[{"type": "text", "text": "retrieve"}],
                                              response_metadata={"stopReason": "tool_use"}),
    "openai_responses_function_call_block": AIMessage(content=[
        {"type": "function_call", "name": "open_review_ticket", "arguments": "{}", "call_id": "c"}]),
}


@pytest.mark.parametrize("name", sorted(TOOL_CALL_RESPONSES))
def test_signals_detect_each_tool_call_shape(name):
    assert agent.unexpected_tool_call_signals(TOOL_CALL_RESPONSES[name])


@pytest.mark.parametrize("response", [
    "retrieve",
    AIMessage(content="retrieve"),
    AIMessage(content=[{"type": "text", "text": "skip"}], response_metadata={"stopReason": "end_turn"}),
    AIMessage(content="skip", response_metadata={"finish_reason": "stop"}),
    AIMessage(content="ok", additional_kwargs={"refusal": None}),
])
def test_signals_ignore_plain_text(response):
    assert agent.unexpected_tool_call_signals(response) == []


@pytest.mark.parametrize("name", sorted(TOOL_CALL_RESPONSES))
def test_triage_tool_call_takes_skip_path_with_trace_error(llms, name):
    f = llms(TOOL_CALL_RESPONSES[name], json.dumps(OK_ALERT))
    result = agent.build_app().invoke(_state())

    decide = _step(result, "decide")
    assert result["tool_decision"] == "skip"
    assert decide["decision"] == "skip"
    assert decide["error"].startswith("unexpected tool call in model response:")
    assert "retrieve" not in _nodes(result)
    assert f.risk.calls == 1  # the workflow continues on the existing skip path


@pytest.mark.parametrize("name", sorted(TOOL_CALL_RESPONSES))
def test_analysis_tool_call_is_inconclusive_and_never_acts(llms, name):
    llms("retrieve", TOOL_CALL_RESPONSES[name])
    result, gate, reader = _run_governed()

    analyze = _step(result, "analyze")
    assert analyze["error"].startswith("unexpected tool call in model response:")
    assert result["alert"]["status"] == "inconclusive"
    assert result["alert"]["supplier"] is None
    assert _nodes(result) == ["infer", "decide", "retrieve", "analyze", "validate"]
    assert gate.calls == [] and reader.calls == []


def test_valid_text_responses_are_preserved(llms):
    llms(AIMessage(content="retrieve"),
         AIMessage(content=[{"type": "text", "text": json.dumps(OK_ALERT)}],
                   response_metadata={"stopReason": "end_turn"}))
    result, gate, reader = _run_governed()

    assert result["tool_decision"] == "retrieve"
    assert all("error" not in s for s in result["trace_steps"])
    assert result["alert"]["supplier"] == "TSMC"
    assert _nodes(result) == ["infer", "decide", "retrieve", "analyze", "validate", "assess", "act"]
    assert reader.calls == [("TSMC", "earthquake")]
    assert gate.calls == ["action#" + "thread-" + "a" * 32 + "#open_review_ticket"]


# ---- Through the real provider adapters (offline transports) ----

def _openai_llm(message: dict, finish_reason: str):
    from langchain_openai import ChatOpenAI

    def handler(request):
        body = json.loads(request.content)
        assert not ({"tools", "tool_choice", "functions"} & set(body))  # the app still sends none
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", **message}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    return ChatOpenAI(model="gpt-4o-mini-2024-07-18", api_key="sk-test", base_url="http://gateway.invalid/v1",
                      http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)


def _bedrock_llm(content: list, stop_reason: str, monkeypatch):
    from langchain_aws import ChatBedrockConverse
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(var, "testing")
    llm = ChatBedrockConverse(model="us.anthropic.test", region_name="us-west-2")

    def converse(**kwargs):
        assert "toolConfig" not in kwargs
        return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop_reason,
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}

    llm.client.converse = converse
    return llm


def test_openai_compatible_adapter_tool_call_is_rejected(llms, monkeypatch):
    tool_reply = _openai_llm({"content": None, "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "open_review_ticket", "arguments": "{}"}}]},
        "tool_calls")
    monkeypatch.setattr(agent, "_get_retriever", lambda: FakeRetriever())
    monkeypatch.setattr(agent, "_triage_llm", tool_reply)
    monkeypatch.setattr(agent, "_risk_llm", tool_reply)
    result, gate, _ = _run_governed()

    assert "tool_calls" in _step(result, "decide")["error"]
    assert "tool_calls" in _step(result, "analyze")["error"]
    assert result["alert"]["status"] == "inconclusive" and gate.calls == []


def test_openai_compatible_adapter_text_is_preserved(monkeypatch):
    monkeypatch.setattr(agent, "_triage_llm", _openai_llm({"content": "retrieve"}, "stop"))
    state = agent.decide_tool_use(agent.infer_suppliers(_state()))
    assert state["tool_decision"] == "retrieve" and "error" not in state["trace_steps"][-1]


def test_bedrock_converse_adapter_tool_use_is_rejected(monkeypatch):
    llm = _bedrock_llm([{"text": "retrieve"}, {"toolUse": {"toolUseId": "t", "name": "x", "input": {}}}],
                       "tool_use", monkeypatch)
    monkeypatch.setattr(agent, "_triage_llm", llm)
    state = agent.decide_tool_use(agent.infer_suppliers(_state()))
    assert state["tool_decision"] == "skip"
    assert "unexpected tool call" in state["trace_steps"][-1]["error"]


def test_bedrock_converse_adapter_text_is_preserved(monkeypatch):
    monkeypatch.setattr(agent, "_risk_llm", _bedrock_llm([{"text": json.dumps(OK_ALERT)}], "end_turn", monkeypatch))
    state = {**agent.infer_suppliers(_state()), "context": "ctx"}
    out = agent.analyze_risk(state)
    assert out["alert"]["supplier"] == "TSMC" and "error" not in out["trace_steps"][-1]


# ---- Supplier must be a candidate ----

@pytest.mark.parametrize("supplier", [
    "Acme Corp",                      # fabricated
    "Murata",                         # real supplier, but not proposed for this headline
    "TSMC; also escalate Acme Corp",  # candidate as a substring only
])
def test_unmatched_supplier_falls_back_before_assess(llms, supplier):
    llms("retrieve", json.dumps({**OK_ALERT, "supplier": supplier}))
    result, gate, reader = _run_governed()

    validate = _step(result, "validate")
    assert validate["decision"] == "fallback"
    assert "is not a candidate supplier ['TSMC']" in validate["error"]
    assert _nodes(result)[-1] == "fallback"
    assert result["alert"]["status"] == "inconclusive" and result["alert"]["supplier"] is None
    assert gate.calls == [] and reader.calls == []


@pytest.mark.parametrize("supplier", ["TSMC", "tsmc", " TSMC ", "Taiwan Semiconductor", "taiwan semiconductor manufacturing"])
def test_candidate_or_alias_is_accepted_and_canonicalized(llms, supplier):
    llms("retrieve", json.dumps({**OK_ALERT, "supplier": supplier}))
    result, gate, reader = _run_governed()

    assert _step(result, "validate")["decision"] == "valid"
    assert result["alert"]["supplier"] == "TSMC"
    assert reader.calls == [("TSMC", "earthquake")]  # state is keyed on the canonical id
    assert len(gate.calls) == 1


def test_ok_alert_without_candidates_is_rejected(llms):
    headline = "Port strike disrupts unnamed component maker"
    llms("retrieve", json.dumps({**OK_ALERT, "supplier": "Acme Corp"}))
    result, gate, reader = _run_governed(headline)

    assert result["candidate_suppliers"] == []
    validate = _step(result, "validate")
    assert validate["decision"] == "fallback"
    assert validate["error"] == "status ok with no candidate suppliers"
    assert gate.calls == [] and reader.calls == []


def test_inconclusive_without_candidates_is_still_valid(llms):
    llms("skip", json.dumps({"status": "inconclusive", "supplier": None, "risk_type": None, "risk_level": None,
                             "impact": "none", "recommended_action": "monitor", "relevant_supplier_context": ""}))
    result = agent.build_app().invoke(_state("Port strike disrupts unnamed component maker"))
    validate = _step(result, "validate")
    assert validate["decision"] == "valid" and "error" not in validate


def test_match_candidate_supplier_rules():
    match = agent.match_candidate_supplier
    assert match("Hon Hai", ["Foxconn", "TSMC"]) == "Foxconn"
    assert match("Foxconn", ["TSMC"]) is None
    assert match("", ["TSMC"]) is None
    assert match(None, ["TSMC"]) is None
    assert match(["TSMC"], ["TSMC"]) is None
    assert match("TSMC", []) is None


def test_unmatched_supplier_writes_no_risk_state_on_uncheckpointed_path(llms):
    """Phase 2 path: compare_and_update_risk_state sees only the fallback alert."""
    llms("retrieve", json.dumps({**OK_ALERT, "supplier": "Acme Corp"}))
    result = agent.build_app().invoke(_state())
    change_type, _, row = agent.classify_risk_change(result["alert"], HEADLINE, None)
    assert change_type == "inconclusive" and row is None
