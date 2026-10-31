"""Concurrent-resume reproduction: SQLite test stores and the race child process.

Not collected by pytest. tests/test_concurrent_resume.py runs this file as
separate worker processes that share one SQLite database, coordinated by
gate files, so a chosen interleaving happens deterministically.

    python tests/race_harness.py --dir D --worker A --role resume --thread-id T \\
        --risk-level High --gates invoke,after-assess,before-finalize

Stores (test-only, one SQLite file, every operation in its own transaction):

  SqliteCheckpointSaver  mirrors DynamoCheckpointSaver: checkpoints are
                         written unconditionally (INSERT OR REPLACE), the
                         latest is the highest checkpoint_id (uuid6 order),
                         regular pending writes are first-write-wins
                         (INSERT OR IGNORE) and special writes (error,
                         interrupt, ...) overwrite (INSERT OR REPLACE).
  SqliteRiskStateStore   mirrors DynamoRiskStateStore: read, then an
                         unconditional put of the whole row.
  SqliteActionGate       mirrors ActionLedger.execute's claim rules: claim if
                         absent or DENIED -> IN_FLIGHT; COMPLETED replays the
                         stored result; IN_FLIGHT raises ActionInFlight;
                         completion requires IN_FLIGHT and the claiming
                         trace_id. Authority and policy always allow.

Differences from DynamoDB (what these tests do not show):
  - SQLite serializes writers with a database lock; DynamoDB items are
    independent and conditions are evaluated per item. Races here come only
    from the interleaving the gates force, not from storage-level timing.
  - The claim is a read-then-write inside one SQLite IMMEDIATE transaction,
    not a TransactWriteItems with ConditionChecks; the authority and policy
    tables are not modelled.
  - Reads are always strongly consistent; there are no eventually consistent
    reads, throttling, retries or partial transaction failures.
  - Item size limits, pagination and serialization to DynamoDB types are not
    exercised. Traces are JSONL lines appended by each process.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from langgraph.checkpoint.base import (  # noqa: E402
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

GATE_TIMEOUT_S = 60
EXIT_GATE_TIMEOUT = 70
EXIT_ERROR = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
  thread_id TEXT, ns TEXT, checkpoint_id TEXT, parent_id TEXT, step INTEGER, writer_trace_id TEXT,
  checkpoint_type TEXT, checkpoint BLOB, metadata_type TEXT, metadata BLOB, created_at TEXT,
  PRIMARY KEY (thread_id, ns, checkpoint_id));
CREATE TABLE IF NOT EXISTS writes (
  thread_id TEXT, ns TEXT, checkpoint_id TEXT, task_id TEXT, idx INTEGER, task_path TEXT,
  channel TEXT, value_type TEXT, value BLOB, writer_trace_id TEXT,
  PRIMARY KEY (thread_id, ns, checkpoint_id, task_id, idx));
CREATE TABLE IF NOT EXISTS write_attempts (
  thread_id TEXT, checkpoint_id TEXT, task_id TEXT, task_path TEXT, writer_trace_id TEXT,
  stored INTEGER, special INTEGER, at TEXT);
CREATE TABLE IF NOT EXISTS supplier_state (pk TEXT PRIMARY KEY, row TEXT);
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY, status TEXT, thread_id TEXT, claimed_by_trace_id TEXT,
  attempt_count INTEGER, subject TEXT, effect TEXT, created_at TEXT, completed_at TEXT);
CREATE TABLE IF NOT EXISTS claim_attempts (action_id TEXT, trace_id TEXT, outcome TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS lease (
  thread_id TEXT PRIMARY KEY, epoch INTEGER, state TEXT, owner TEXT, expires_at_ms INTEGER);
CREATE TABLE IF NOT EXISTS fenced_log (
  kind TEXT, thread_id TEXT, checkpoint_id TEXT, task_path TEXT, owner TEXT, epoch INTEGER,
  outcome TEXT, at TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


class SqliteCheckpointSaver(BaseCheckpointSaver):
    """Test-only saver with DynamoCheckpointSaver's write semantics (see module doc)."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def _conn(self):
        return connect(self.path)

    @staticmethod
    def _ns(config) -> str:
        return config["configurable"].get("checkpoint_ns", "")

    def _tuple(self, conn, row) -> CheckpointTuple:
        thread_id, ns, checkpoint_id, parent_id, ctype, cblob, mtype, mblob = row
        writes = conn.execute(
            "SELECT task_id, channel, value_type, value FROM writes "
            "WHERE thread_id=? AND ns=? AND checkpoint_id=? ORDER BY task_id, idx",
            (thread_id, ns, checkpoint_id)).fetchall()
        cfg = lambda cid: {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": cid}}  # noqa: E731
        return CheckpointTuple(
            config=cfg(checkpoint_id),
            checkpoint=self.serde.loads_typed((ctype, cblob)),
            metadata=self.serde.loads_typed((mtype, mblob)),
            pending_writes=[(t, ch, self.serde.loads_typed((vt, v))) for t, ch, vt, v in writes],
            parent_config=cfg(parent_id) if parent_id else None,
        )

    _COLUMNS = "thread_id, ns, checkpoint_id, parent_id, checkpoint_type, checkpoint, metadata_type, metadata"

    def get_tuple(self, config) -> CheckpointTuple | None:
        thread_id, ns = config["configurable"]["thread_id"], self._ns(config)
        with self._conn() as conn:
            if checkpoint_id := get_checkpoint_id(config):
                row = conn.execute(f"SELECT {self._COLUMNS} FROM checkpoints "
                                   "WHERE thread_id=? AND ns=? AND checkpoint_id=?",
                                   (thread_id, ns, checkpoint_id)).fetchone()
            else:
                row = conn.execute(f"SELECT {self._COLUMNS} FROM checkpoints WHERE thread_id=? AND ns=? "
                                   "ORDER BY checkpoint_id DESC LIMIT 1", (thread_id, ns)).fetchone()
            return self._tuple(conn, row) if row else None

    def list(self, config, *, filter=None, before=None, limit=None) -> Iterator[CheckpointTuple]:
        thread_id, ns = config["configurable"]["thread_id"], self._ns(config)
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {self._COLUMNS} FROM checkpoints WHERE thread_id=? AND ns=? "
                                "ORDER BY checkpoint_id DESC", (thread_id, ns)).fetchall()
            tuples = [self._tuple(conn, r) for r in rows]
        before_id = get_checkpoint_id(before) if before else None
        returned = 0
        for tup in tuples:
            if before_id and tup.config["configurable"]["checkpoint_id"] >= before_id:
                continue
            if filter and not all(tup.metadata.get(k) == v for k, v in filter.items()):
                continue
            if limit is not None and returned >= limit:
                return
            returned += 1
            yield tup

    def put(self, config, checkpoint, metadata, new_versions):
        thread_id, ns = config["configurable"]["thread_id"], self._ns(config)
        metadata = get_checkpoint_metadata(config, metadata)
        ctype, cblob = self.serde.dumps_typed(checkpoint)
        mtype, mblob = self.serde.dumps_typed(metadata)
        with self._conn() as conn:
            conn.execute(  # unconditional, like put_item without a condition
                "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (thread_id, ns, checkpoint["id"], config["configurable"].get("checkpoint_id"),
                 metadata.get("step"), metadata.get("trace_id"), ctype, cblob, mtype, mblob, _now()))
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

    def put_writes(self, config, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        thread_id, ns = config["configurable"]["thread_id"], self._ns(config)
        checkpoint_id = config["configurable"]["checkpoint_id"]
        writer = config["configurable"].get("trace_id")
        with self._conn() as conn:
            for i, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, i)
                vtype, vblob = self.serde.dumps_typed(value)
                verb = "INSERT OR REPLACE" if idx < 0 else "INSERT OR IGNORE"
                cur = conn.execute(f"{verb} INTO writes VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (thread_id, ns, checkpoint_id, task_id, idx, task_path, channel,
                                    vtype, vblob, writer))
                conn.execute("INSERT INTO write_attempts VALUES (?,?,?,?,?,?,?,?)",
                             (thread_id, checkpoint_id, task_id, task_path, writer, cur.rowcount, int(idx < 0), _now()))


