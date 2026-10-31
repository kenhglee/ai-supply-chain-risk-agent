"""Same-thread concurrent resume (R1-R4) and same-risk cross-thread races (X1).

Each worker is a separate Python process (tests/race_harness.py) running the
unmodified lifecycle against one persistent SQLite database. Gate files force
a specific interleaving; every scenario first verifies from the event log and
the database that the intended interleaving actually happened.

The observed-behavior tests pass today: they assert the unsafe behavior.
Future protection requirements are separate strict xfails.

See tests/race_harness.py for how the SQLite stores differ from DynamoDB.
Set RACE_REPORT_DIR to also write one JSON summary per scenario.
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from scripts.c2b_analysis_kill import read_events  # noqa: E402
from tests import race_harness  # noqa: E402

HARNESS = str(PROJECT_ROOT / "tests" / "race_harness.py")
WAIT_S = 45


class Race:
    """Spawns gated workers, releases gates, and always cleans up its processes."""

    def __init__(self, base: Path):
        self.base = base
        self.db = base / "race.sqlite"
        race_harness.init_db(self.db)
        self.procs: dict[str, subprocess.Popen] = {}

    # ---- processes ----

    def _cmd(self, worker, role, **opts):
        cmd = [sys.executable, HARNESS, "--dir", str(self.base), "--worker", worker, "--role", role]
        for key, value in opts.items():
            if value:
                cmd += [f"--{key.replace('_', '-')}", str(value)]
        return cmd

    def start_paused(self) -> str:
        """Setup: one thread, paused before analyze (triage and retrieval done)."""
        proc = subprocess.run(self._cmd("S", "start", pause_before="analyze"), cwd=PROJECT_ROOT,
                              capture_output=True, text=True, timeout=WAIT_S)
        assert proc.returncode == 0, proc.stderr[-2000:]
        return json.loads(proc.stdout.strip().splitlines()[-1])["thread_id"]

    def spawn(self, worker, role, *, gates, risk_level="High", thread_id=None, extra=()):
        err = open(self.base / f"{worker}.stderr", "w")
        self.procs[worker] = subprocess.Popen(
            self._cmd(worker, role, thread_id=thread_id, risk_level=risk_level, gates=",".join(gates))
            + list(extra),
            cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=err)
        err.close()

    def wait_at(self, worker, point):
        marker, deadline = self.base / f"at-{worker}-{point}", time.monotonic() + WAIT_S
        while not marker.exists():
            proc = self.procs[worker]
            if proc.poll() is not None:
                raise AssertionError(f"{worker} exited {proc.returncode} before {point}: "
                                     f"{(self.base / f'{worker}.stderr').read_text()[-1500:]}")
            if time.monotonic() > deadline:
                raise AssertionError(f"{worker} never reached {point}")
            time.sleep(0.01)

    def release(self, worker, point):
        (self.base / f"go-{worker}-{point}").touch()

    def wait_exit(self, worker) -> int:
        return self.procs[worker].wait(timeout=WAIT_S)

    def cleanup(self):
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=10)

    # ---- evidence ----

    def events(self, worker=None):
        return [e for e in read_events(self.base / "events.jsonl") if worker is None or e["attempt"] == worker]

    def names(self, worker):
        return [e["event"] for e in self.events(worker)]

    def first(self, worker, event, **match):
        return next((e for e in self.events(worker) if e["event"] == event
                     and all(e.get(k) == v for k, v in match.items())), None)

    def query(self, sql, *params):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params)]

    def summary(self, thread_ids) -> dict:
        marks = ",".join("?" * len(thread_ids))
        checkpoints = self.query(f"SELECT thread_id, checkpoint_id, parent_id, step, writer_trace_id "
                                 f"FROM checkpoints WHERE thread_id IN ({marks}) ORDER BY checkpoint_id", *thread_ids)
        children: dict[str, list] = {}
        for c in checkpoints:
            children.setdefault(c["parent_id"], []).append(c)
        traces = [json.loads(line) for line in (self.base / "risk_traces.jsonl").read_text().splitlines()]
        exits = {w: self.first(w, "attempt_exit") or self.first(w, "attempt_error") for w in self.procs}
        return {
            "inference_count": {w: self.names(w).count("model_invoke") for w in self.procs},
            "process_outcomes": {w: {"returncode": p.returncode, **{k: (exits[w] or {}).get(k) for k in (
                "event", "status", "error_type", "eligibility_change_type", "change_type", "risk_level",
                "action_status", "trace_id")}} for w, p in self.procs.items()},
            "checkpoints": len(checkpoints),
            "forks": {parent: [(c["step"], c["writer_trace_id"]) for c in kids]
                      for parent, kids in children.items()
                      if parent and len({c["writer_trace_id"] for c in kids}) > 1},
            "write_attempts_dropped": self.query("SELECT task_path, writer_trace_id FROM write_attempts "
                                                 "WHERE stored = 0"),
            "actions": self.query("SELECT action_id, status, claimed_by_trace_id, attempt_count, subject "
                                  "FROM actions"),
            "claim_attempts": self.query("SELECT action_id, trace_id, outcome FROM claim_attempts ORDER BY at"),
            "effects": [(e["attempt"], e.get("risk_level"), e.get("ticket_id"))
                        for e in self.events() if e["event"] == "effect_executed"],
            "supplier_state": self.query("SELECT row FROM supplier_state"),
            "trace_events": [(t.get("thread_id"), t.get("workflow_event"), t.get("change_type"))
                             for t in traces],
            "latest_checkpoint_writer": {tid: [c for c in checkpoints if c["thread_id"] == tid][-1]["writer_trace_id"]
                                         for tid in thread_ids},
        }


def _analyze_writes(race) -> int:
    return race.query("SELECT count(*) n FROM writes WHERE task_path LIKE '%analyze'")[0]["n"]


def _wait_until(predicate, what):
    deadline = time.monotonic() + WAIT_S
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


def _both_inside_analyze(race):
    """Barrier inside analyze: both loaded the same checkpoint; no analyze write exists yet."""
    for w in "AB":
        race.wait_at(w, "analyze-enter")
    assert _analyze_writes(race) == 0


def _report(name, summary):
    if out := os.environ.get("RACE_REPORT_DIR"):
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"{name}.json").write_text(json.dumps(summary, indent=2, default=str))


def _scenario(tmp_path_factory, name, script):
    race = Race(tmp_path_factory.mktemp(name))
    try:
        result = script(race)
    finally:
        race.cleanup()
    result["summary"] = race.summary(result["thread_ids"])
    _report(name, result["summary"])
    result["race"] = race
    return result


def _state_row(race) -> dict:
    rows = race.query("SELECT row FROM supplier_state")
    assert len(rows) <= 1, rows
    return json.loads(rows[0]["row"]) if rows else {}


def _ts(race, worker, event, **match):
    return race.first(worker, event, **match)["ts"]


# ---- R1: double analysis from the same checkpoint ----

def _r1(race):
    thread_id = race.start_paused()
    for w, level in (("A", "High"), ("B", "Medium")):
        race.spawn(w, "resume", thread_id=thread_id, risk_level=level, gates=["analyze-enter", "after-analyze"])
    _both_inside_analyze(race)
    analyze_writes_before = _analyze_writes(race)
    for w in "AB":
        race.release(w, "analyze-enter")
    for w in "AB":
        race.wait_at(w, "after-analyze")
    analyze_writes_at_hold = _analyze_writes(race)
    race.release("A", "after-analyze")
    rc_a = race.wait_exit("A")
    race.release("B", "after-analyze")
    rc_b = race.wait_exit("B")
    return {"thread_ids": [thread_id], "rc": (rc_a, rc_b),
            "analyze_writes": (analyze_writes_before, analyze_writes_at_hold)}


@pytest.fixture(scope="module")
def r1(tmp_path_factory):
    return _scenario(tmp_path_factory, "R1-double-analysis", _r1)


def test_r1_interleaving_occurred(r1):
    race = r1["race"]
    # Both passed the resumability check and executed analyze from the same loaded checkpoint.
    assert race.first("A", "analyze_enter")["checkpoint_id"] == race.first("B", "analyze_enter")["checkpoint_id"]
    # Both had inferred before any analyze write existed; A was released first.
    assert r1["analyze_writes"] == (0, 0)
    assert _ts(race, "B", "gate_reached", point="after-analyze") < _ts(race, "A", "gate_released",
                                                                        point="after-analyze")


def test_r1_observed_double_inference_forked_history_and_divergent_outcome(r1):
    s = r1["summary"]
    assert r1["rc"] == (0, 0)
    assert s["inference_count"] == {"A": 1, "B": 1}  # one paused thread, two inferences
    # B's analyze writes lost the first-write-wins race, but B continued with its own alert.
    assert {d["writer_trace_id"] for d in s["write_attempts_dropped"]} == {s["process_outcomes"]["B"]["trace_id"]}
    assert all(d["task_path"].endswith("analyze") for d in s["write_attempts_dropped"])
    # The pre-analyze checkpoint has children written by both workers.
    [(parent, kids)] = s["forks"].items()
    assert {t for _, t in kids} == {s["process_outcomes"]["A"]["trace_id"], s["process_outcomes"]["B"]["trace_id"]}
    # A acted on High; B then assessed its own Medium alert against A's row: downgraded, no action.
    assert [(e[1]) for e in s["effects"]] == ["High"]
    assert (s["process_outcomes"]["A"]["change_type"], s["process_outcomes"]["B"]["change_type"]) == (
        "new_alert", "downgraded")
    assert _state_row(r1["race"])["current_risk_level"] == "Medium"  # business state from B, ticket from A
    assert s["latest_checkpoint_writer"][r1["thread_ids"][0]] == s["process_outcomes"]["B"]["trace_id"]


@pytest.mark.xfail(strict=True, reason="Requirement: a thread resumed concurrently runs inference at most once; "
                                        "the second worker reports contention and executes nothing")
def test_r1_requirement_single_execution(r1):
    s = r1["summary"]
    assert sum(s["inference_count"].values()) == 1


@pytest.mark.xfail(strict=True, reason="Requirement: concurrent resume never forks a thread's checkpoint history")
def test_r1_requirement_no_checkpoint_fork(r1):
    assert r1["summary"]["forks"] == {}


# ---- R2: second worker reaches act while the first is inside the effect ----

def _release_a_then_b_from_analyze(race):
    """A's analyze writes are stored first; B, already executing analyze, follows."""
    _both_inside_analyze(race)
    race.release("A", "analyze-enter")
    _wait_until(lambda: _analyze_writes(race) > 0, "A's analyze writes")
    race.release("B", "analyze-enter")


