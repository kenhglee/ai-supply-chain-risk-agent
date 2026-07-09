import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.copilot.planner_copilot import (
    CopilotLogStore,
    PlannerCopilot,
    RECOMMENDED_ACTION,
    REVIEW_APPROVED_DECISION,
    _business_impact,
    _confidence,
    _match_supplier,
    classify_intent,
    graph_exposures,
    load_supplier_risk_rows,
    top_suppliers,
)


# ---- classify_intent ----

def test_classify_intent_monitor():
    assert classify_intent("Which suppliers should I monitor this week?") == "monitor"


def test_classify_intent_why():
    assert classify_intent("Why?") == "why"
    assert classify_intent("why is that") == "why"


def test_classify_intent_create_ticket():
    assert classify_intent("Create a review ticket for the top supplier.") == "create_ticket"


def test_classify_intent_unknown():
    assert classify_intent("What's the weather?") == "unknown"
    assert classify_intent("") == "unknown"


def test_classify_intent_approve_review():
    assert classify_intent("Approve review") == "approve_review"
    assert classify_intent("Please approve the review") == "approve_review"


def test_classify_intent_why_with_supplier_name():
    assert classify_intent("Why TSMC?") == "why"


# ---- _confidence / _business_impact / _match_supplier ----

def test_confidence_buckets_by_signal_count():
    assert _confidence(0) == "Low"
    assert _confidence(1) == "Low"
    assert _confidence(2) == "Medium"
    assert _confidence(3) == "High"
    assert _confidence(10) == "High"


def test_business_impact_includes_risk_type_and_exposures():
    text = _business_impact("TSMC", "earthquake", ["drought", "earthquake"])
    assert "TSMC" in text
    assert "earthquake" in text
    assert "drought" in text


def test_business_impact_handles_missing_risk_type_and_exposures():
    text = _business_impact("TSMC", None, [])
    assert "TSMC" in text
    assert "unspecified" in text
    assert "no structural exposure on record" in text


def test_match_supplier_case_insensitive():
    suppliers = [{"supplier": "TSMC"}, {"supplier": "Foxconn"}]
    assert _match_supplier("Why tsmc?", suppliers)["supplier"] == "TSMC"
    assert _match_supplier("why FOXCONN", suppliers)["supplier"] == "Foxconn"
    assert _match_supplier("Why?", suppliers) is None


# ---- top_suppliers ----

def _row(supplier, risk_type, level, headline, seen_at):
    return {
        "supplier": supplier,
        "risk_type": risk_type,
        "current_risk_level": level,
        "last_headline": headline,
        "last_seen_at": seen_at,
    }


def test_top_suppliers_ranks_by_risk_level_then_recency():
    rows = [
        _row("Foxconn", "geopolitical_tension", "Medium", "h1", "2026-01-01T00:00:00+00:00"),
        _row("TSMC", "earthquake", "High", "h2", "2026-01-02T00:00:00+00:00"),
        _row("Murata", "flood", "Low", "h3", "2026-01-03T00:00:00+00:00"),
    ]
    ranked = top_suppliers(rows, limit=3)
    assert [s["supplier"] for s in ranked] == ["TSMC", "Foxconn", "Murata"]


def test_top_suppliers_picks_highest_risk_row_per_supplier():
    rows = [
        _row("TSMC", "drought", "Low", "old", "2026-01-01T00:00:00+00:00"),
        _row("TSMC", "earthquake", "High", "new", "2026-01-05T00:00:00+00:00"),
    ]
    ranked = top_suppliers(rows, limit=3)
    assert len(ranked) == 1
    assert ranked[0]["risk_level"] == "High"
    assert ranked[0]["risk_signal_count"] == 2


def test_top_suppliers_respects_limit():
    rows = [_row(f"S{i}", "risk", "Low", "h", "2026-01-01T00:00:00+00:00") for i in range(5)]
    assert len(top_suppliers(rows, limit=3)) == 3


def test_top_suppliers_empty():
    assert top_suppliers([], limit=3) == []


# ---- graph_exposures ----

def test_graph_exposures():
    graph = [
        ["TSMC", "operates_in", "Taiwan"],
        ["Taiwan", "has_risk", "earthquake"],
        ["Taiwan", "has_risk", "typhoon"],
        ["TSMC", "depends_on", "water"],
        ["water", "has_risk", "drought"],
    ]
    assert graph_exposures(graph, "TSMC") == ["drought", "earthquake", "typhoon"]
    assert graph_exposures(graph, "Unknown") == []


# ---- load_supplier_risk_rows (csv backend) ----