class SqliteLeaseStore:
    """Lease rules of app.storage.thread_lease.LeaseStore, in one SQLite IMMEDIATE transaction each."""

    def __init__(self, path: Path, ttl_ms: int):
        self.path, self.ttl_ms = path, ttl_ms

    def acquire(self, thread_id: str, owner: str):
        from app.storage.thread_lease import HELD, Lease, LeaseContended

        now = int(time.time() * 1000)
        conn = connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT epoch, state, owner, expires_at_ms FROM lease WHERE thread_id=?",
                               (thread_id,)).fetchone()
            if row and row[1] == HELD and row[3] >= now:
                conn.execute("ROLLBACK")
                raise LeaseContended(thread_id, {"epoch": row[0], "owner": row[2], "expires_at_ms": row[3]})
            epoch = (row[0] if row else 0) + 1
            conn.execute("INSERT OR REPLACE INTO lease VALUES (?,?,?,?,?)",
                         (thread_id, epoch, HELD, owner, now + self.ttl_ms))
            conn.execute("COMMIT")
            return Lease(thread_id, owner, epoch, now + self.ttl_ms)
        finally:
            conn.close()

    def release(self, lease) -> bool:
        with connect(self.path) as conn:
            cur = conn.execute("UPDATE lease SET state='RELEASED' WHERE thread_id=? AND owner=? AND epoch=?",
                               (lease.thread_id, lease.owner, lease.epoch))
            return cur.rowcount == 1

    def read(self, thread_id: str) -> dict | None:
        with connect(self.path) as conn:
            row = conn.execute("SELECT epoch, state, owner, expires_at_ms FROM lease WHERE thread_id=?",
                               (thread_id,)).fetchone()
        return dict(zip(("epoch", "state", "owner", "expires_at_ms"), row)) if row else None