def _r2(race):
    thread_id = race.start_paused()
    race.spawn("A", "resume", thread_id=thread_id, gates=["analyze-enter", "inside-effect"])
    race.spawn("B", "resume", thread_id=thread_id, gates=["analyze-enter", "after-assess"])
    _release_a_then_b_from_analyze(race)
    race.wait_at("A", "inside-effect")
    race.wait_at("B", "after-assess")
    ledger_while_a_in_effect = race.query("SELECT status, claimed_by_trace_id FROM actions")
    race.release("B", "after-assess")
    rc_b = race.wait_exit("B")
    race.release("A", "inside-effect")
    rc_a = race.wait_exit("A")
    return {"thread_ids": [thread_id], "rc": (rc_a, rc_b), "ledger_while_a_in_effect": ledger_while_a_in_effect}


@pytest.fixture(scope="module")
def r2(tmp_path_factory):
    return _scenario(tmp_path_factory, "R2-action-in-flight", _r2)


def test_r2_interleaving_occurred(r2):
    race, s = r2["race"], r2["summary"]
    assert race.first("A", "analyze_enter")["checkpoint_id"] == race.first("B", "analyze_enter")["checkpoint_id"]
    a_trace = s["process_outcomes"]["A"]["trace_id"]
    assert r2["ledger_while_a_in_effect"] == [{"status": "IN_FLIGHT", "claimed_by_trace_id": a_trace}]
    assert race.first("B", "assess_return")["action_eligible"] is True  # B assessed before A finalized
    assert _ts(race, "B", "attempt_error") < _ts(race, "A", "gate_released", point="inside-effect")