def test_load_supplier_risk_rows_csv(tmp_path, monkeypatch):
    path = tmp_path / "risk_state.csv"
    path.write_text(
        "supplier,risk_type,current_risk_level,last_headline,last_seen_at\n"
        "TSMC,earthquake,High,headline,2026-01-01T00:00:00+00:00\n"
    )
    monkeypatch.setenv("RISK_STATE_BACKEND", "csv")
    monkeypatch.setenv("RISK_STATE_FILE", str(path))
    rows = load_supplier_risk_rows()
    assert len(rows) == 1
    assert rows[0]["supplier"] == "TSMC"


def test_load_supplier_risk_rows_csv_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RISK_STATE_BACKEND", "csv")
    monkeypatch.setenv("RISK_STATE_FILE", str(tmp_path / "missing.csv"))
    assert load_supplier_risk_rows() == []


# ---- PlannerCopilot end-to-end conversation ----

class _FakeLogStore:
    def __init__(self):
        self.records = []

    def append(self, record):
        self.records.append(record)


def _fake_risk_rows():
    return [
        _row("TSMC", "earthquake", "High", "TSMC hit by earthquake risk", "2026-01-02T00:00:00+00:00"),
        _row("Foxconn", "geopolitical_tension", "Medium", "Foxconn faces tension", "2026-01-01T00:00:00+00:00"),
        _row("Murata", "flood", "Low", "Murata flood watch", "2026-01-03T00:00:00+00:00"),
    ]


def _fake_profiles():
    return {"TSMC": "TSMC profile text.", "Foxconn": "Foxconn profile text.", "Murata": "Murata profile text."}


def _fake_graph():
    return [["TSMC", "operates_in", "Taiwan"], ["Taiwan", "has_risk", "earthquake"]]


def _make_copilot(log_store=None, ticket_creator=None, decision_saver=None):
    return PlannerCopilot(
        risk_rows_loader=_fake_risk_rows,
        profiles_loader=_fake_profiles,
        graph_loader=_fake_graph,
        context_retriever=lambda query, suppliers: None,
        ticket_creator=ticket_creator or (lambda event, decision: {"ticket_id": "MOCK-CHG-TEST", **decision}),
        decision_saver=decision_saver or (lambda event, decision, ticket=None: "record-123"),
        log_store=log_store or _FakeLogStore(),
    )


def test_monitor_returns_top_three_suppliers():
    copilot = _make_copilot()
    answer = copilot.handle("Which suppliers should I monitor this week?")
    assert "TSMC" in answer
    assert "Foxconn" in answer
    assert "Murata" in answer
    assert answer.index("TSMC") < answer.index("Foxconn") < answer.index("Murata")
    assert copilot.last_recommendation is not None
    assert copilot.last_recommendation.suppliers[0]["supplier"] == "TSMC"


def test_monitor_response_includes_business_impact_confidence_and_recommended_action():
    copilot = _make_copilot()
    answer = copilot.handle("Which suppliers require attention today?")

    assert "impact:" in answer
    assert "confidence:" in answer
    assert "recommended action:" in answer

    top = copilot.last_recommendation.suppliers[0]
    assert top["supplier"] == "TSMC"
    assert top["confidence"] == "Low"  # single signal per supplier in fake fixture data
    assert top["recommended_action"] == RECOMMENDED_ACTION["High"]
    assert "TSMC" in top["business_impact"]


def test_why_without_prior_recommendation():
    copilot = _make_copilot()
    answer = copilot.handle("Why?")
    assert "don't have a prior recommendation" in answer


def test_why_explains_prior_recommendation():
    copilot = _make_copilot()
    copilot.handle("Which suppliers should I monitor this week?")
    answer = copilot.handle("Why?")
    assert "TSMC" in answer
    assert "earthquake" in answer
    assert "Structural exposure" in answer
    assert "TSMC profile text." in answer
    assert "Business impact" in answer
    assert "Confidence" in answer


def test_why_with_supplier_name_scopes_to_that_supplier():
    copilot = _make_copilot()
    copilot.handle("Which suppliers should I monitor this week?")
    answer = copilot.handle("Why TSMC?")

    assert "Evidence for TSMC:" in answer
    assert "- TSMC:" in answer
    assert "- Foxconn:" not in answer
    assert "- Murata:" not in answer
    assert "Prioritized above" in answer
    assert "Foxconn (Medium risk" in answer
    assert "Murata (Low risk" in answer