class FencedSqliteCheckpointSaver(SqliteCheckpointSaver):
    """Fenced writes (DynamoCheckpointSaver fenced=True): the lease check and the
    write commit together in one IMMEDIATE transaction. Retry and read-back are
    exercised against DynamoDB (moto), not here."""

    def __init__(self, path: Path):
        super().__init__(path)
        self.lost: dict = {}

    def _fenced(self, config, kind, task_path, write):
        from app.storage.thread_lease import HELD, LeaseLost, OwnershipRequired

        c = config["configurable"]
        if not c.get("lease_owner") or c.get("lease_epoch") is None:
            raise OwnershipRequired("fenced write without lease_owner/lease_epoch")
        token = (c["thread_id"], c["lease_owner"], int(c["lease_epoch"]))
        if token in self.lost:
            raise self.lost[token]
        conn = connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner, epoch, state FROM lease WHERE thread_id=?", (token[0],)).fetchone()
            owned = bool(row) and row[0] == token[1] and row[1] == token[2] and row[2] == HELD
            log = (kind, token[0], c.get("checkpoint_id"), task_path, token[1], token[2])
            if not owned:
                conn.execute("INSERT INTO fenced_log VALUES (?,?,?,?,?,?,?,?)", (*log, "rejected", _now()))
                conn.execute("COMMIT")
                self.lost[token] = LeaseLost(token[0], token[1], token[2], kind)
                raise self.lost[token]
            result = write(conn)
            conn.execute("INSERT INTO fenced_log VALUES (?,?,?,?,?,?,?,?)", (*log, "stored", _now()))
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def put(self, config, checkpoint, metadata, new_versions):
        thread_id, ns = config["configurable"]["thread_id"], self._ns(config)
        metadata = get_checkpoint_metadata(config, metadata)
        ctype, cblob = self.serde.dumps_typed(checkpoint)
        mtype, mblob = self.serde.dumps_typed(metadata)

        def write(conn):
            conn.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (thread_id, ns, checkpoint["id"], config["configurable"].get("checkpoint_id"),
                          metadata.get("step"), metadata.get("trace_id"), ctype, cblob, mtype, mblob, _now()))
        self._fenced({**config, "configurable": {**config["configurable"], "checkpoint_id": checkpoint["id"]}},
                     "checkpoint", None, write)
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

    def put_writes(self, config, writes, task_id, task_path=""):
        thread_id, ns = config["configurable"]["thread_id"], self._ns(config)
        checkpoint_id = config["configurable"]["checkpoint_id"]
        writer = config["configurable"].get("trace_id")

        def write(conn):
            for i, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, i)
                vtype, vblob = self.serde.dumps_typed(value)
                verb = "INSERT OR REPLACE" if idx < 0 else "INSERT OR IGNORE"
                cur = conn.execute(f"{verb} INTO writes VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (thread_id, ns, checkpoint_id, task_id, idx, task_path, channel, vtype, vblob,
                                    writer))
                conn.execute("INSERT INTO write_attempts VALUES (?,?,?,?,?,?,?,?)",
                             (thread_id, checkpoint_id, task_id, task_path, writer, cur.rowcount, int(idx < 0),
                              _now()))
        self._fenced(config, "writes", task_path, write)


