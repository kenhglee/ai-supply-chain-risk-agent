import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.model_registry import get_model, resolve_model_runtime


# ---- helpers ----

def _write_model(directory: Path, version: str, status: str, **overrides) -> None:
    entry = {
        "model_id": directory.name,
        "version": version,
        "status": status,
        "owner": "Test Owner",
        "created_at": "2026-06-18",
        "description": f"{status} model config",
        "provider": "openai",
        "model_name": "gpt-4o-mini",
        "use_case": "test",
        **overrides,
    }
    (directory / f"{version}.json").write_text(json.dumps(entry))


# ---- load approved model ----

def test_load_approved_model_risk_analysis():
    record = get_model("risk_analysis_primary")
    assert record.model_id == "risk_analysis_primary"
    assert record.status == "approved"
    assert record.provider
    assert record.model_name
    assert record.use_case == "risk_classification"


def test_load_approved_model_triage():
    record = get_model("triage_primary")
    assert record.model_id == "triage_primary"
    assert record.status == "approved"
    assert record.use_case == "triage"


def test_model_record_has_all_metadata_fields():
    record = get_model("risk_analysis_primary")
    assert record.version
    assert record.owner
    assert record.created_at
    assert record.description
    assert record.provider
    assert record.model_name


# ---- load specific approved version ----

def test_load_specific_approved_version():
    record = get_model("risk_analysis_primary", version="v1")
    assert record.version == "v1"
    assert record.model_id == "risk_analysis_primary"


def test_load_triage_specific_version():
    record = get_model("triage_primary", version="v1")
    assert record.version == "v1"
    assert record.model_id == "triage_primary"


# ---- reject draft by default ----

def test_reject_draft_model_by_default(tmp_path):
    mid = "draft_model"
    d = tmp_path / mid
    d.mkdir()
    _write_model(d, "v1", "draft")
    with pytest.raises(ValueError, match="has status 'draft', not 'approved'"):
        get_model(mid, version="v1", models_dir=tmp_path)


def test_reject_pending_model_by_default(tmp_path):
    mid = "pending_model"
    d = tmp_path / mid
    d.mkdir()
    _write_model(d, "v1", "pending")
    with pytest.raises(ValueError, match="has status 'pending', not 'approved'"):
        get_model(mid, version="v1", models_dir=tmp_path)


# ---- allow draft when require_approved=False ----

def test_allow_draft_when_require_approved_false(tmp_path):
    mid = "draft_model"
    d = tmp_path / mid
    d.mkdir()
    _write_model(d, "v1", "draft", model_name="gpt-4o")
    record = get_model(mid, version="v1", require_approved=False, models_dir=tmp_path)
    assert record.status == "draft"
    assert record.model_name == "gpt-4o"


def test_allow_deprecated_when_require_approved_false(tmp_path):
    mid = "old_model"
    d = tmp_path / mid
    d.mkdir()
    _write_model(d, "v1", "deprecated")
    record = get_model(mid, version="v1", require_approved=False, models_dir=tmp_path)
    assert record.status == "deprecated"


# ---- latest approved selection ignores newer drafts ----

def test_latest_approved_ignores_newer_draft(tmp_path):
    mid = "versioned_model"
    d = tmp_path / mid
    d.mkdir()
    _write_model(d, "v1", "approved")
    _write_model(d, "v2", "draft")
    record = get_model(mid, models_dir=tmp_path)
    assert record.version == "v1"
    assert record.status == "approved"


def test_latest_approved_picks_highest_approved_version(tmp_path):
    mid = "versioned_model"
    d = tmp_path / mid
    d.mkdir()
    _write_model(d, "v1", "approved")
    _write_model(d, "v2", "approved")
    _write_model(d, "v3", "draft")
    record = get_model(mid, models_dir=tmp_path)
    assert record.version == "v2"
    assert record.status == "approved"


