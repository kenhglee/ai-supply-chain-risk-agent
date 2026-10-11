import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.retrieval.retriever import (
    TITAN_EMBED_MODEL_ID,
    FaissRetriever,
    RetrieverResult,
    build_embeddings,
    get_retriever,
)


# ---- FaissRetriever metadata attributes ----

def test_faiss_retriever_has_required_metadata_attrs():
    assert FaissRetriever.retriever_id == "faiss_supplier_profiles"
    assert FaissRetriever.retriever_version == "v1"


# ---- get_retriever: unsupported provider ----

def test_get_retriever_unknown_provider_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("RETRIEVER_PROVIDER", "pinecone")
    with pytest.raises(ValueError, match="Unsupported RETRIEVER_PROVIDER: 'pinecone'"):
        get_retriever(tmp_path / "profiles.json")


# ---- get_retriever: unsupported embedding provider ----

def _write_profiles(path: Path) -> None:
    profiles = [
        {"supplier": "TSMC", "profile": "TSMC is a semiconductor foundry in Taiwan."},
        {"supplier": "Murata", "profile": "Murata makes passive electronic components."},
    ]
    path.write_text(json.dumps(profiles), encoding="utf-8")


def test_get_retriever_unknown_embedding_provider_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("RETRIEVER_PROVIDER", "faiss")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "unsupported_embedder")
    profiles_path = tmp_path / "profiles.json"
    _write_profiles(profiles_path)
    with pytest.raises(ValueError, match="Unsupported EMBEDDING_PROVIDER: 'unsupported_embedder'"):
        get_retriever(profiles_path)


# ---- RetrieverResult ----

def test_retriever_result_holds_context():
    r = RetrieverResult(context="some context text")
    assert r.context == "some context text"


# ---- skip-path lazy init: structural guard ----

def test_process_alert_row_does_not_call_get_retriever():
    """
    process_alert_row must take retriever metadata from the graph result
    (RiskState.retriever_metadata) rather than calling _get_retriever().
    Calling the accessor would force retriever initialization on the
    'decide → skip → analyze' path, where retrieve() is never executed.

    This is a structural test: it inspects the source of process_alert_row
    without importing supplier_risk_agent (which has module-level side effects
    requiring credentials).
    """
    agent_src = Path(__file__).parent.parent / "app" / "workflows" / "supplier_risk_agent.py"
    src = agent_src.read_text(encoding="utf-8")

    start = src.index("def process_alert_row(")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]

    assert "_get_retriever()" not in body, (
        "process_alert_row must not call _get_retriever() — "
        "use retriever_metadata from graph state so the skip path never forces retriever initialization"
    )


# ---- build_embeddings: provider selection (offline) ----

@pytest.fixture
def no_network(monkeypatch):
    """Fail the test if anything tries to open a socket connection."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class _RecordingEmbeddings:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_build_embeddings_openai(monkeypatch, no_network):
    import langchain_openai
    monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", _RecordingEmbeddings)

    embeddings = build_embeddings("openai")

    assert isinstance(embeddings, _RecordingEmbeddings)
    assert embeddings.kwargs == {}


def test_build_embeddings_bedrock_uses_titan_v2(monkeypatch, no_network):
    import langchain_aws
    monkeypatch.setattr(langchain_aws, "BedrockEmbeddings", _RecordingEmbeddings)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")

    embeddings = build_embeddings("bedrock")

    assert isinstance(embeddings, _RecordingEmbeddings)
    # No credential arguments: the default AWS credential chain is used.
    assert embeddings.kwargs == {"model_id": "amazon.titan-embed-text-v2:0", "region_name": "us-west-2"}
    assert TITAN_EMBED_MODEL_ID == "amazon.titan-embed-text-v2:0"


def test_build_embeddings_bedrock_real_class_constructs_offline(monkeypatch, no_network):
    from langchain_aws import BedrockEmbeddings
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")

    embeddings = build_embeddings("bedrock")

    assert isinstance(embeddings, BedrockEmbeddings)
    assert embeddings.model_id == TITAN_EMBED_MODEL_ID
    assert embeddings.region_name == "us-west-2"


def test_build_embeddings_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unsupported EMBEDDING_PROVIDER: 'cohere'.*openai, bedrock"):
        build_embeddings("cohere")


def test_faiss_retriever_with_bedrock_embeddings(tmp_path, monkeypatch, no_network):
    """End to end through get_retriever with a deterministic fake in place of Bedrock."""
    import langchain_aws
    from langchain_core.embeddings import Embeddings

    class FakeTitan(Embeddings):
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        @staticmethod
        def _vec(text: str) -> list[float]:
            t = text.lower()
            return [float(t.count(w)) for w in ("tsmc", "taiwan", "murata", "passive")] + [1.0]

        def embed_documents(self, texts):
            return [self._vec(t) for t in texts]

        def embed_query(self, text):
            return self._vec(text)

    monkeypatch.setattr(langchain_aws, "BedrockEmbeddings", FakeTitan)
    monkeypatch.setenv("RETRIEVER_PROVIDER", "faiss")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "BEDROCK")
    profiles_path = tmp_path / "profiles.json"
    _write_profiles(profiles_path)

    retriever = get_retriever(profiles_path)
    result = retriever.retrieve("TSMC Taiwan earthquake", ["TSMC"])

    assert retriever.embedding_provider == "bedrock"
    assert result.context == "TSMC is a semiconductor foundry in Taiwan."