def test_why_with_supplier_name_no_comparison_when_single_supplier():
    copilot = PlannerCopilot(
        risk_rows_loader=lambda: [_row("TSMC", "earthquake", "High", "h", "2026-01-01T00:00:00+00:00")],
        profiles_loader=_fake_profiles,
        graph_loader=_fake_graph,
        context_retriever=lambda query, suppliers: None,
        decision_saver=lambda event, decision, ticket=None: "record-123",
        log_store=_FakeLogStore(),
    )
    copilot.handle("Which suppliers should I monitor this week?")
    answer = copilot.handle("Why TSMC?")
    assert "Prioritized above" not in answer


def test_create_ticket_without_prior_recommendation():
    copilot = _make_copilot()
    answer = copilot.handle("Create a review ticket for the top supplier.")
    assert "No recommendation to act on yet" in answer


def test_create_ticket_for_top_supplier():
    saved = {}

    def fake_ticket_creator(event, decision):
        return {"ticket_id": "MOCK-CHG-ABC123", **decision}

    def fake_decision_saver(event, decision, ticket=None):
        saved["event"] = event
        saved["decision"] = decision
        saved["ticket"] = ticket
        return "record-abc"

    copilot = _make_copilot(ticket_creator=fake_ticket_creator, decision_saver=fake_decision_saver)
    copilot.handle("Which suppliers should I monitor this week?")
    answer = copilot.handle("Create a review ticket for the top supplier.")

    assert "MOCK-CHG-ABC123" in answer
    assert "TSMC" in answer
    assert saved["event"]["supplier"] == "TSMC"
    assert saved["decision"]["decision"] == "manual_review_required"
    assert saved["decision"]["risk_score"] == 85


def test_approve_review_without_prior_recommendation():
    copilot = _make_copilot()
    answer = copilot.handle("Approve review")
    assert "No recommendation to act on yet" in answer


def test_approve_review_for_top_supplier():
    saved = {}

    def fake_decision_saver(event, decision, ticket=None):
        saved["event"] = event
        saved["decision"] = decision
        saved["ticket"] = ticket
        return "record-approved"

    def failing_ticket_creator(event, decision):
        raise AssertionError("approve_review must not create a ServiceNow ticket")

    copilot = _make_copilot(ticket_creator=failing_ticket_creator, decision_saver=fake_decision_saver)
    copilot.handle("Which suppliers should I monitor this week?")
    answer = copilot.handle("Approve review")

    assert "Approved review for TSMC" in answer
    assert "High risk" in answer
    assert "record-approved" in answer
    assert saved["event"]["supplier"] == "TSMC"
    assert saved["decision"]["decision"] == REVIEW_APPROVED_DECISION
    assert saved["decision"]["risk_score"] == 85
    assert saved["ticket"] is None


def test_unknown_question():
    copilot = _make_copilot()
    answer = copilot.handle("What's the capital of France?")
    assert "I can help with" in answer


def test_conversation_logs_each_turn():
    log_store = _FakeLogStore()
    copilot = _make_copilot(log_store=log_store)
    copilot.handle("Which suppliers should I monitor this week?")
    copilot.handle("Why?")
    copilot.handle("Create a review ticket for the top supplier.")

    assert len(log_store.records) == 3
    intents = [r["intent"] for r in log_store.records]
    assert intents == ["monitor", "why", "create_ticket"]
    for record in log_store.records:
        assert record["trace_id"]
        assert record["session_id"] == copilot.session_id
    assert log_store.records[1]["parent_trace_id"] == log_store.records[0]["trace_id"]
    assert log_store.records[2]["action"]["ticket"]["ticket_id"] == "MOCK-CHG-TEST"


def test_conversation_logs_approve_review_turn_chained_to_recommendation():
    log_store = _FakeLogStore()
    copilot = _make_copilot(log_store=log_store)
    copilot.handle("Which suppliers require attention today?")
    copilot.handle("Why TSMC?")
    copilot.handle("Approve review")

    intents = [r["intent"] for r in log_store.records]
    assert intents == ["monitor", "why", "approve_review"]
    monitor_trace_id = log_store.records[0]["trace_id"]
    assert log_store.records[1]["parent_trace_id"] == monitor_trace_id
    assert log_store.records[2]["parent_trace_id"] == monitor_trace_id
    assert log_store.records[2]["action"]["decision_record_id"] == "record-123"


def test_copilot_log_store_appends_jsonl(tmp_path):
    path = tmp_path / "copilot_log.jsonl"
    store = CopilotLogStore(path=path)
    store.append({"trace_id": "abc", "question": "hi"})
    store.append({"trace_id": "def", "question": "bye"})
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