def test_r2_observed_unhandled_action_in_flight_and_single_effect(r2):
    s = r2["summary"]
    assert r2["rc"] == (0, 1)
    assert s["process_outcomes"]["B"]["error_type"] == "ActionInFlight"  # an exception, not an outcome
    assert [c["outcome"] for c in s["claim_attempts"]] == ["claimed", "in_flight"]
    assert len(s["effects"]) == 1
    assert s["inference_count"] == {"A": 1, "B": 1}
    assert s["process_outcomes"]["A"]["action_status"] == "COMPLETED"


@pytest.mark.xfail(strict=True, reason="Requirement: action contention is reported as a lifecycle outcome "
                                        "(no unhandled ActionInFlight); the contending worker exits cleanly")
def test_r2_requirement_contention_is_an_outcome(r2):
    assert r2["summary"]["process_outcomes"]["B"]["event"] == "attempt_exit"


# ---- R3: both assessed eligible, divergent alerts; B replays A's action ----

def _both_assessed_then_ordered(race, levels):
    thread_id = race.start_paused()
    for w, level in zip("AB", levels):
        race.spawn(w, "resume", thread_id=thread_id, risk_level=level,
                   gates=["analyze-enter", "after-assess", "before-finalize"])
    _release_a_then_b_from_analyze(race)
    for w in "AB":
        race.wait_at(w, "after-assess")
    before_any_act = {"actions": race.query("SELECT * FROM actions"), "state": race.query("SELECT * FROM supplier_state")}
    race.release("A", "after-assess")
    race.wait_at("A", "before-finalize")
    race.release("B", "after-assess")
    race.wait_at("B", "before-finalize")
    race.release("A", "before-finalize")
    rc_a = race.wait_exit("A")
    race.release("B", "before-finalize")
    rc_b = race.wait_exit("B")
    return {"thread_ids": [thread_id], "rc": (rc_a, rc_b), "before_any_act": before_any_act}


