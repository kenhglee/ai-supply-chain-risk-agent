import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.prompt_registry import get_prompt


def test_load_risk_classifier_prompt():
    record = get_prompt("risk_classifier")
    assert record.prompt_id == "risk_classifier"
    assert record.version == "v1"
    assert record.status == "approved"
    assert record.template
    assert "{headline}" in record.template
    assert "{candidate_suppliers}" in record.template
    assert "{context}" in record.template


def test_load_triage_agent_prompt():
    record = get_prompt("triage_agent")
    assert record.prompt_id == "triage_agent"
    assert record.version == "v1"
    assert record.status == "approved"
    assert "{headline}" in record.template
    assert "{suppliers}" in record.template


def test_load_prompt_by_explicit_version():
    record = get_prompt("risk_classifier", version="v1")
    assert record.version == "v1"
    assert record.prompt_id == "risk_classifier"


def test_prompt_record_has_all_metadata_fields():
    record = get_prompt("risk_classifier")
    assert record.owner
    assert record.created_at
    assert record.description


def test_missing_prompt_id_raises():
    with pytest.raises(ValueError, match="Unknown prompt_id"):
        get_prompt("nonexistent_prompt")


def test_missing_version_raises():
    with pytest.raises(ValueError, match="Version 'v99' not found"):
        get_prompt("risk_classifier", version="v99")


def test_unapproved_not_selected_by_default(tmp_path):
    pid = "test_draft"
    (tmp_path / pid).mkdir()
    draft = {
        "prompt_id": pid,
        "version": "v1",
        "status": "draft",
        "owner": "Test Owner",
        "created_at": "2026-06-18",
        "description": "A draft prompt not yet approved.",
        "template": "draft template {var}",
    }
    (tmp_path / pid / "v1.json").write_text(json.dumps(draft))
    with pytest.raises(ValueError, match="No approved version"):
        get_prompt(pid, prompts_dir=tmp_path)


def test_approved_version_selected_when_mixed(tmp_path):
    pid = "test_mixed"
    (tmp_path / pid).mkdir()
    draft = {
        "prompt_id": pid, "version": "v1", "status": "draft",
        "owner": "Test", "created_at": "2026-06-18",
        "description": "Draft", "template": "draft {x}",
    }
    approved = {
        "prompt_id": pid, "version": "v2", "status": "approved",
        "owner": "Test", "created_at": "2026-06-18",
        "description": "Approved", "template": "approved {x}",
    }
    (tmp_path / pid / "v1.json").write_text(json.dumps(draft))
    (tmp_path / pid / "v2.json").write_text(json.dumps(approved))
    record = get_prompt(pid, prompts_dir=tmp_path)
    assert record.version == "v2"
    assert record.status == "approved"


def test_risk_classifier_template_renders():
    record = get_prompt("risk_classifier")
    rendered = record.template.format(
        headline="Earthquake disrupts TSMC fab in Taiwan",
        candidate_suppliers="TSMC",
        context="TSMC operates major fabs in Hsinchu, Taiwan.",
    )
    assert "TSMC" in rendered
    assert "Earthquake" in rendered
    assert "{" in rendered


def _draft_prompt_dir(tmp_path: Path, pid: str, version: str = "v1") -> Path:
    """Write a single draft prompt file and return the prompts root."""
    (tmp_path / pid).mkdir()
    (tmp_path / pid / f"{version}.json").write_text(json.dumps({
        "prompt_id": pid,
        "version": version,
        "status": "draft",
        "owner": "Test Owner",
        "created_at": "2026-06-18",
        "description": "Draft prompt under development.",
        "template": "draft template {var}",
    }))
    return tmp_path


# ---- require_approved tests ----

def test_explicit_approved_version_loads_successfully():
    record = get_prompt("risk_classifier", version="v1")
    assert record.status == "approved"
    assert record.version == "v1"


def test_explicit_non_approved_version_fails_by_default(tmp_path):
    prompts_dir = _draft_prompt_dir(tmp_path, "qa_draft")
    with pytest.raises(ValueError, match="has status 'draft', not 'approved'"):
        get_prompt("qa_draft", version="v1", prompts_dir=prompts_dir)


def test_explicit_non_approved_version_loads_when_require_approved_false(tmp_path):
    prompts_dir = _draft_prompt_dir(tmp_path, "qa_draft")
    record = get_prompt("qa_draft", version="v1", require_approved=False, prompts_dir=prompts_dir)
    assert record.status == "draft"
    assert record.template == "draft template {var}"


def test_omitted_version_selects_latest_approved_ignores_draft(tmp_path):
    pid = "qa_mixed"
    (tmp_path / pid).mkdir()
    for ver, status in [("v1", "approved"), ("v2", "draft"), ("v3", "approved")]:
        (tmp_path / pid / f"{ver}.json").write_text(json.dumps({
            "prompt_id": pid, "version": ver, "status": status,
            "owner": "Test", "created_at": "2026-06-18",
            "description": f"{status} prompt", "template": f"{ver} {{x}}",
        }))
    record = get_prompt(pid, prompts_dir=tmp_path)
    assert record.version == "v3"
    assert record.status == "approved"


def test_github_risk_evaluation_works():
    from app.workflows.github_risk_evaluator import evaluate_github_event_risk

    event = {
        "event_type": "pull_request",
        "pull_request": {"base_ref": "main"},
    }
    result = evaluate_github_event_risk(event)
    assert result["decision"] == "review_recommended"
    assert result["risk_score"] == 60
    assert "reason" in result


def test_github_risk_push_to_main():
    from app.workflows.github_risk_evaluator import evaluate_github_event_risk

    event = {
        "event_type": "push",
        "push": {"ref": "refs/heads/main"},
    }
    result = evaluate_github_event_risk(event)
    assert result["decision"] == "manual_review_required"
    assert result["risk_score"] == 80