class SqliteRiskStateStore:
    """Read, then unconditional whole-row put (DynamoRiskStateStore semantics)."""

    def __init__(self, path: Path):
        self.path = path

    def get(self, supplier: str, risk_type: str) -> dict | None:
        with connect(self.path) as conn:
            row = conn.execute("SELECT row FROM supplier_state WHERE pk=?", (f"{supplier}#{risk_type}",)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, supplier: str, risk_type: str, row: dict) -> None:
        with connect(self.path) as conn:
            conn.execute("INSERT OR REPLACE INTO supplier_state VALUES (?,?)",
                         (f"{supplier}#{risk_type}", json.dumps({**row, "supplier": supplier, "risk_type": risk_type})))

    def list_all(self) -> list[dict]:
        with connect(self.path) as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT row FROM supplier_state")]

    def flush(self) -> None:
        pass


class SqliteActionGate:
    """ActionLedger.execute claim rules (see module doc); authority and policy always allow."""

    def __init__(self, path: Path):
        self.path = path

    def _log(self, conn, action_id, trace_id, outcome):
        conn.execute("INSERT INTO claim_attempts VALUES (?,?,?,?)", (action_id, trace_id, outcome, _now()))

    @staticmethod
    def _result(row, replayed: bool) -> dict:
        return {"action_id": row["action_id"], "action_type": "open_review_ticket", "status": row["status"],
                "authority_version": None, "policy_version": None, "policy_sha256": None,
                "effect": json.loads(row["effect"]) if row["effect"] else None, "replayed": replayed}

    def execute(self, action_id: str, *, scope: str, context: dict, effect) -> dict:
        from app.governance.action_ledger import ActionInFlight

        trace_id = context["trace_id"]
        conn = connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
            if row is None or row["status"] == "DENIED":
                conn.execute(
                    "INSERT OR REPLACE INTO actions VALUES (?,?,?,?,?,?,?,?,?)",
                    (action_id, "IN_FLIGHT", context["thread_id"], trace_id,
                     (row["attempt_count"] if row else 0) + 1, json.dumps(context.get("subject") or {}),
                     None, _now(), None))
                self._log(conn, action_id, trace_id, "claimed")
                conn.execute("COMMIT")
            elif row["status"] == "COMPLETED":
                self._log(conn, action_id, trace_id, "replayed")
                conn.execute("COMMIT")
                return self._result(row, replayed=True)
            else:
                self._log(conn, action_id, trace_id, "in_flight")
                conn.execute("COMMIT")
                raise ActionInFlight(f"action {action_id} is IN_FLIGHT from trace {row['claimed_by_trace_id']}; "
                                     "reconciliation required")
        finally:
            conn.close()

        result = effect()
        with connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "UPDATE actions SET status='COMPLETED', effect=?, completed_at=? "
                "WHERE action_id=? AND status='IN_FLIGHT' AND claimed_by_trace_id=?",
                (json.dumps(result, default=str), _now(), action_id, trace_id))
            if cur.rowcount != 1:
                raise RuntimeError(f"completion of {action_id} rejected: not IN_FLIGHT for {trace_id}")
            row = conn.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
        return self._result(row, replayed=False)


