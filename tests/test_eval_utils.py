import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.evaluation.risk_classifier_eval import (
    DEFAULT_DATASET,
    compare_case,
    compute_metrics,
    load_dataset,
    run_case,
    _normalize_risk_type,
    _mock_classify,
)
from app.prompt_registry import get_prompt


# ---- helpers ----

def _write_dataset(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "cases.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _valid_row(eval_id: str = "test-001", status: str = "ok") -> dict:
    return {
        "eval_id": eval_id,
        "headline": "Test headline",
        "candidate_suppliers": ["TSMC"],
        "context": "TSMC profile",
        "expected": {
            "status": status,
            "supplier": "TSMC" if status == "ok" else None,
            "risk_type": "earthquake" if status == "ok" else None,
            "risk_level": "High" if status == "ok" else None,
            "must_include_terms": ["TSMC"] if status == "ok" else [],
        },
    }


def _actual_ok(supplier: str = "TSMC", risk_type: str = "earthquake", risk_level: str = "High") -> dict:
    return {
        "status": "ok",
        "supplier": supplier,
        "risk_type": risk_type,
        "risk_level": risk_level,
        "impact": f"Earthquake disrupts {supplier} production in Taiwan.",
        "recommended_action": "Monitor supply chain closely.",
        "relevant_supplier_context": f"{supplier} operates in Taiwan.",
    }


def _actual_inconclusive() -> dict:
    return {
        "status": "inconclusive",
        "supplier": None,
        "risk_type": None,
        "risk_level": None,
        "impact": "No clearly relevant supplier identified.",
        "recommended_action": "Monitor for additional signals.",
        "relevant_supplier_context": "",
    }


# ---- dataset loading ----

def test_load_real_dataset_succeeds():
    cases = load_dataset(DEFAULT_DATASET)
    assert len(cases) == 15
    for c in cases:
        assert "eval_id" in c
        assert "headline" in c
        assert "expected" in c


def test_load_dataset_custom_path(tmp_path):
    p = _write_dataset(tmp_path, [_valid_row("t-001"), _valid_row("t-002")])
    cases = load_dataset(p)
    assert len(cases) == 2
    assert cases[0]["eval_id"] == "t-001"


def test_load_dataset_skips_empty_lines(tmp_path):
    p = tmp_path / "cases.jsonl"
    rows = [json.dumps(_valid_row("t-001")), "", "   ", json.dumps(_valid_row("t-002"))]
    p.write_text("\n".join(rows), encoding="utf-8")
    cases = load_dataset(p)
    assert len(cases) == 2


def test_load_dataset_missing_file_raises():
    with pytest.raises(FileNotFoundError, match="not found"):
        load_dataset(Path("/nonexistent/path/cases.jsonl"))


def test_load_dataset_malformed_json_raises(tmp_path):
    p = tmp_path / "cases.jsonl"
    # First line is valid; second line is not parseable JSON.
    p.write_text(json.dumps(_valid_row("t-001")) + "\nnot valid json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Line 2.*invalid JSON"):
        load_dataset(p)


def test_load_dataset_missing_case_fields_raises(tmp_path):
    bad = {"eval_id": "t-001", "headline": "Test"}  # missing candidate_suppliers, context, expected
    p = _write_dataset(tmp_path, [bad])
    with pytest.raises(ValueError, match="missing required fields"):
        load_dataset(p)


def test_load_dataset_missing_expected_fields_raises(tmp_path):
    bad = {
        "eval_id": "t-001", "headline": "Test",
        "candidate_suppliers": [], "context": "",
        "expected": {"status": "ok"},  # missing supplier, risk_type, risk_level, must_include_terms
    }
    p = _write_dataset(tmp_path, [bad])
    with pytest.raises(ValueError, match="expected block missing fields"):
        load_dataset(p)


# ---- normalisation ----

def test_normalize_aliases():
    assert _normalize_risk_type("flooding") == "flood"
    assert _normalize_risk_type("labor_strike") == "strike"
    assert _normalize_risk_type("power_outage") == "outage"
    assert _normalize_risk_type("export_control") == "export_controls"
    assert _normalize_risk_type("geopolitical_tensions") == "geopolitical_tension"
    assert _normalize_risk_type("cyber") == "cyber_attack"


def test_normalize_none_returns_none():
    assert _normalize_risk_type(None) is None
    assert _normalize_risk_type("") is None


def test_normalize_unknown_passthrough():
    assert _normalize_risk_type("some_novel_risk") == "some_novel_risk"


# ---- compare_case ----

def test_compare_all_fields_pass():
    case = _valid_row()
    actual = _actual_ok()
    result = compare_case(case, actual)
    assert result["passed"] is True
    assert result["failures"] == []
    assert all(result["field_results"].values())


def test_compare_status_mismatch_fails():
    case = _valid_row(status="ok")
    actual = _actual_inconclusive()
    result = compare_case(case, actual)
    assert result["passed"] is False
    assert result["field_results"]["status"] is False
    assert any("status" in f for f in result["failures"])


def test_compare_supplier_mismatch_fails():
    case = _valid_row()
    actual = _actual_ok(supplier="Murata")
    result = compare_case(case, actual)
    assert result["passed"] is False
    assert result["field_results"]["supplier"] is False


def test_compare_supplier_case_insensitive():
    case = _valid_row()
    actual = _actual_ok(supplier="tsmc")
    result = compare_case(case, actual)
    assert result["field_results"]["supplier"] is True


def test_compare_risk_type_normalised():
    case = _valid_row()  # expected risk_type = "earthquake"
    actual = _actual_ok(risk_type="Earthquake")
    result = compare_case(case, actual)
    assert result["field_results"]["risk_type"] is True


def test_compare_risk_type_alias_normalised():
    row = _valid_row()
    row["expected"]["risk_type"] = "flooding"
    actual = _actual_ok(risk_type="flood")
    result = compare_case(row, actual)
    assert result["field_results"]["risk_type"] is True


def test_compare_risk_level_mismatch_fails():
    case = _valid_row()
    actual = _actual_ok(risk_level="Low")
    result = compare_case(case, actual)
    assert result["passed"] is False
    assert result["field_results"]["risk_level"] is False


def test_compare_must_include_terms_found_in_impact():
    case = _valid_row()  # must_include_terms = ["TSMC"]
    actual = _actual_ok()  # impact contains "TSMC"
    result = compare_case(case, actual)
    assert result["field_results"]["must_include_terms"] is True


def test_compare_must_include_terms_missing_fails():
    case = _valid_row()
    case["expected"]["must_include_terms"] = ["TSMC", "uranium"]
    actual = _actual_ok()
    result = compare_case(case, actual)
    assert result["field_results"]["must_include_terms"] is False
    assert any("uranium" in str(f) for f in result["failures"])


def test_compare_empty_must_include_terms_always_passes():
    case = _valid_row(status="inconclusive")
    actual = _actual_inconclusive()
    result = compare_case(case, actual)
    assert result["field_results"]["must_include_terms"] is True


def test_compare_inconclusive_expected_and_actual():
    case = _valid_row(status="inconclusive")
    actual = _actual_inconclusive()
    result = compare_case(case, actual)
    assert result["passed"] is True


# ---- compute_metrics ----

def _make_result(passed: bool, **field_overrides) -> dict:
    fields = {"status": passed, "supplier": passed, "risk_type": passed,
              "risk_level": passed, "must_include_terms": passed}
    fields.update(field_overrides)
    return {"passed": passed, "field_results": fields}


def test_compute_metrics_all_pass():
    results = [_make_result(True)] * 10
    m = compute_metrics(results)
    assert m["total"] == 10
    assert m["passed"] == 10
    assert m["failed"] == 0
    assert m["pass_rate"] == 1.0
    assert m["status_accuracy"] == 1.0


def test_compute_metrics_mixed():
    results = [_make_result(True)] * 7 + [_make_result(False, status=False)] * 3
    m = compute_metrics(results)
    assert m["total"] == 10
    assert m["passed"] == 7
    assert m["failed"] == 3
    assert m["pass_rate"] == 0.7
    assert m["status_accuracy"] == 0.7


def test_compute_metrics_empty():
    m = compute_metrics([])
    assert m["total"] == 0
    assert m["pass_rate"] == 0.0
    assert m["status_accuracy"] == 0.0


# ---- run_case (mock mode) ----

def test_run_case_mock_with_suppliers():
    prompt = get_prompt("risk_classifier")
    case = _valid_row()
    actual = run_case(case, prompt, mock=True)
    assert actual["status"] == "ok"
    assert actual["supplier"] == "TSMC"


def test_run_case_mock_without_suppliers():
    prompt = get_prompt("risk_classifier")
    case = {
        "eval_id": "t-999", "headline": "Generic news",
        "candidate_suppliers": [], "context": "",
        "expected": {"status": "inconclusive", "supplier": None,
                     "risk_type": None, "risk_level": None, "must_include_terms": []},
    }
    actual = run_case(case, prompt, mock=True)
    assert actual["status"] == "inconclusive"
    assert actual["supplier"] is None


def test_run_case_mock_uses_first_supplier():
    prompt = get_prompt("risk_classifier")
    case = {
        "eval_id": "t-998", "headline": "Murata and TSMC news",
        "candidate_suppliers": ["Murata", "TSMC"], "context": "Murata profile info",
        "expected": {"status": "ok", "supplier": "Murata",
                     "risk_type": "outage", "risk_level": "Medium", "must_include_terms": []},
    }
    actual = run_case(case, prompt, mock=True)
    # Mock always picks the first supplier in the list
    assert actual["supplier"] == "Murata"
