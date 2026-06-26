import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.retrieval.retriever import FaissRetriever, RetrieverResult, get_retriever


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
    process_alert_row must read the module global _retriever directly rather
    than calling _get_retriever(). Calling the accessor would force retriever
    initialization on the 'decide → skip → analyze' path, where retrieve()
    is never executed.

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
        "read _retriever directly so the skip path never forces retriever initialization"
    )