# ---- Child process ----

RISK_ALERT = {"status": "ok", "supplier": "TSMC", "risk_type": "earthquake", "risk_level": "High",
              "impact": "Wafer output delayed", "recommended_action": "Contact supplier",
              "relevant_supplier_context": "Fabs in Taiwan"}


def run_child(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--role", choices=("start", "resume"), required=True)
    parser.add_argument("--thread-id")
    parser.add_argument("--headline", default="TSMC halts production after earthquake in Taiwan")
    parser.add_argument("--risk-level", default="High")
    parser.add_argument("--pause-before", default="")
    parser.add_argument("--gates", default="")
    # Ownership mode: acquire the thread's lease first (harness-side; the
    # lifecycle does not acquire yet) and run with the fenced saver.
    parser.add_argument("--ownership", action="store_true")
    parser.add_argument("--lease-ttl-ms", type=int, default=120_000)
    parser.add_argument("--fail-analyze", action="store_true", help="raise inside analyze after its gates")
    args = parser.parse_args(argv)

    os.environ["RISK_STATE_BACKEND"] = "csv"  # unused: stores are injected
    os.environ["TRACE_STORE_BACKEND"] = "jsonl"
    from app.storage import risk_trace_store
    from app.workflows import risk_workflow
    from app.workflows import supplier_risk_agent as agent
    from scripts.c2b_analysis_kill import EventLog

    base, worker = args.dir, args.worker
    gates = {g for g in args.gates.split(",") if g}
    log = EventLog(base / "events.jsonl", attempt=worker)
    risk_trace_store._store = risk_trace_store.JsonlTraceStore(base / "risk_traces.jsonl")
    db = base / "race.sqlite"

    def gate(point: str) -> None:
        if point not in gates:
            return
        log.emit("gate_reached", point=point)
        (base / f"at-{worker}-{point}").touch()
        go, deadline = base / f"go-{worker}-{point}", time.monotonic() + GATE_TIMEOUT_S
        while not go.exists():
            if time.monotonic() > deadline:
                log.emit("gate_timeout", point=point)
                os._exit(EXIT_GATE_TIMEOUT)
            time.sleep(0.01)
        log.emit("gate_released", point=point)

    # Fake models: inference is counted from these events.
    class Triage:
        def invoke(self, prompt):
            log.emit("triage_invoke")
            return "retrieve"

    class Risk:
        def invoke(self, prompt):
            log.emit("model_invoke", risk_level=args.risk_level)
            return json.dumps({**RISK_ALERT, "risk_level": args.risk_level})

    def failing_analyze_point():
        if args.fail_analyze:
            log.emit("analyze_raising")
            raise RuntimeError("injected analyze failure")

    retriever = SimpleNamespace(retriever_id="fake", retriever_version="v0", embedding_provider="fake", top_k=2,
                                retrieve=lambda q, s: SimpleNamespace(context="TSMC fabs are in Taiwan."))
    agent._triage_llm, agent._risk_llm = Triage(), Risk()
    agent._get_retriever = lambda: retriever

    # Probes and gates (all experiment-side; the lifecycle code is unchanged).
    original_build_app = risk_workflow.build_app

    class GatedApp:
        def __init__(self, app):
            self._app = app

        def __getattr__(self, name):
            return getattr(self._app, name)

        def invoke(self, *a, **k):
            log.emit("invoke_enter")  # resume_workflow has already decided the thread is resumable
            gate("invoke")
            return self._app.invoke(*a, **k)

    risk_workflow.build_app = lambda *a, **k: GatedApp(original_build_app(*a, **k))

    original_analyze = agent.analyze_risk

    def analyze(state, config):
        # The checkpoint this worker is executing from (LangGraph loaded it inside invoke).
        log.emit("analyze_enter", trace_id=config["configurable"].get("trace_id"),
                 checkpoint_id=config["configurable"].get("checkpoint_id"))
        gate("analyze-enter")
        result = original_analyze(state)
        log.emit("analyze_return", risk_level=result["alert"].get("risk_level"))
        gate("after-analyze")
        failing_analyze_point()
        return result

    agent.analyze_risk = analyze

    original_make_assess = agent.make_assess_node

    def make_assess(reader):
        inner = original_make_assess(reader)

        def assess(state):
            result = inner(state)
            log.emit("assess_return", **{k: result["assessment"].get(k)
                                          for k in ("eligibility_change_type", "action_eligible", "prior_risk_level")})
            gate("after-assess")
            return result
        return assess

    agent.make_assess_node = make_assess

    original_ticket = agent.open_review_ticket

    def ticket(alert, headline, action_id):
        result = original_ticket(alert, headline, action_id)
        log.emit("effect_executed", ticket_id=result["ticket_id"], risk_level=alert.get("risk_level"))
        gate("inside-effect")
        return result

    agent.open_review_ticket = ticket

    original_finalize = risk_workflow.finalize_completed_run

    def finalize(*a, **k):
        gate("before-finalize")
        log.emit("finalize_enter")
        enriched = original_finalize(*a, **k)
        log.emit("finalize_return", change_type=enriched["change_type"], risk_level=enriched["risk_level"])
        return enriched

    risk_workflow.finalize_completed_run = finalize

    saver = FencedSqliteCheckpointSaver(db) if args.ownership else SqliteCheckpointSaver(db)
    deps = {"checkpointer": saver, "risk_store": SqliteRiskStateStore(db), "action_gate": SqliteActionGate(db)}
    log.emit("attempt_start", role=args.role, thread_id=args.thread_id, ownership=args.ownership)

    lease = None
    if args.ownership:
        from app.storage.thread_lease import LeaseContended, LeaseLost
        import uuid as _uuid

        leases = SqliteLeaseStore(db, args.lease_ttl_ms)
        gate("before-acquire")
        try:
            lease = leases.acquire(args.thread_id, f"owner-{worker}-{_uuid.uuid4().hex[:8]}")
        except LeaseContended as exc:
            log.emit("contended", holder_epoch=exc.holder.get("epoch"), holder=exc.holder.get("owner"))
            return 5
        log.emit("acquired", epoch=lease.epoch, owner=lease.owner)
        original_config = risk_workflow._config

        def owned_config(thread_id, trace_id=None):
            cfg = original_config(thread_id, trace_id)
            cfg["configurable"].update(lease_owner=lease.owner, lease_epoch=lease.epoch)
            return cfg

        risk_workflow._config = owned_config

        # Hold point after analyze's writes are stored, before the next checkpoint
        # (the main thread waits on that put). Recognized by checkpoint contents.
        original_put = saver.put

        def put(config, checkpoint, metadata, new_versions):
            steps = checkpoint["channel_values"].get("trace_steps") or []
            if steps and steps[-1]["node_name"] == "analyze":
                gate("after-analyze-writes")
            return original_put(config, checkpoint, metadata, new_versions)

        saver.put = put
    try:
        if args.role == "start":
            outcome = risk_workflow.start_workflow(
                {"headline": args.headline},
                pause_before=[args.pause_before] if args.pause_before else None, **deps)
        else:
            outcome = risk_workflow.resume_workflow(args.thread_id, **deps)
    except Exception as exc:  # report, then fail the process as an unhandled error would
        log.emit("attempt_error", error_type=type(exc).__name__, error=str(exc)[:300],
                 ownership_lost=[list(k) for k in getattr(saver, "lost", {})])
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if args.ownership and type(exc).__name__ == "LeaseLost":
            return 6
        return EXIT_ERROR
    if lease is not None:
        log.emit("released", ok=leases.release(lease), epoch=lease.epoch)
    log.emit("attempt_exit", **{k: outcome.get(k) for k in (
        "status", "thread_id", "trace_id", "next", "change_type", "state_written", "risk_level",
        "eligibility_change_type", "action_eligible", "action_id", "action_status")})
    print(json.dumps({k: outcome.get(k) for k in ("status", "thread_id", "trace_id")}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(run_child(sys.argv[1:]))