def test_no_approved_version_raises(tmp_path):
    mid = "all_draft"
    d = tmp_path / mid
    d.mkdir()
    _write_model(d, "v1", "draft")
    _write_model(d, "v2", "draft")
    with pytest.raises(ValueError, match="No approved version found"):
        get_model(mid, models_dir=tmp_path)


# ---- unknown model_id error ----

def test_unknown_model_id_raises():
    with pytest.raises(ValueError, match="Unknown model_id"):
        get_model("nonexistent_model")


def test_unknown_model_id_raises_custom_dir(tmp_path):
    with pytest.raises(ValueError, match="Unknown model_id"):
        get_model("ghost", models_dir=tmp_path)


# ---- unknown version error ----

def test_unknown_version_raises():
    with pytest.raises(ValueError, match="Version 'v99' not found"):
        get_model("risk_analysis_primary", version="v99")


def test_unknown_version_raises_custom_dir(tmp_path):
    mid = "real_model"
    d = tmp_path / mid
    d.mkdir()
    _write_model(d, "v1", "approved")
    with pytest.raises(ValueError, match="Version 'v5' not found"):
        get_model(mid, version="v5", models_dir=tmp_path)


# ---- missing required fields ----

def test_missing_required_field_raises(tmp_path):
    mid = "incomplete_model"
    d = tmp_path / mid
    d.mkdir()
    # omit "model_name"
    entry = {
        "model_id": mid, "version": "v1", "status": "approved",
        "owner": "Test", "created_at": "2026-06-18",
        "description": "Missing field", "provider": "openai", "use_case": "test",
    }
    (d / "v1.json").write_text(json.dumps(entry))
    with pytest.raises(ValueError, match="missing required fields"):
        get_model(mid, version="v1", models_dir=tmp_path)


# ---- resolve_model_runtime ----

def test_resolve_no_override(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    record = get_model("risk_analysis_primary")
    rt = resolve_model_runtime(record)
    assert rt.runtime_provider == record.provider.lower()
    assert rt.runtime_model_name == record.model_name
    assert rt.runtime_overridden is False
    # registry fields are preserved
    assert rt.model_id == record.model_id
    assert rt.model_provider == record.provider
    assert rt.model_name == record.model_name


def test_resolve_llm_provider_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    record = get_model("risk_analysis_primary")   # registered as openai
    rt = resolve_model_runtime(record)
    assert rt.runtime_provider == "bedrock"
    assert rt.runtime_overridden is True
    assert rt.model_provider == "openai"          # registry value unchanged


def test_resolve_openai_model_override(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    record = get_model("risk_analysis_primary")   # registered as gpt-4o-mini
    rt = resolve_model_runtime(record)
    assert rt.runtime_model_name == "gpt-4o"
    assert rt.runtime_overridden is True
    assert rt.model_name == record.model_name     # registry value unchanged


def test_resolve_bedrock_model_id_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    bedrock_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    monkeypatch.setenv("BEDROCK_MODEL_ID", bedrock_id)
    record = get_model("risk_analysis_primary")
    rt = resolve_model_runtime(record)
    assert rt.runtime_provider == "bedrock"
    assert rt.runtime_model_name == bedrock_id
    assert rt.runtime_overridden is True


def test_resolve_bifrost_provider_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bifrost")
    monkeypatch.delenv("BIFROST_MODEL_ID", raising=False)
    record = get_model("risk_analysis_primary")   # registered as openai
    rt = resolve_model_runtime(record)
    assert rt.runtime_provider == "bifrost"
    assert rt.runtime_overridden is True
    assert rt.model_provider == "openai"          # registry value unchanged


def test_resolve_bifrost_model_id_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bifrost")
    bifrost_id = "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
    monkeypatch.setenv("BIFROST_MODEL_ID", bifrost_id)
    record = get_model("risk_analysis_primary")
    rt = resolve_model_runtime(record)
    assert rt.runtime_provider == "bifrost"
    assert rt.runtime_model_name == bifrost_id
    assert rt.runtime_overridden is True
