import os
import sys
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.evaluation.risk_classifier_eval import _build_llm
from app.model_registry import get_model


def test_build_llm_bifrost_constructs_chat_openai_with_gateway_config(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bifrost")
    monkeypatch.setenv(
        "BIFROST_MODEL_ID", "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    monkeypatch.delenv("BIFROST_BASE_URL", raising=False)
    monkeypatch.delenv("BIFROST_API_KEY", raising=False)

    record = get_model("risk_analysis_primary")

    with patch("langchain_openai.ChatOpenAI") as mock_chat_openai:
        _build_llm(record)

    mock_chat_openai.assert_called_once_with(
        model="bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        temperature=0.2,
        base_url="http://localhost:8080/langchain",
        api_key="dummy-key",
    )


def test_build_llm_bifrost_respects_base_url_and_api_key_overrides(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bifrost")
    monkeypatch.setenv("BIFROST_BASE_URL", "http://bifrost.internal:9000/langchain")
    monkeypatch.setenv("BIFROST_API_KEY", "team-shared-key")

    record = get_model("risk_analysis_primary")

    with patch("langchain_openai.ChatOpenAI") as mock_chat_openai:
        _build_llm(record)

    _, kwargs = mock_chat_openai.call_args
    assert kwargs["base_url"] == "http://bifrost.internal:9000/langchain"
    assert kwargs["api_key"] == "team-shared-key"
