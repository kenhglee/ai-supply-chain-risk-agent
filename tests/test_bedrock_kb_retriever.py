import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.retrieval.retriever import BedrockKBRetriever, RetrieverResult, get_retriever


def _make_retriever(kb_id: str = "kb-test", region: str = "us-east-1", top_k: int = 4) -> tuple[BedrockKBRetriever, MagicMock]:
    with patch("boto3.client") as mock_boto:
        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        retriever = BedrockKBRetriever(kb_id=kb_id, region=region, top_k=top_k)
        retriever._client = mock_client
    return retriever, mock_client


def _result(text: str) -> dict:
    return {"content": {"text": text}}


# ---- structural / metadata ----

def test_bedrock_kb_retriever_has_required_metadata_attrs():
    assert isinstance(BedrockKBRetriever.retriever_id, str)
    assert isinstance(BedrockKBRetriever.retriever_version, str)
    assert BedrockKBRetriever.retriever_id  # non-empty
    assert BedrockKBRetriever.retriever_version


def test_embedding_provider_is_bedrock_managed_sentinel():
    assert BedrockKBRetriever.embedding_provider == "bedrock_managed"


def test_top_k_is_set_from_constructor():
    retriever, _ = _make_retriever(top_k=8)
    assert retriever.top_k == 8


# ---- retrieve() API call shape ----

def test_retrieve_passes_query_and_top_k():
    retriever, mock_client = _make_retriever(kb_id="my-kb", top_k=4)
    mock_client.retrieve.return_value = {"retrievalResults": [_result("some text")]}

    retriever.retrieve("earthquake in Taiwan", [])

    call_kwargs = mock_client.retrieve.call_args[1]
    assert call_kwargs["knowledgeBaseId"] == "my-kb"
    assert call_kwargs["retrievalQuery"]["text"] == "earthquake in Taiwan"
    assert call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 4


def test_retrieve_applies_supplier_filter():
    retriever, mock_client = _make_retriever()
    mock_client.retrieve.return_value = {"retrievalResults": [_result("context")]}

    retriever.retrieve("port congestion", ["TSMC", "Murata"])

    vector_config = mock_client.retrieve.call_args[1]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert "filter" in vector_config
    assert vector_config["filter"] == {"in": {"key": "supplier", "value": ["TSMC", "Murata"]}}


def test_retrieve_no_filter_when_no_candidates():
    retriever, mock_client = _make_retriever()
    mock_client.retrieve.return_value = {"retrievalResults": [_result("context")]}

    retriever.retrieve("general risk", [])

    vector_config = mock_client.retrieve.call_args[1]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert "filter" not in vector_config


# ---- retrieve() return value ----

def test_retrieve_returns_context_from_results():
    retriever, mock_client = _make_retriever()
    mock_client.retrieve.return_value = {
        "retrievalResults": [_result("TSMC profile text"), _result("more TSMC text")]
    }

    result = retriever.retrieve("TSMC disruption", ["TSMC"])

    assert isinstance(result, RetrieverResult)
    assert "TSMC profile text" in result.context
    assert "more TSMC text" in result.context


def test_retrieve_returns_no_context_found_on_empty_results():
    retriever, mock_client = _make_retriever()
    mock_client.retrieve.return_value = {"retrievalResults": []}

    result = retriever.retrieve("unknown supplier", [])

    assert result.context == "No context found"


def test_retrieve_limits_to_two_results():
    retriever, mock_client = _make_retriever()
    mock_client.retrieve.return_value = {
        "retrievalResults": [
            _result("result 1"),
            _result("result 2"),
            _result("result 3"),
            _result("result 4"),
        ]
    }

    result = retriever.retrieve("query", [])

    assert "result 1" in result.context
    assert "result 2" in result.context
    assert "result 3" not in result.context
    assert "result 4" not in result.context


# ---- factory ----

def test_get_retriever_factory_returns_bedrock_kb(monkeypatch, tmp_path):
    monkeypatch.setenv("RETRIEVER_PROVIDER", "bedrock_kb")
    monkeypatch.setenv("BEDROCK_KB_ID", "kb-test-123")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")

    with patch("boto3.client"):
        retriever = get_retriever(tmp_path / "profiles.json")

    assert isinstance(retriever, BedrockKBRetriever)
    assert retriever._kb_id == "kb-test-123"


def test_get_retriever_factory_bedrock_kb_missing_id_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("RETRIEVER_PROVIDER", "bedrock_kb")
    monkeypatch.delenv("BEDROCK_KB_ID", raising=False)

    with pytest.raises(ValueError, match="BEDROCK_KB_ID must be set"):
        get_retriever(tmp_path / "profiles.json")


def test_get_retriever_factory_bedrock_kb_top_k_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RETRIEVER_PROVIDER", "bedrock_kb")
    monkeypatch.setenv("BEDROCK_KB_ID", "kb-abc")
    monkeypatch.setenv("BEDROCK_KB_TOP_K", "6")

    with patch("boto3.client"):
        retriever = get_retriever(tmp_path / "profiles.json")

    assert retriever.top_k == 6
