"""
Evaluation harness for the risk_classifier prompt.

Runs curated eval cases from evals/risk_classifier/golden_set_v1.jsonl against
the current prompt and model registry configuration, then reports per-case and
aggregate metrics.

Usage:
    # Live mode (requires OPENAI_API_KEY or Bedrock credentials):
    uv run python -m app.evaluation.risk_classifier_eval

    # Mock mode (deterministic, no credentials required):
    uv run python -m app.evaluation.risk_classifier_eval --mock

    # Custom dataset:
    uv run python -m app.evaluation.risk_classifier_eval --dataset path/to/cases.jsonl
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from app.prompt_registry import get_prompt, PromptRecord
from app.model_registry import get_model, ModelRecord, resolve_model_runtime

EVALS_DIR = Path(__file__).resolve().parent.parent.parent / "evals"
DEFAULT_DATASET = EVALS_DIR / "risk_classifier" / "golden_set_v1.jsonl"

_REQUIRED_CASE_FIELDS = {"eval_id", "headline", "candidate_suppliers", "context", "expected"}
_REQUIRED_EXPECTED_FIELDS = {"status", "supplier", "risk_type", "risk_level", "must_include_terms"}


# ---- Dataset ----

def load_dataset(path: Path = DEFAULT_DATASET) -> list[dict]:
    """Load and validate a JSONL evaluation dataset.

    Raises FileNotFoundError if the file is missing, ValueError if any row
    has invalid JSON or is missing required fields.
    """
    if not path.exists():
        raise FileNotFoundError(f"Evaluation dataset not found: {path}")

    cases = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            case = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Line {lineno}: invalid JSON — {exc}") from exc

        missing = _REQUIRED_CASE_FIELDS - case.keys()
        if missing:
            raise ValueError(
                f"Line {lineno} (eval_id={case.get('eval_id', '?')}): "
                f"missing required fields {sorted(missing)}"
            )
        missing_exp = _REQUIRED_EXPECTED_FIELDS - (case.get("expected") or {}).keys()
        if missing_exp:
            raise ValueError(
                f"Line {lineno} (eval_id={case.get('eval_id', '?')}): "
                f"expected block missing fields {sorted(missing_exp)}"
            )
        cases.append(case)

    return cases


# ---- Normalisation (mirrors production logic; kept local to avoid importing
#      supplier_risk_agent, which has module-level FAISS/DynamoDB/LLM init) ----

_RISK_TYPE_ALIASES: dict[str, str] = {
    "earthquake": "earthquake",
    "flood": "flood",
    "flooding": "flood",
    "strike": "strike",
    "labor_strike": "strike",
    "outage": "outage",
    "power_outage": "outage",
    "sanction": "sanctions",
    "sanctions": "sanctions",
    "export_control": "export_controls",
    "export_controls": "export_controls",
    "typhoon": "typhoon",
    "drought": "drought",
    "congestion": "congestion",
    "geopolitics": "geopolitical_tension",
    "geopolitical_tensions": "geopolitical_tension",
    "geopolitical_risk": "geopolitical_tension",
    "geopolitical_tension": "geopolitical_tension",
    "cyber": "cyber_attack",
    "cyber_attack": "cyber_attack",
    "supply_chain_attack": "cyber_attack",
}


def _normalize_risk_type(risk_type: str | None) -> str | None:
    if not risk_type:
        return None
    value = risk_type.strip().lower().replace(" ", "_")
    return _RISK_TYPE_ALIASES.get(value, value)


def _extract_text(response) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _parse_alert(raw_text: str) -> dict:
    text = raw_text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        return {
            "status": "inconclusive",
            "supplier": None,
            "risk_type": None,
            "risk_level": None,
            "impact": "Model output could not be parsed reliably.",
            "recommended_action": "Review the headline and prompt logic.",
            "relevant_supplier_context": "",
        }


# ---- LLM builder (avoids importing supplier_risk_agent) ----

def _build_llm(model_record: ModelRecord):
    runtime = resolve_model_runtime(model_record)

    if runtime.runtime_provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=runtime.runtime_model_name, temperature=0.2)

    if runtime.runtime_provider == "bedrock":
        from langchain_aws import ChatBedrockConverse
        return ChatBedrockConverse(
            model=runtime.runtime_model_name,
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
            temperature=0.2,
        )

    raise ValueError(f"Unsupported provider: '{runtime.runtime_provider}'")


# ---- Mock classifier ----

def _mock_classify(headline: str, candidate_suppliers: list[str], context: str) -> dict:
    """Deterministic mock output for offline testing. Not intended to be accurate."""
    if candidate_suppliers and context:
        return {
            "status": "ok",
            "supplier": candidate_suppliers[0],
            "risk_type": "outage",
            "risk_level": "Medium",
            "impact": f"Mock impact for {candidate_suppliers[0]}: disruption detected.",
            "recommended_action": "Mock: monitor the situation closely.",
            "relevant_supplier_context": context[:120],
        }
    return {
        "status": "inconclusive",
        "supplier": None,
        "risk_type": None,
        "risk_level": None,
        "impact": "Mock: no clearly relevant supplier identified.",
        "recommended_action": "Mock: monitor for additional signals.",
        "relevant_supplier_context": "",
    }


# ---- Single case execution ----

def run_case(
    case: dict,
    prompt_record: PromptRecord,
    llm=None,
    mock: bool = False,
) -> dict:
    """Run one eval case and return the normalised actual output dict."""
    headline = case["headline"]
    candidate_suppliers = case.get("candidate_suppliers") or []
    context = case.get("context") or "No additional context retrieved."

    if mock:
        return _mock_classify(headline, candidate_suppliers, context)

    prompt = prompt_record.template.format(
        headline=headline,
        candidate_suppliers=", ".join(candidate_suppliers) if candidate_suppliers else "None",
        context=context,
    )

    response = llm.invoke(prompt)
    actual = _parse_alert(_extract_text(response))

    actual["risk_type"] = _normalize_risk_type(actual.get("risk_type"))
    if actual.get("risk_level"):
        actual["risk_level"] = actual["risk_level"].strip().title()
    if actual.get("status") == "inconclusive":
        actual["supplier"] = None
        actual["risk_type"] = None
        actual["risk_level"] = None

    return actual


# ---- Comparison ----

def compare_case(case: dict, actual: dict) -> dict:
    """Compare actual output against expected values and return a result record."""
    expected = case["expected"]
    failures: list[str] = []

    status_pass = actual.get("status") == expected.get("status")
    if not status_pass:
        failures.append(
            f"status: expected={expected.get('status')!r}  actual={actual.get('status')!r}"
        )

    exp_supplier = (expected.get("supplier") or "").lower().strip()
    act_supplier = (actual.get("supplier") or "").lower().strip()
    supplier_pass = act_supplier == exp_supplier
    if not supplier_pass:
        failures.append(
            f"supplier: expected={expected.get('supplier')!r}  actual={actual.get('supplier')!r}"
        )

    exp_risk_type = _normalize_risk_type(expected.get("risk_type"))
    act_risk_type = _normalize_risk_type(actual.get("risk_type"))
    risk_type_pass = act_risk_type == exp_risk_type
    if not risk_type_pass:
        failures.append(
            f"risk_type: expected={exp_risk_type!r}  actual={act_risk_type!r}"
        )

    exp_level = (expected.get("risk_level") or "").strip().title()
    act_level = (actual.get("risk_level") or "").strip().title()
    risk_level_pass = act_level == exp_level
    if not risk_level_pass:
        failures.append(
            f"risk_level: expected={exp_level!r}  actual={act_level!r}"
        )

    terms = expected.get("must_include_terms") or []
    searchable = " ".join(
        str(v) for v in [
            actual.get("impact"),
            actual.get("recommended_action"),
            actual.get("relevant_supplier_context"),
        ]
        if v
    ).lower()
    missing_terms = [t for t in terms if t.lower() not in searchable]
    terms_pass = not missing_terms
    if missing_terms:
        failures.append(f"must_include_terms: missing {missing_terms!r}")

    return {
        "eval_id": case["eval_id"],
        "passed": not failures,
        "field_results": {
            "status": status_pass,
            "supplier": supplier_pass,
            "risk_type": risk_type_pass,
            "risk_level": risk_level_pass,
            "must_include_terms": terms_pass,
        },
        "failures": failures,
        "actual": actual,
        "expected": expected,
    }


# ---- Metrics ----

def compute_metrics(results: list[dict]) -> dict:
    """Aggregate per-case results into summary metrics."""
    total = len(results)
    if total == 0:
        return {
            "total": 0, "passed": 0, "failed": 0, "pass_rate": 0.0,
            "status_accuracy": 0.0, "supplier_accuracy": 0.0,
            "risk_type_accuracy": 0.0, "risk_level_accuracy": 0.0,
        }

    passed = sum(1 for r in results if r["passed"])

    def _acc(field: str) -> float:
        return round(sum(1 for r in results if r["field_results"][field]) / total, 3)

    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 3),
        "status_accuracy": _acc("status"),
        "supplier_accuracy": _acc("supplier"),
        "risk_type_accuracy": _acc("risk_type"),
        "risk_level_accuracy": _acc("risk_level"),
    }


# ---- Report ----

def format_report(
    results: list[dict],
    metrics: dict,
    registry_info: dict,
    mock: bool = False,
) -> str:
    mode_tag = "  [MOCK MODE — outputs are deterministic, not model-driven]\n" if mock else ""
    lines = [
        "=" * 62,
        "Risk Classifier Evaluation Report",
        "=" * 62,
        mode_tag,
        "Registry",
        f"  prompt_id:          {registry_info.get('prompt_id')}",
        f"  prompt_version:     {registry_info.get('prompt_version')}",
        f"  model_id:           {registry_info.get('model_id')}",
        f"  model_version:      {registry_info.get('model_version')}",
        f"  runtime_provider:   {registry_info.get('runtime_provider')}",
        f"  runtime_model_name: {registry_info.get('runtime_model_name')}",
        "",
        "Metrics",
        f"  Total cases:         {metrics['total']}",
        f"  Passed:              {metrics['passed']}",
        f"  Failed:              {metrics['failed']}",
        f"  Pass rate:           {metrics['pass_rate']:.1%}",
        f"  Status accuracy:     {metrics['status_accuracy']:.1%}",
        f"  Supplier accuracy:   {metrics['supplier_accuracy']:.1%}",
        f"  Risk type accuracy:  {metrics['risk_type_accuracy']:.1%}",
        f"  Risk level accuracy: {metrics['risk_level_accuracy']:.1%}",
    ]

    failures = [r for r in results if not r["passed"]]
    if failures:
        lines += ["", f"Failures ({len(failures)})", "-" * 40]
        for r in failures:
            lines.append(f"  FAIL  {r['eval_id']}")
            for f in r["failures"]:
                lines.append(f"        {f}")
    else:
        lines += ["", "  All cases passed."]

    lines += ["", "=" * 62]
    return "\n".join(lines)


# ---- Orchestration ----

def run_eval(
    dataset_path: Path = DEFAULT_DATASET,
    mock: bool = False,
) -> dict:
    """Load the dataset, run every case, and return results + metrics + registry_info."""
    cases = load_dataset(dataset_path)

    prompt_record = get_prompt("risk_classifier")
    model_record = get_model("risk_analysis_primary")
    runtime = resolve_model_runtime(model_record)

    registry_info = {
        "prompt_id": prompt_record.prompt_id,
        "prompt_version": prompt_record.version,
        "model_id": model_record.model_id,
        "model_version": model_record.version,
        "runtime_provider": runtime.runtime_provider,
        "runtime_model_name": runtime.runtime_model_name,
    }

    llm = None
    if not mock:
        try:
            llm = _build_llm(model_record)
        except Exception as exc:
            print(
                f"ERROR: Could not initialise LLM — {exc}\n"
                f"  Ensure OPENAI_API_KEY is set (or Bedrock credentials for bedrock provider),\n"
                f"  or run with --mock for offline evaluation.",
                file=sys.stderr,
            )
            sys.exit(1)

    results = [
        compare_case(case, run_case(case, prompt_record, llm=llm, mock=mock))
        for case in cases
    ]

    return {
        "results": results,
        "metrics": compute_metrics(results),
        "registry_info": registry_info,
    }


# ---- CLI entry point ----

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Risk classifier evaluation harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run python -m app.evaluation.risk_classifier_eval\n"
            "  uv run python -m app.evaluation.risk_classifier_eval --mock\n"
            "  uv run python -m app.evaluation.risk_classifier_eval --dataset path/to/cases.jsonl"
        ),
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use deterministic mock outputs instead of live LLM (no credentials required)",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        metavar="PATH",
        help=f"Path to JSONL eval dataset (default: {DEFAULT_DATASET})",
    )
    args = parser.parse_args()

    output = run_eval(dataset_path=args.dataset, mock=args.mock)
    print(format_report(output["results"], output["metrics"], output["registry_info"], mock=args.mock))

    if output["metrics"]["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