@pytest.fixture(scope="module")
def r3(tmp_path_factory):
    return _scenario(tmp_path_factory, "R3-divergent-replay", lambda race: _both_assessed_then_ordered(
        race, ("High", "Medium")))


def test_r3_interleaving_occurred(r3):
    race = r3["race"]
    assert race.first("A", "analyze_enter")["checkpoint_id"] == race.first("B", "analyze_enter")["checkpoint_id"]
    # Both workers assessed eligibility before A acted or finalized.
    for w in "AB":
        assessed = race.first(w, "assess_return")
        assert (assessed["eligibility_change_type"], assessed["action_eligible"]) == ("new_alert", True)
    assert r3["before_any_act"] == {"actions": [], "state": []}
    assert max(_ts(race, w, "assess_return") for w in "AB") < _ts(race, "A", "effect_executed")
    # B reached act only after A's action completed, and finalized after A.
    assert _ts(race, "A", "gate_reached", point="before-finalize") < _ts(race, "B", "gate_released",
                                                                          point="after-assess")
    assert _ts(race, "A", "finalize_return") < _ts(race, "B", "finalize_enter")


def test_r3_observed_replay_with_divergent_business_state(r3):
    s, race = r3["summary"], r3["race"]
    assert r3["rc"] == (0, 0)
    assert [c["outcome"] for c in s["claim_attempts"]] == ["claimed", "replayed"]
    assert s["effects"] == [("A", "High", s["effects"][0][2])]  # the one ticket is for A's High alert
    [action] = s["actions"]
    assert json.loads(action["subject"])["risk_level"] == "High"
    b = s["process_outcomes"]["B"]
    # B decided to act on its own (Medium) analysis, "completed" by replaying A's action...
    assert (b["eligibility_change_type"], b["action_status"], b["risk_level"]) == ("new_alert", "COMPLETED", "Medium")
    # ...and then wrote business state from that Medium analysis.
    assert b["change_type"] == "downgraded"
    assert _state_row(race)["current_risk_level"] == "Medium"


