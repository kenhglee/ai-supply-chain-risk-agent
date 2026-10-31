"""The C2b driver on the owned lifecycle: contention and kill/expiry/takeover (separate processes).

Runs scripts/c2b_analysis_kill.py through tests/c2b_local_child.py with
C2B_OWNED=1 (SQLite stand-ins of every owned store). These are the local
rehearsals of the manual AWS experiments.
"""
import json
import os
import subprocess
import sys
import time

from scripts import c2b_analysis_kill as driver
from tests.test_c2b_driver import CHILD, PROJECT_ROOT, Run

SHORT_TTL_MS = 1500


class OwnedRun(Run):
    def __init__(self, base, ttl_ms=120_000):
        super().__init__(base)
        self.env.update(C2B_OWNED="1", C2B_LEASE_TTL_MS=str(ttl_ms))

    def held(self, thread_id, hold, *extra):
        err = open(self.base / f"{hold}.stderr", "w")
        proc = subprocess.Popen([sys.executable, CHILD, "attempt", "--thread-id", thread_id, "--events",
                                 str(self.events), "--hold", hold, "--hold-timeout", "120", *extra],
                                cwd=PROJECT_ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=err)
        err.close()
        deadline = time.time() + 60
        while not any(e["event"] == "holding" and e["pid"] == proc.pid for e in driver.read_events(self.events)):
            assert proc.poll() is None and time.time() < deadline, (self.base / f"{hold}.stderr").read_text()
            time.sleep(0.05)
        return proc


def _contention(tmp_path):
    run = OwnedRun(tmp_path)
    thread_id = run.start()
    release = tmp_path / "release-owner"
    owner = run.held(thread_id, "after-inference", "--release-file", str(release))
    try:
        run.snapshot(thread_id, "before-contender")
        contender = run.attempt(thread_id)
        run.snapshot(thread_id, "after-contender")
        release.touch()
        assert owner.wait(timeout=60) == 0
    finally:
        if owner.poll() is None:
            owner.kill()
    return run, thread_id, contender


def test_owned_contention_contender_does_nothing_and_owner_completes(tmp_path):
    run, thread_id, contender = _contention(tmp_path)

    assert contender["status"] == "contended"
    result = run.verdict(thread_id, "contention")
    assert result["passed"] and result["complete"], {
        k: v for k, v in result["checks"]["acceptance"].items() if not v["ok"]}
    assert result["code"]["git_commit"]  # code identity is recorded
    final = result["final_durable_state"]
    assert (final["final"]["epoch"], final["lease"]["state"], final["lease"]["epoch"]) == (2, "RELEASED", 2)


def test_contention_verdict_rejects_a_contender_that_ran_inference(tmp_path):
    run, thread_id, _ = _contention(tmp_path)
    events = driver.read_events(run.events)
    contender = next(e["attempt"] for e in events if e["event"] == "attempt_exit" and e.get("status") == "contended")
    events.insert(next(i for i, e in enumerate(events) if e["attempt"] == contender and e["event"] == "attempt_exit"),
                  {"ts": "x", "pid": 0, "attempt": contender, "seq": 99, "event": "model_return"})
    checks = driver.evaluate_contention(events, final_state=None, contender_traces=None, owner_trace=None)
    assert checks["acceptance"]["contender_executed_nothing"]["ok"] is False
    assert checks["acceptance"]["one_inference_total"]["ok"] is False


