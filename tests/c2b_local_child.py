"""Subprocess entry point for local C2b tests (not collected by pytest).

Runs scripts/c2b_analysis_kill.py unchanged, with local dependencies that
survive SIGKILL:

  FileCheckpointSaver  InMemorySaver whose whole state is atomically
                       replaced and fsynced on every put / put_writes.
  CsvRiskStateStore    seeded TSMC/earthquake High (assess: suppressed).
  JsonlTraceStore      under C2B_DIR.
  Fake models          triage "retrieve"; risk returns an AIMessage with a
                       request id and token usage, like Bedrock Converse.

    python tests/c2b_local_child.py <c2b_analysis_kill args...>     (env C2B_DIR)

C2B_OWNED=1 runs the owned lifecycle instead, on the SQLite stand-ins of
tests/race_harness.py (lease, fenced saver, finalization, fenced action gate,
write-once traces); C2B_LEASE_TTL_MS sets the lease TTL.
"""
import json
import os
import pickle
import sys
import threading
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

RISK_ALERT = {
    "status": "ok", "supplier": "TSMC", "risk_type": "earthquake", "risk_level": "High",
    "impact": "Wafer output delayed", "recommended_action": "Contact supplier",
    "relevant_supplier_context": "Fabs in Taiwan",
}


class FileCheckpointSaver(InMemorySaver):
    """Test-only persistent saver: survives process termination (fsync + atomic replace)."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self._lock = threading.Lock()
        if path.exists():
            data = pickle.loads(path.read_bytes())
            for thread_id, namespaces in data["storage"].items():
                for ns, checkpoints in namespaces.items():
                    self.storage[thread_id][ns].update(checkpoints)
            self.writes.update(data["writes"])
            self.blobs.update(data["blobs"])

    def _persist(self) -> None:
        data = {
            "storage": {t: {ns: dict(c) for ns, c in nss.items()} for t, nss in self.storage.items()},
            "writes": {k: dict(v) for k, v in self.writes.items()},
            "blobs": dict(self.blobs),
        }
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            fh.write(pickle.dumps(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        dir_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def put(self, config, checkpoint, metadata, new_versions):
        with self._lock:
            result = super().put(config, checkpoint, metadata, new_versions)
            self._persist()
            return result

    def put_writes(self, config, writes, task_id, task_path=""):
        with self._lock:
            super().put_writes(config, writes, task_id, task_path)
            self._persist()


class FakeTriage:
    def invoke(self, prompt):
        return "retrieve"


class FakeRisk:
    def invoke(self, prompt):
        return AIMessage(
            content=json.dumps(RISK_ALERT),
            response_metadata={"ResponseMetadata": {"RequestId": f"fake-req-{os.getpid()}"},
                               "stopReason": "end_turn", "metrics": {"latencyMs": 5}},
            usage_metadata={"input_tokens": 120, "output_tokens": 40, "total_tokens": 160},
        )


class FakeRetriever:
    retriever_id, retriever_version, embedding_provider, top_k = "fake_retriever", "v0", "fake", 2

    def retrieve(self, query, suppliers):
        return SimpleNamespace(context="TSMC fabs are in Taiwan.")


def seed_state_csv(path: Path) -> None:
    if not path.exists():
        path.write_text("supplier,risk_type,current_risk_level,last_headline,last_seen_at\n"
                        "TSMC,earthquake,High,earlier headline,2026-01-01T00:00:00+00:00\n")


def main(argv: list[str]) -> int:
    # Set only in the child process (importing this module from a test must not change env).
    os.environ["RISK_STATE_BACKEND"] = "csv"
    os.environ["TRACE_STORE_BACKEND"] = "jsonl"
    base = Path(os.environ["C2B_DIR"])
    state_csv = base / "risk_state.csv"
    seed_state_csv(state_csv)
    os.environ["RISK_STATE_FILE"] = str(state_csv)

    from app.governance.action_ledger import UnconfiguredActionGate
    from app.storage import risk_trace_store
    from app.storage.supplier_risk_state_store import CsvRiskStateStore
    from app.workflows import supplier_risk_agent as agent
    from scripts import c2b_analysis_kill as driver

    risk_trace_store._store = risk_trace_store.JsonlTraceStore(base / "risk_traces.jsonl")
    agent._triage_llm = FakeTriage()
    agent._risk_llm = FakeRisk()
    retriever = FakeRetriever()
    agent._get_retriever = lambda: retriever

    if os.environ.get("C2B_OWNED") == "1":
        return driver.main(argv, deps_factory=_owned_deps(base), require_bedrock=False)

    def deps():
        # The C2b local tests characterize the unfenced lifecycle's crash recovery
        # (explicit baseline); owned recovery is covered in tests/test_ownership_processes.py.
        return {"checkpointer": FileCheckpointSaver(base / "checkpoints.pkl"),
                "risk_store": CsvRiskStateStore(state_csv),
                "action_gate": UnconfiguredActionGate(),
                "unfenced_baseline": True}

    return driver.main(argv, deps_factory=deps, require_bedrock=False)


def _owned_deps(base: Path):
    from app.storage import risk_trace_store
    from app.workflows.risk_workflow import Ownership
    from tests import race_harness as h

    db = base / "owned.sqlite"
    if not db.exists():
        h.init_db(db)
        with h.connect(db) as conn:  # prior TSMC/earthquake High: the alert assesses suppressed
            conn.execute("INSERT OR REPLACE INTO supplier_state VALUES (?,?)", ("TSMC#earthquake", json.dumps(
                {"supplier": "TSMC", "risk_type": "earthquake", "current_risk_level": "High",
                 "last_headline": "earlier headline", "last_seen_at": "2026-01-01T00:00:00+00:00"})))
    risk_trace_store._store = h.SqliteTraceStore(db)
    ttl_ms = int(os.environ.get("C2B_LEASE_TTL_MS", "120000"))

    def deps():
        return {"checkpointer": h.FencedSqliteCheckpointSaver(db), "risk_store": h.SqliteRiskStateStore(db),
                "action_gate": h.SqliteActionGate(db),
                "ownership": Ownership(h.SqliteLeaseStore(db, ttl_ms), h.SqliteFinalStore(db))}
    return deps


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
