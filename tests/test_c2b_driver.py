"""C2b driver tests.

Process-kill tests: every attempt is a separate Python process
(tests/c2b_local_child.py running scripts/c2b_analysis_kill.py unchanged),
the held attempt is terminated with SIGKILL, and only files fsynced to disk
carry state to the next process (FileCheckpointSaver, CSV supplier state,
JSONL traces, the event log). These do not exercise DynamoCheckpointSaver;
that is left to the AWS run.

Unit tests at the bottom run in-process and kill nothing.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from scripts import c2b_analysis_kill as driver  # noqa: E402
from tests.c2b_local_child import RISK_ALERT  # noqa: E402

CHILD = str(PROJECT_ROOT / "tests" / "c2b_local_child.py")
HEADLINE = "TSMC halts production after earthquake in Taiwan"
HOLD_WAIT_S = 60


class Run:
    def __init__(self, base: Path):
        self.base = base
        self.events = base / "events.jsonl"
        self.env = {**os.environ, "C2B_DIR": str(base)}

    def child(self, *args, check=True) -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, CHILD, *args], cwd=PROJECT_ROOT, env=self.env,
                              capture_output=True, text=True, timeout=120)
        if check and proc.returncode != 0:
            raise AssertionError(f"{args[0]} exited {proc.returncode}: {proc.stderr[-2000:]}")
        return proc

    def start(self) -> str:
        out = self.child("start", "--headline", HEADLINE, "--events", str(self.events)).stdout
        return next(line.split("=", 1)[1] for line in out.splitlines() if line.startswith("THREAD_ID="))

    def snapshot(self, thread_id: str, label: str) -> dict:
        out = self.child("snapshot", "--thread-id", thread_id, "--events", str(self.events), "--label", label)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def held_attempt_killed(self, thread_id: str, hold: str, *, linger_s: float = 0.0) -> dict:
        """Start an attempt, wait for its durable 'holding' event, then SIGKILL it."""
        stderr_path = self.base / f"{hold}.stderr"
        with open(stderr_path, "w") as err:
            proc = subprocess.Popen(
                [sys.executable, CHILD, "attempt", "--thread-id", thread_id, "--events", str(self.events),
                 "--hold", hold, "--hold-timeout", str(HOLD_WAIT_S * 2)],
                cwd=PROJECT_ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=err)
        deadline = time.time() + HOLD_WAIT_S
        while time.time() < deadline:
            if any(e["event"] == "holding" and e["pid"] == proc.pid for e in driver.read_events(self.events)):
                break
            if proc.poll() is not None:
                raise AssertionError(f"attempt exited {proc.returncode} before holding: {stderr_path.read_text()}")
            time.sleep(0.05)
        else:
            proc.kill()
            raise AssertionError("attempt never reached its hold")
        time.sleep(linger_s)  # give anything that is not actually blocked a chance to run
        alive_before_kill = proc.poll() is None
        os.kill(proc.pid, signal.SIGKILL)
        returncode = proc.wait(timeout=30)
        return {"pid": proc.pid, "returncode": returncode, "alive_before_kill": alive_before_kill,
                "stderr": stderr_path.read_text()}

    def attempt(self, thread_id: str, *extra) -> dict:
        out = self.child("attempt", "--thread-id", thread_id, "--events", str(self.events), "--hold", "none", *extra)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def verdict(self, thread_id: str, expect: str, *extra) -> dict:
        proc = self.child("verdict", "--thread-id", thread_id, "--events", str(self.events),
                          "--expect", expect, *extra, check=False)
        return {**json.loads(proc.stdout), "exit_code": proc.returncode}

    def attempt_events(self, pid: int) -> list[str]:
        return [e["event"] for e in driver.read_events(self.events) if e["pid"] == pid]


def _assert_no_content_recorded(events_path: Path) -> None:
    text = events_path.read_text()
    for secret in (RISK_ALERT["impact"], RISK_ALERT["relevant_supplier_context"], "TSMC fabs are in Taiwan",
                   HEADLINE, "AWS_SECRET_ACCESS_KEY", "aws_session_token"):
        assert secret not in text, secret


# ---- Process-kill tests ----

def test_c1_sigkill_after_inference_reruns_analysis_but_not_retrieval(tmp_path):
    run = Run(tmp_path)
    thread_id = run.start()
    assert run.snapshot(thread_id, "before-kill")["next"] == ["analyze"]

    killed = run.held_attempt_killed(thread_id, "after-inference", linger_s=1.0)
    assert killed["returncode"] == -signal.SIGKILL
    assert f"pid={killed['pid']}" in killed["stderr"] and thread_id in killed["stderr"]
    killed_events = run.attempt_events(killed["pid"])
    assert killed_events[-3:] == ["model_invoke_start", "model_return", "holding"]
    assert "attempt_exit" not in killed_events  # no normal completion; the event log is the record
    [model_return] = [e for e in driver.read_events(run.events)
                      if e["pid"] == killed["pid"] and e["event"] == "model_return"]
    assert model_return["request_id"] == f"fake-req-{killed['pid']}"
    assert (model_return["input_tokens"], model_return["output_tokens"]) == (120, 40)

    after_kill = run.snapshot(thread_id, "after-kill")
    assert after_kill["next"] == ["analyze"] and not after_kill["analyze_writes_stored"]
    traces = [json.loads(line) for line in (tmp_path / "risk_traces.jsonl").read_text().splitlines()]
    assert [t["workflow_event"] for t in traces] == ["paused"]  # the killed attempt wrote no trace

    resumed = run.attempt(thread_id)
    assert (resumed["status"], resumed["eligibility_change_type"]) == ("completed", "suppressed")

    result = run.verdict(thread_id, "c1")
    assert result["passed"], {k: v for k, v in result["checks"]["acceptance"].items() if not v["ok"]}
    acceptance = result["checks"]["acceptance"]
    request_ids = [m["request_id"] for m in acceptance["resume_reran_analyze_and_inference"]["detail"]["model_returns"]]
    assert len(set(request_ids)) == 2  # two separate inferences
    _assert_no_content_recorded(run.events)


def _c3_killed(run: Run) -> tuple[str, dict]:
    thread_id = run.start()
    run.snapshot(thread_id, "before-kill")
    killed = run.held_attempt_killed(thread_id, "after-analyze-writes", linger_s=1.5)
    return thread_id, killed


def test_c3_hold_is_reached_after_writes_and_before_downstream(tmp_path):
    run = Run(tmp_path)
    thread_id, killed = _c3_killed(run)

    assert killed["returncode"] == -signal.SIGKILL and killed["alive_before_kill"]
    events = run.attempt_events(killed["pid"])
    assert events[events.index("analyze_return"):] == [
        "analyze_return", "writes_committed", "checkpoint_put_blocked", "holding",
    ]
    # Nothing downstream ran during the 1.5 s hold: no validate, no checkpoint committed.
    assert "validate_enter" not in events and "checkpoint_committed" not in events
    assert "downstream_during_hold" not in events

    after_kill = run.snapshot(thread_id, "after-kill")
    assert after_kill["analyze_writes_stored"] is True
    assert after_kill["step"] == 3  # still the pre-analyze checkpoint
    assert after_kill["tasks"] == [{"name": "analyze", "has_result": True, "error": False}]


def test_c3_langgraph_reuses_stored_writes_without_rerunning_analysis(tmp_path):
    run = Run(tmp_path)
    thread_id, _ = _c3_killed(run)
    run.snapshot(thread_id, "after-kill")

    direct = run.attempt(thread_id, "--direct-invoke")

    assert direct["status"] == "direct_invoke_returned"
    assert [s["node_name"] for s in direct["trace_steps"]][-3:] == ["analyze", "validate", "assess"]
    result = run.verdict(thread_id, "c3")
    assert result["checks"]["diagnostics"]["langgraph_direct_invoke_reuses_stored_writes"]["ok"] is True
    assert result["diagnostics_ok"] is True
    acceptance = result["checks"]["acceptance"]
    for name in ("killed_attempt_reached_hold", "nothing_downstream_before_kill",
                 "analysis_writes_durable_before_recovery"):
        assert acceptance[name]["ok"], (name, acceptance[name])
    # A diagnostic is not a lifecycle recovery: without a lifecycle RESUME, acceptance fails.
    assert acceptance["resume_did_not_execute_analyze_or_invoke_model"]["ok"] is False
    assert result["passed"] is False
    _assert_no_content_recorded(run.events)


def test_c3_lifecycle_resume_continues_from_stored_writes(tmp_path):
    """Regression: RESUME used to report already_completed here (next=() with analyze's result stored)."""
    run = Run(tmp_path)
    thread_id, _ = _c3_killed(run)
    run.snapshot(thread_id, "after-kill")

    resumed = run.attempt(thread_id)

    assert (resumed["status"], resumed["eligibility_change_type"]) == ("completed", "suppressed")
    assert resumed["state_written"] is True
    result = run.verdict(thread_id, "c3")
    assert result["passed"] and result["complete"], {
        k: v for k, v in result["checks"]["acceptance"].items() if not v["ok"]}
    acceptance = result["checks"]["acceptance"]
    assert acceptance["final_analysis_step_is_persisted_original"]["detail"]["basis"].startswith("exact")
    # The optional diagnostic was not run and does not affect acceptance.
    assert result["checks"]["diagnostics"]["langgraph_direct_invoke_reuses_stored_writes"]["status"] == "not_run"
    assert result["diagnostics_ok"] is None

    offline = run.verdict(thread_id, "c3", "--offline")
    assert (offline["passed"], offline["complete"], offline["exit_code"]) == (False, False, 1)
    assert offline["durable_state_read"] is False
    _assert_no_content_recorded(run.events)


# ---- c3 verdict: rejecting repeated inference and missing evidence ----
# One real c3 run (subprocesses, SIGKILL, lifecycle RESUME); its evidence is
# then altered in memory and re-evaluated with driver.evaluate.

@pytest.fixture(scope="module")
def c3_evidence(tmp_path_factory):
    run = Run(tmp_path_factory.mktemp("c3"))
    thread_id, _ = _c3_killed(run)
    run.snapshot(thread_id, "after-kill")
    run.attempt(thread_id)
    final_state = run.snapshot(thread_id, "final")
    events = driver.read_events(run.events)
    traces = [json.loads(line) for line in (run.base / "risk_traces.jsonl").read_text().splitlines()]
    paused = next(t for t in traces if t["workflow_event"] == "paused")
    final = next(t for t in traces if t["workflow_event"] == "resumed_completed")
    return {"events": events, "paused": paused, "final": final, "final_state": final_state}


def _evaluate(ev, events=None, final=None, final_state=None):
    return driver.evaluate(events if events is not None else ev["events"], "c3",
                           final if final is not None else ev["final"], ev["paused"],
                           final_state if final_state is not None else ev["final_state"])["acceptance"]


def _attempt(events, prefix):
    return next(e["attempt"] for e in events if e["attempt"].startswith(prefix))


def test_c3_evidence_passes_unaltered(c3_evidence):
    acceptance = _evaluate(c3_evidence)
    assert all(c["ok"] is True for c in acceptance.values()), {k: v for k, v in acceptance.items() if not v["ok"]}


def test_c3_verdict_rejects_repeated_inference_on_resume(c3_evidence):
    events = [dict(e) for e in c3_evidence["events"]]
    resume = _attempt(events, "resume-none-")
    exit_index = next(i for i, e in enumerate(events) if e["attempt"] == resume and e["event"] == "attempt_exit")
    events[exit_index:exit_index] = [
        {**events[exit_index], "event": "analyze_enter"},
        {**events[exit_index], "event": "model_return", "request_id": "second-inference"},
    ]
    assert _evaluate(c3_evidence, events=events)["resume_did_not_execute_analyze_or_invoke_model"]["ok"] is False


@pytest.mark.parametrize("drop, check", [
    (lambda e: e["attempt"].startswith("resume-none-") and e["event"] == "hooks_installed",
     "resume_did_not_execute_analyze_or_invoke_model"),  # no probes means no evidence of absence
    (lambda e: e["attempt"].startswith("resume-none-") and e["event"] == "attempt_exit",
     "resume_did_not_execute_analyze_or_invoke_model"),
    (lambda e: e["event"] == "durable_snapshot" and e["label"] == "after-kill",
     "analysis_writes_durable_before_recovery"),
    (lambda e: e["event"] == "writes_committed" and e.get("node") == "analyze",
     "analysis_writes_durable_before_recovery"),
    (lambda e: e["attempt"].startswith("resume-none-") and e["event"] == "writes_committed"
     and e.get("node") == "assess", "downstream_and_finalization_completed"),
])
def test_c3_verdict_rejects_missing_evidence(c3_evidence, drop, check):
    events = [e for e in c3_evidence["events"] if not drop(e)]
    assert _evaluate(c3_evidence, events=events)[check]["ok"] is False


def test_c3_verdict_rejects_analysis_step_that_is_not_the_persisted_one(c3_evidence):
    final = json.loads(json.dumps(c3_evidence["final"]))
    analyze = next(st for st in final["trace_steps"] if st["node_name"] == "analyze")
    analyze["started_at"] = analyze["ended_at"] = "2099-01-01T00:00:00.000+00:00"
    assert _evaluate(c3_evidence, final=final)["final_analysis_step_is_persisted_original"]["ok"] is False

    # Without the stored step (older snapshots) the killed attempt's window is the basis; it still rejects.
    events = [dict(e, pending_analyze_step=None) if e["event"] == "durable_snapshot" else e
              for e in c3_evidence["events"]]
    check = _evaluate(c3_evidence, events=events, final=final)["final_analysis_step_is_persisted_original"]
    assert check["ok"] is False and check["detail"]["basis"].startswith("within")
    assert _evaluate(c3_evidence, events=events)["final_analysis_step_is_persisted_original"]["ok"] is True


def test_c3_verdict_rejects_unfinished_finalization(c3_evidence):
    pending = {**c3_evidence["final_state"], "tasks": [{"name": "assess", "has_result": True, "error": False}]}
    assert _evaluate(c3_evidence, final_state=pending)["downstream_and_finalization_completed"]["ok"] is False


def test_c3_offline_verdict_cannot_pass(c3_evidence):
    acceptance = driver.evaluate(c3_evidence["events"], "c3", None, None, None)["acceptance"]
    not_evaluated = {k for k, c in acceptance.items() if c["ok"] is None}
    assert not_evaluated == {"retrieval_reused_from_start", "final_analysis_step_is_persisted_original",
                             "downstream_and_finalization_completed"}
    assert all(c["ok"] is True for k, c in acceptance.items() if k not in not_evaluated)


# ---- Unit tests (in-process; nothing is killed) ----

def test_response_fields_record_ids_and_usage_but_no_content():
    from langchain_core.messages import AIMessage

    message = AIMessage(content=json.dumps(RISK_ALERT),
                        response_metadata={"ResponseMetadata": {"RequestId": "req-1"}, "stopReason": "end_turn",
                                           "metrics": {"latencyMs": 812}},
                        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    fields = driver.response_fields(message)
    assert fields == {"request_id": "req-1", "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                      "stop_reason": "end_turn", "provider_latency_ms": 812, "response_type": "AIMessage"}
    assert RISK_ALERT["impact"] not in json.dumps(fields)


def test_hook_install_fails_clearly_when_hook_points_are_missing(monkeypatch, tmp_path):
    from app.workflows import supplier_risk_agent as agent

    monkeypatch.setattr(agent, "analyze_risk", lambda state: state)  # no _risk_llm.invoke inside
    with pytest.raises(driver.HookInstallError, match="analyze_risk no longer calls"):
        driver._verify_hook_points(agent)


def test_hook_install_refuses_non_bedrock_risk_model(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.workflows import supplier_risk_agent as agent

    monkeypatch.setattr(agent, "_risk_runtime", SimpleNamespace(runtime_provider="openai", runtime_model_name="x"))
    ctx = driver._Context("none", 1, driver.EventLog(tmp_path / "e.jsonl", attempt="t"), "thread-x")
    saver = SimpleNamespace(put=lambda *a: None, put_writes=lambda *a: None)
    with pytest.raises(driver.HookInstallError, match="only allows Bedrock"):
        driver.install_hooks(ctx, saver, require_bedrock=True)


def test_event_log_is_flushed_per_event(tmp_path):
    log = driver.EventLog(tmp_path / "e.jsonl", attempt="a")
    log.emit("one", x=1)
    # Read by a different handle while the writer is still open.
    assert [e["event"] for e in driver.read_events(tmp_path / "e.jsonl")] == ["one"]