def test_owned_kill_is_contended_until_expiry_then_taken_over(tmp_path):
    run = OwnedRun(tmp_path, ttl_ms=SHORT_TTL_MS)
    thread_id = run.start()
    run.snapshot(thread_id, "before-kill")
    owner = run.held(thread_id, "after-inference")
    owner.kill()
    owner.wait(timeout=30)
    run.snapshot(thread_id, "after-kill")
    early = run.attempt(thread_id)  # the dead owner's lease has not expired yet
    time.sleep(SHORT_TTL_MS / 1000 + 0.5)
    late = run.attempt(thread_id)

    assert (early["status"], late["status"]) == ("contended", "completed")
    assert late["lease_epoch"] == early["holder"]["epoch"] + 1
    result = run.verdict(thread_id, "c1")
    acceptance = result["checks"]["acceptance"]
    assert result["passed"], {k: v for k, v in acceptance.items() if not v["ok"]}
    for name in ("ownership_contended_until_expiry", "ownership_takeover_epoch_increased",
                 "ownership_final_marker_and_lease"):
        assert acceptance[name]["ok"] is True, name
    assert acceptance["ownership_contended_until_expiry"]["detail"]["contended_attempts"] == 1


def test_direct_invoke_is_refused_on_owned_dependencies(tmp_path):
    run = OwnedRun(tmp_path)
    thread_id = run.start()
    proc = run.child("attempt", "--thread-id", thread_id, "--events", str(run.events), "--hold", "none",
                     "--direct-invoke", check=False)
    assert proc.returncode == 2 and "refuse" in proc.stderr


# ---- Contention verdict: missing vs unusable evidence ----

def _evaluate(run, events, **durable):
    return driver.evaluate_contention(events, final_state=durable.get("final_state"),
                                      contender_traces=durable.get("contender_traces"),
                                      owner_trace=durable.get("owner_trace"))["acceptance"]


def test_contention_verdict_without_contender_snapshots_is_incomplete_not_passed(tmp_path):
    run, thread_id, _ = _contention(tmp_path)
    events = [e for e in driver.read_events(run.events) if e["event"] != "durable_snapshot"]
    (tmp_path / "no-snapshots.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    proc = run.child("verdict", "--thread-id", thread_id, "--events", str(tmp_path / "no-snapshots.jsonl"),
                     "--expect", "contention", check=False)
    verdict = json.loads(proc.stdout)
    check = verdict["checks"]["acceptance"]["no_workflow_write_by_the_contender"]
    assert (check["ok"], check["status"]) == (None, "not_evaluated")
    assert (verdict["complete"], verdict["passed"], proc.returncode) == (False, False, 1)
    # The call-log check still reports what it can, as separate evidence.
    assert verdict["checks"]["acceptance"]["contender_write_calls_limited_to_the_lease_attempt"]["ok"] is True


def test_contention_snapshots_with_null_fields_fail_rather_than_match(tmp_path):
    run, _, _ = _contention(tmp_path)
    events = [dict(e, checkpoint_id=None, lease=None) if e["event"] == "durable_snapshot" else e
              for e in driver.read_events(run.events)]
    check = _evaluate(run, events)["no_workflow_write_by_the_contender"]
    assert check["ok"] is False and "lack checkpoint_id or lease" in check["detail"]["reason"]


def test_contention_snapshots_that_differ_fail(tmp_path):
    run, _, _ = _contention(tmp_path)
    events = [dict(e, checkpoint_id="changed") if e.get("label") == "after-contender" else e
              for e in driver.read_events(run.events)]
    assert _evaluate(run, events)["no_workflow_write_by_the_contender"]["ok"] is False


def test_contender_write_calls_beyond_the_lease_attempt_fail(tmp_path):
    run, _, _ = _contention(tmp_path)
    events = driver.read_events(run.events)
    exit_event = next(e for e in events if e["event"] == "attempt_exit" and e.get("status") == "contended")
    assert set(exit_event["aws_calls"]) <= {"dynamodb:Query", "dynamodb:GetItem", "dynamodb:UpdateItem"} or \
        exit_event["aws_calls"] == {}  # the real local run: reads plus at most the lease attempt
    for extra in ({"dynamodb:TransactWriteItems": 1}, {"dynamodb:UpdateItem": 2}, {"dynamodb:PutItem": 1}):
        altered = [dict(e, aws_calls={**(e.get("aws_calls") or {}), **extra}) if e is exit_event else e
                   for e in events]
        assert _evaluate(run, altered)["contender_write_calls_limited_to_the_lease_attempt"]["ok"] is False, extra