@pytest.mark.xfail(strict=True, reason="Requirement: a thread's action and its business-state finalization come "
                                        "from the same analysis (one owner)")
def test_r3_requirement_action_and_business_state_agree(r3):
    [action] = r3["summary"]["actions"]
    assert json.loads(action["subject"])["risk_level"] == _state_row(r3["race"])["current_risk_level"]


# ---- R4: identical analyses, double finalization ----

@pytest.fixture(scope="module")
def r4(tmp_path_factory):
    return _scenario(tmp_path_factory, "R4-double-finalization", lambda race: _both_assessed_then_ordered(
        race, ("High", "High")))


def test_r4_interleaving_occurred(r4):
    race = r4["race"]
    assert race.first("A", "analyze_enter")["checkpoint_id"] == race.first("B", "analyze_enter")["checkpoint_id"]
    assert all(race.first(w, "assess_return")["eligibility_change_type"] == "new_alert" for w in "AB")
    assert _ts(race, "A", "finalize_return") < _ts(race, "B", "finalize_enter")


def test_r4_observed_two_finalizations_for_one_thread(r4):
    s = r4["summary"]
    assert r4["rc"] == (0, 0)
    assert len(s["effects"]) == 1
    assert (s["process_outcomes"]["A"]["change_type"], s["process_outcomes"]["B"]["change_type"]) == (
        "new_alert", "suppressed")
    thread_id = r4["thread_ids"][0]
    assert [e for e in s["trace_events"] if e[0] == thread_id and e[1] == "resumed_completed"] == [
        (thread_id, "resumed_completed", "new_alert"), (thread_id, "resumed_completed", "suppressed")]
    assert s["forks"]


@pytest.mark.xfail(strict=True, reason="Requirement: a thread's business state is finalized exactly once")
def test_r4_requirement_single_finalization(r4):
    s = r4["summary"]
    assert len([e for e in s["trace_events"] if e[1] == "resumed_completed"]) == 1


# ---- X1: different threads, same business change (not a same-thread race) ----

def _x1(race):
    for w in "AB":
        race.spawn(w, "start", gates=["after-assess", "before-finalize"])
    for w in "AB":
        race.wait_at(w, "after-assess")
    for w in "AB":
        race.release(w, "after-assess")
    for w in "AB":
        race.wait_at(w, "before-finalize")
    race.release("A", "before-finalize")
    rc_a = race.wait_exit("A")
    race.release("B", "before-finalize")
    rc_b = race.wait_exit("B")
    thread_ids = sorted({e["thread_id"] for e in race.events() if e["event"] == "attempt_exit"})
    return {"thread_ids": thread_ids, "rc": (rc_a, rc_b)}


@pytest.fixture(scope="module")
def x1(tmp_path_factory):
    return _scenario(tmp_path_factory, "X1-cross-thread-same-risk", _x1)


def test_x1_interleaving_occurred(x1):
    race = x1["race"]
    assert len(x1["thread_ids"]) == 2
    assert all(race.first(w, "assess_return")["eligibility_change_type"] == "new_alert" for w in "AB")
    assert max(_ts(race, w, "assess_return") for w in "AB") < min(_ts(race, w, "effect_executed") for w in "AB")


def test_x1_observed_two_tickets_for_one_business_change(x1):
    s = x1["summary"]
    assert x1["rc"] == (0, 0)
    assert len(s["actions"]) == 2 and len({a["action_id"] for a in s["actions"]}) == 2
    assert len(s["effects"]) == 2
    assert s["forks"] == {}  # per-thread histories are linear: this is not a same-thread race
    assert sorted(o["change_type"] for o in s["process_outcomes"].values()) == ["new_alert", "suppressed"]


@pytest.mark.xfail(strict=True, reason="Requirement (separate from per-thread ownership): one business change "
                                        "opens at most one ticket across threads")
def test_x1_requirement_one_ticket_per_business_change(x1):
    assert len(x1["summary"]["effects"]) == 1
