# Same-Thread Ownership: Design (Draft)

**Status:** draft for review. Nothing here is implemented. No IAM, schema or infrastructure change has been made.
**Baseline:** `assess-before-act` at `2517e9a`. The races this design addresses are reproduced in `tests/test_concurrent_resume.py`; see "Concurrent resume reproduction (local)" in `docs/Architecture.md`.

**Goal:** at most one owner executes and mutates a checkpointed thread at a time. A stale owner cannot change durable workflow state after another owner takes over.

**Outside this milestone:**
- **X1: cross-thread business deduplication.** Two threads for the same business change can each open a ticket, and the supplier-state row has no version check.
- **External `IN_FLIGHT` reconciliation.** Determining whether an admitted effect happened when its executor died or lost ownership mid-effect.

---

## 1. Model

**Ownership token.** Each execution attempt carries `(thread_id, owner, epoch)`, where `owner` is the attempt's `trace_id`.
- The token travels in `configurable` as `lease_owner` and `lease_epoch`, alongside `trace_id`.
- The saver and the action gate read it from the config of every write.
- LangGraph copies string and integer `configurable` keys into each checkpoint's metadata, so every checkpoint records the owner and epoch that wrote it.

**Two new item types in `risk_agent_checkpoints`.** The partition key is `thread_id`. Neither sort key matches the `chk#` or `wr#` prefixes the saver queries.

| `sk` | Fields | Lifetime |
|---|---|---|
| `lease` | `epoch` (N), `state` (`HELD` / `RELEASED`), `owner`, `owner_executor`, `expires_at_ms` (N), `acquired_at`, `renewed_at`, `released_at`, `adopted_legacy` (bool) | Created on first acquisition. **Never deleted.** `delete_thread` must skip it, so an epoch is never reused |
| `final` | `kind` (`finalized` / `legacy_recognized`), `trace_id`, `epoch`, `change_type`, `eligibility_change_type`, `final_checkpoint_id`, `action_id`, `action_status`, `risk_level`, `source_trace_id` (legacy only), `finalized_at` | Written once (`attribute_not_exists`) |

**Ownership rules:**
- **Acquisition requires** the lease to be absent, `RELEASED`, or expired (`expires_at_ms < :now`). Acquisition always increments `epoch`.
- **Fenced writes require** `owner = :me AND epoch = :epoch AND state = HELD`. **Expiry is not part of the write condition.**
  - DynamoDB conditions have no server clock. `:now` is the writer's clock, so testing expiry on writes would turn clock skew into self-rejection.
  - Fencing is what revokes ownership: once another worker acquires, the epoch has changed and every write by the old owner fails.
- **An expired owner** may keep writing, and may renew, only while the epoch is unchanged, i.e. while nobody has taken over. Such a renewal is recorded as `renewed_after_expiry`. After a takeover, renewal and writes fail.
- **Clock skew** can cause an early takeover. That costs wasted work (inference is at least once), never a second durable mutation.

## 2. Ownership lifecycle operations

Notation:
- `CC(lease)` is a ConditionCheck on the `lease` item with `#owner = :me AND epoch = :epoch AND #state = :held`.
- **Txn** is `TransactWriteItems`. IAM has no TransactWriteItems action; each item in a transaction needs its own per-item permission.

| Operation | Condition | Boundary | Failure → result |
|---|---|---|---|
| **Acquire on START** (new thread) | `attribute_not_exists(sk)`; `SET … epoch = 1, state = HELD` | `UpdateItem` | Condition fails → the existing thread-collision error |
| **Acquire on RESUME** | `attribute_not_exists(sk) OR #state = :released OR expires_at_ms < :now`; `SET owner, owner_executor, state = HELD, expires_at_ms = :now + :ttl, acquired_at ADD epoch :one`; `ReturnValues = ALL_NEW` | `UpdateItem`, **before** any workflow state is read | Condition fails → read the lease → **`contended`**, reporting the holder, its trace and `expires_at`. Nothing is executed or written. A missing lease item on an existing thread creates it with `adopted_legacy = true` (§6) |
| **Renew** (heartbeat every `ttl/3`) | `#owner = :me AND epoch = :epoch AND #state = :held`; `SET expires_at_ms, renewed_at` | `UpdateItem` | Fails → set the local `lost` flag (§8) |
| **Release** | `#owner = :me AND epoch = :epoch`; `SET state = RELEASED, released_at`. **Epoch and item retained** | `UpdateItem`, last step of shutdown (§8) | Fails → `release_failed` on the outcome; the lease expires on its own |
| **Takeover** | Acquire on RESUME succeeds against an expired lease | as Acquire | The new owner proceeds per §7. The old owner's next fenced write fails |

## 3. Fenced workflow mutations

| Mutation | Condition | Boundary | Failure → result |
|---|---|---|---|
| **Checkpoint `put`** | `CC(lease)` + `Put chk#{ns}#{id}` with `attribute_not_exists(sk)` | Txn, 2 items (checkpoint ≤ 350 KB) | Lease → **`LeaseLost`**; the main thread waits on this put, so the attempt stops at that step. Data condition → §5 |
| **`put_writes`**: all writes of one task, regular **and** special (`__error__`, `__interrupt__`, `__scheduled__`, `__resume__`) | `CC(lease)` + one `Put wr#…` per write: regular writes `attribute_not_exists(sk)`, special writes unconditional | **One txn per task.** More than 99 writes → refuse; never split. A partial batch would be reapplied as the node's result | Lease → **`LeaseLost`**, nothing stored. Data condition → §5 |
| **Action admission** | Authority and policy ConditionChecks (existing) + `CC(lease)` + `Update action` (absent or `DENIED` → `IN_FLIGHT`, `claimed_epoch`) | The existing cross-table txn, plus 1 item | Lease → **`LeaseLost`**. Authority or policy → `authority_denied`. `IN_FLIGHT` (any epoch) → **`action_in_flight`**, an outcome, not an exception |
| **Action replay** (`COMPLETED`) | `CC(lease)` + `Update action SET last_replayed_by, replay_epoch` with `#status = :completed` | Txn, replacing today's unconditional read | Lease → **`LeaseLost`**. A non-owner can no longer complete by replaying |
| **Denial record** | `CC(lease)` + `Update action → DENIED` (existing condition) | Txn | Lease → `LeaseLost`; nothing recorded |
| **Action completion** | Unchanged: `#status = :in_flight AND claimed_by_trace_id = :me`. **No lease check** | `UpdateItem` | Records an effect that already ran (§4) |
| **Finalization** | Consistent read of the prior row, classify, then `CC(lease)` + `Put supplier row` (unconditional; versioning is X1) + `Put final` with `attribute_not_exists(sk)` | Txn across 2 tables, 3 items | Lease → **`lease_lost`**, nothing written. `final` exists → **`already_finalized`**, reporting the marker's trace and epoch |
| **Completion trace** | `Put trace` with `attribute_not_exists(trace_id)` | `PutItem`, after finalization | Exists → §5 (verify). A failure does not undo finalization; recovery repairs it (§7) |

**Fail closed.** A `DynamoCheckpointSaver` write without an ownership token in `configurable` is refused. The C2b `--direct-invoke` diagnostic must acquire a lease or stay on local test savers. The test savers (InMemory, File, SQLite) stay unfenced unless they implement these conditions.

## 4. External-effect guarantees (narrowed)

- **Ownership fences admission and durable workflow mutations:** checkpoints, every pending write, action admission, replay and denial records, finalization, and the `final` marker.
- **It does not stop an effect that was already admitted.** If an owner loses its lease while its effect runs, the effect may continue and complete after a takeover, and its completion is recorded by claim (`claimed_by_trace_id`) without a lease check. Revocation is prospective, as with authority (Phase 3D).
- **A new owner that finds the action `IN_FLIGHT`** reports `action_in_flight` and stops. Whether that effect happened is unresolved; reconciliation is outside this milestone.
- At most one admission per `action_id` remains the ledger's guarantee. Exactly-once external effects are not claimed.

## 5. Retry and idempotency after an unknown outcome

**Error classes.** These are evaluated in this order. A `TransactionCanceledException` carries per-item `CancellationReasons`; data items are sent with `ReturnValuesOnConditionCheckFailure = ALL_OLD`, so the existing item comes back without an extra read.

| Class | Signals | Handling |
|---|---|---|
| **Lost ownership** | Cancellation reason `ConditionalCheckFailed` on the `lease` item | Raise `LeaseLost`. **No retry** |
| **Data conflict** | `ConditionalCheckFailed` on a data item (`chk#`, `wr#`, `final`, trace) | **Verify, don't assume** (rules below): identical means already applied; different means a conflict error, no retry |
| **Transaction conflict** | Reason `TransactionConflict`; `TransactionInProgressException` | Retry the **same request with the same `ClientRequestToken`**, bounded exponential backoff |
| **Throttling** | `ProvisionedThroughputExceededException`, `ThrottlingException`, reason `ThrottlingError`, `RequestLimitExceeded` | As transaction conflict. When retries are exhausted: unknown outcome |
| **Unknown outcome** | Timeouts, connection reset, `InternalServerError`, `ServiceUnavailable`, retries exhausted | Retry with the same token while inside DynamoDB's 10-minute idempotency window. Otherwise, or if still unknown: **read back and verify** |
| **Request bug** | `IdempotentParameterMismatchException`, `ValidationException`, item size | Fail, no retry |

**Tokens.** `ClientRequestToken` is deterministic per logical write: a hash of thread, operation, and checkpoint or task identity, ≤ 36 characters. A retry **reuses the exact serialized payload** from the first attempt (serialize once), so byte comparison on read-back is meaningful.

**Rules for each write:**
- **Checkpoint.** After an unknown outcome or a `chk#` condition failure, read the item. Stored checkpoint bytes, metadata bytes and parent ID equal to ours → **success**: our earlier attempt applied it. **An identical retry is not a bug.** Any difference → `CheckpointConflict`. Absent → re-check the lease (lost → `LeaseLost`), then retry.
- **Pending writes.** Read **every** `wr#` item for `(checkpoint_id, task_id)` and compare with the **complete intended batch**: the same `idx` set, channels, value types and value bytes, special writes included.
  - Exactly equal → **success**.
  - None present → re-check the lease, then retry.
  - A **partial** batch, or any differing item → `PendingWriteConflict`. A transaction can't leave a partial batch, so this means a protocol violation, not a retryable state.
- **Finalization.** Read `final`.
  - Present with our `trace_id` and epoch → **success**. The supplier row was in the same transaction, so it needn't be checked.
  - Present with another trace → `already_finalized`.
  - Absent and the lease is ours → retry with the same token and payload inside the window. Beyond the window → re-read the prior row, reclassify, and submit a new transaction.
  - Absent and the lease is lost → `LeaseLost`.
- **Completion trace.** On a condition failure or an unknown outcome, read the trace by `trace_id`. The same `thread_id`, `workflow_event`, `change_type` and `lease_epoch` → **success**. Different → record `trace_conflict` on the outcome. Finalization stands, and nothing is overwritten.
- **Lease operations** have no token, so read back:
  - **Acquire, unknown:** `owner = me` → held at the returned epoch; otherwise re-evaluate.
  - **Renew, unknown:** `owner = me` and `epoch = mine` → held.
  - **Release, unknown:** `RELEASED` by me, or another owner → done.
- **Action operations, unknown:**
  - **Admission:** read the action. `IN_FLIGHT` with `claimed_by_trace_id = me` → admitted. Otherwise evaluate as a fresh admission.
  - **Completion:** read the action. `COMPLETED` by me → done.

## 6. Compatibility: threads created before ownership

**Protocol threads and legacy threads.** A checkpoint written under this protocol carries `lease_epoch` in its metadata (§1). That gives a crisp rule:
- **Recovery finalization** (§7) applies only when the **final checkpoint was written by the protocol**, i.e. its metadata has `lease_epoch`.
- A **completed thread whose final checkpoint lacks `lease_epoch`** is **legacy**. It is **never finalized again automatically**.

**Legacy completed thread** (no pending tasks, no `final` marker, final checkpoint without `lease_epoch`). Acquisition adopts it (creates the lease with `adopted_legacy = true`) and then evaluates the historical evidence. **All** of the following are required:

| # | Evidence | Source |
|---|---|---|
| E1 | The latest checkpoint has no pending tasks (`snapshot.tasks == ()`) | Checkpoint query |
| E2 | Linear history: no two `chk#` items share a parent | Query of the thread's `chk#` items |
| E3 | The completing attempt is identified: the final checkpoint's metadata `trace_id` (written since `25a7193`), else the final state's `trace_id` (older threads, stamped by `update_state`) | Final checkpoint |
| E4 | A completion trace exists for that `trace_id`, with matching `thread_id`, `workflow_event` in {`started_completed`, `resumed_completed`}, and a non-null `change_type` | Trace `GetItem` by `trace_id` |

- **All present → recognized.** Write `final` with `kind = legacy_recognized`, `source_trace_id`, the `change_type` from the trace, and the current epoch. Use the same fenced, conditional transaction as finalization, but **without a supplier-state write and without a new trace**. Outcome `already_completed` (`legacy_recognized = true`).
- **Missing or ambiguous** (no trace for E3's ID; a fork under E2; no identifiable trace ID; or a mismatched thread, event, or null change type):
  - **No** supplier-state write, **no** marker, **no** trace rewrite.
  - Outcome **`completion_unverified`**, listing which evidence failed. Worker exit 7; Lambda 409.
  - The lease is released. The thread is re-evaluated, and reported the same way, on every RESUME until an operator resolves it. An operator resolution command is a later milestone.
- **Why not finalize when evidence is missing:** before this protocol, a crash between END and the state write lost the write, and a crash after it left no trace. The two cases can't be told apart from the trace store, and finalizing could apply a business change twice. The supplier row isn't used as evidence, because other threads overwrite it.
- **Expected scope:** runs whose traces went only to local JSONL (the default before DynamoDB traces) report `completion_unverified`. That is the intended conservative result.

**Legacy paused or parked threads** (pending tasks remain) are adopted and continue normally under the lease.
- Their pending writes were stored without fencing. They're reused as today, and the attempt's trace carries `legacy_pending_writes = true`.
- Threads parked at `act` with no assessment keep the existing `legacy_unassessed` behavior.

## 7. Takeover and recovery after acquisition

| Durable state | Action | Outcome |
|---|---|---|
| Pending tasks | `invoke(None)`. Pending writes from an earlier epoch were fenced when written and are reused (C3) | as today |
| No pending tasks; no `final`; final checkpoint **has** `lease_epoch` | **Recovery finalization** under the new epoch from the final checkpoint: `act` is not re-run, and its stored `action_result` is used | `completed`, `recovered_finalization = true` |
| No pending tasks; no `final`; final checkpoint **lacks** `lease_epoch` | Legacy rule (§6) | `already_completed` (recognized) or `completion_unverified` |
| `final` present; completion trace for `final.trace_id` absent (`kind = finalized`) | Write that trace with **the original `trace_id` and epoch**, built from `final` and the final checkpoint, marked `recovered_by = <this trace>`; idempotent (§5) | `already_completed`, `trace_recovered = true` |
| `final` present; trace present | Nothing | `already_completed`, reporting `final.trace_id`, `final.epoch`, `final.change_type` |
| Action `IN_FLIGHT` from any epoch | Stop | `action_in_flight` (§4) |

## 8. Shutdown ordering and cleanup

**Normal completion or pause.** The steps run in this order:
1. **Drain checkpoint writers.** `invoke()` has returned. The saver keeps a per-attempt count of in-flight writes and their errors; wait for zero with a deadline. An error recorded by a background writer is raised now and handled below.
   - **To verify:** whether LangGraph 1.2.2 joins its background writer before `invoke()` returns. The C2b probe suggests it does. The drain step is required either way, because the lifecycle must not rely on it.
2. **Settle.** If paused: write the paused trace. If completed: finalization (§3, §5), then the completion trace.
3. **Stop the heartbeat**, then join it with a deadline. It stays alive through finalization and the trace, so the lease can't expire mid-finalization.
4. **Release conditionally** (§2).
5. **Emit the outcome.** Its lineage is described in §9.

**On exceptions:**

| Cause | Writers | Finalization | Trace | Heartbeat | Release | Outcome |
|---|---|---|---|---|---|---|
| `LeaseLost` (any fenced write, or found when draining) | The saver enters a local `lost` state and refuses further writes; drain outstanding writes, which fail fenced | Skipped | `lease_lost` trace: an unfenced audit record, conditional on `trace_id` | Stop and join | **Skipped:** no longer the owner | `lease_lost` |
| `AuthorityDenied` | Drain | Skipped | Denial trace (existing) | Stop and join | Release | `authority_denied` |
| `ActionInFlight` | Drain | Skipped | `action_in_flight` trace | Stop and join | Release | `action_in_flight` |
| Data conflict, retries exhausted, unknown outcome not resolved, other errors | Drain | Skipped, or left unknown | Best-effort `attempt_failed` trace | Stop and join | Release: safe, because finalization is atomic and its marker conditional, so the next owner re-evaluates from durable state | `attempt_failed` |
| SIGTERM or interrupt | Best-effort drain within a deadline | Skipped if not started | Best-effort | Stop | Best-effort release | — |
| SIGKILL or crash | — | — | — | — | None; the lease expires | Recovered by the next owner (§7) |

**Heartbeat failure** only sets `lost`. Nodes check it before starting a model call (an optional cost saving). Safety comes from the next fenced write failing.

## 9. Keeping outcomes tied to the execution lineage

- Each attempt records **its own** last checkpoint ID, as returned by its fenced puts. `_settle` reads `get_state` at that checkpoint ID, never the thread's latest. This removes the misattribution seen in the race harness, where a worker reported another worker's paused state as its own.
- `already_completed`, recovery and legacy recognition report from the `final` marker: who finalized, at which epoch, with which `change_type`.
- Trace records add `lease_epoch` and `lineage_checkpoint_id`. Checkpoint metadata carries the owner and epoch.
- **New `workflow_event` values:** `lease_lost`, `action_in_flight`, `attempt_failed` and `completion_unverified`. They are added to `INCOMPLETE_WORKFLOW_EVENTS`. `contended` writes no trace.

| Outcome | Worker exit | Lambda |
|---|---|---|
| `contended` | 5 | 409 |
| `lease_lost`, `action_in_flight` | 6 | 409 |
| `completion_unverified` | 7 | 409 |

## 10. Complete operation and permission inventory

These permissions are what each executor (Lambda, container worker) would need. **No IAM change has been made.**

| # | Operation | API | Table / key | Lambda role today | Governed worker today |
|---|---|---|---|---|---|
| 1 | Acquire, renew, release | `UpdateItem` | checkpoints / `sk = lease` | missing | missing |
| 2 | Inspect lease (contention report, unknown-outcome read-back) | `Query` (or `GetItem`) | checkpoints / `lease` | `Query` ✓ | missing |
| 3 | Read checkpoints and pending writes | `Query` | checkpoints / `chk#`, `wr#` | ✓ | missing |
| 4 | Checkpoint put (fenced) | `ConditionCheckItem` + `PutItem` | checkpoints / `lease`, `chk#` | `PutItem` ✓, CC missing | missing |
| 5 | Pending writes (fenced) | `ConditionCheckItem` + `PutItem` | checkpoints / `lease`, `wr#` | `PutItem` ✓, CC missing | missing |
| 6 | Verify checkpoint or pending writes after an unknown outcome | `Query` | checkpoints | ✓ | missing |
| 7 | Read `final` marker; E1–E3 legacy evidence | `Query` (or `GetItem`) | checkpoints / `final`, `chk#` | ✓ | missing |
| 8 | Write `final` (finalization, legacy recognition) | `PutItem` + `ConditionCheckItem` | checkpoints / `final`, `lease` | `PutItem` ✓, CC missing | missing |
| 9 | Supplier comparison read | `GetItem` (consistent) | supplier state | ✓ | missing |
| 10 | Supplier write (in the finalization txn) | `PutItem` | supplier state | ✓ | missing |
| 11 | Authority and policy read before admission | `GetItem` | authority / `agent#…`, `policy#…` | missing | ✓ |
| 12 | Admission, denial (fenced) | `ConditionCheckItem` (authority, checkpoints `lease`) + `UpdateItem` (actions) | authority, checkpoints, actions / `action#thread-*` | missing | authority ✓; checkpoints missing; actions only `action#probe-*` |
| 13 | Replay read and fenced replay record | `GetItem` + `UpdateItem` + `ConditionCheckItem` | actions, checkpoints | missing | probe keys only; CC missing |
| 14 | Completion | `UpdateItem` | actions | missing | probe keys only |
| 15 | Action read-back after an unknown outcome | `GetItem` | actions | missing | probe keys only |
| 16 | Completion, paused and denial traces (conditional put) | `PutItem` | traces | ✓ | missing |
| 17 | Trace read: recovery (§7), legacy E4 (§6), verify after unknown (§5) | `GetItem` | traces / `trace_id` | ✓ | missing |
| 18 | Model inference (unchanged) | `bedrock:InvokeModel` | — | Titan only | Claude ✓ |

`ClientRequestToken` and `ReturnValuesOnConditionCheckFailure` need no extra permission. Every step and every task's writes become transactions, which doubles their write units (about 80 writes per run today).

## 11. Mapping to the reproduced races

| Race / requirement (strict xfail in `2517e9a`) | Protection | Expected after |
|---|---|---|
| R1: single execution | Acquire on RESUME | B is `contended`: 0 inferences, no writes |
| R1: no checkpoint fork | Acquire, plus fenced `put` and `put_writes` | One writer per epoch. Stale writes are rejected |
| R2: contention is an outcome | Acquire, plus the `action_in_flight` outcome | B is `contended`. A new owner meeting `IN_FLIGHT` gets exit 6, no exception |
| R3: action and business state agree | Fenced admission, replay and finalization | Only the owner's analysis acts and finalizes |
| R4: single finalization | `final` marker in the finalization txn | The second finalization is `already_finalized` |
| X1: one ticket per business change | **Out of scope** | Remains a strict xfail |
| Pending writes deciding recovery (C3, R1) | Fenced `put_writes`, whole batch | Only writes stored under a valid lease are reused |
| Stale error and interrupt writes (R2) | Special writes inside the fenced txn | Rejected |

## 12. Tests to add before or with the implementation

The SQLite stand-ins get extended with `lease` and `final` and these conditions, with injected `now()` and TTL. There are also moto tests for the DynamoDB implementation, and subprocess SIGKILL tests for the crash cases.

- **Converted:** the R1–R4 strict xfails become passing regressions, and their observed-behavior tests are replaced by protected outcomes. X1 is unchanged.
- **Stale owner** (A loses the lease to a forced takeover, then is released):

| Test | Stale owner A | Expect |
|---|---|---|
| S1 | is mid-task | `put_writes` rejected; nothing stored |
| S2 | has stored its writes, before `put` | `put` rejected; no fork |
| S3 | writes an `__error__` | rejected |
| S4 | attempts admission | rejected; no `IN_FLIGHT` |
| S5 | attempts replay and finalization after B finished | rejected; one marker |
| S6 | — | heartbeat keeps B `contended`; after a takeover, renewal fails and sets `lost` |
| S7 | is expired but not taken over | may renew and write (`renewed_after_expiry`) |
| S8 | — | epochs 1, 2, 3 across START and two resumes; the lease survives `delete_thread` |

- **Crash and takeover:**

| Test | Kill point | Expect |
|---|---|---|
| K1 | mid-`analyze` | the analysis is re-run |
| K2 | after analyze's writes are stored | reused, not re-run |
| K3 | after END, before finalization | recovery finalization; `act` not re-run |
| K4 | after finalization, before the trace | trace recovered with the original `trace_id`; no second state write |
| K5 | `IN_FLIGHT` | `action_in_flight` |
| K6 | — | contention reporting (exit 5 / 409) |

- **Retry:** each write is put into the unknown-outcome state by a fault-injecting client, which applies the write, then raises a timeout.
  - An identical checkpoint retry → success.
  - A complete identical pending-write batch → success. Partial or different → `PendingWriteConflict`.
  - Finalization verified by the marker.
  - The trace verified, or `trace_conflict`.
  - `TransactionConflict` and throttling are retried with the same token. A lease failure is not retried.
- **Compatibility:** legacy completed threads:
  - all evidence present → `legacy_recognized`, no state write;
  - no trace → `completion_unverified`, no writes;
  - a forked legacy history → `completion_unverified`;
  - a legacy paused thread → adopted and completed;
  - a legacy thread parked at `act` → `legacy_unassessed`.
- **Shutdown:**
  - Lease release happens after finalization and the trace, and after the heartbeat is joined.
  - A drain error surfaces before settling.
  - `LeaseLost` skips release.
  - SIGTERM releases on a best-effort basis.

## 13. Open questions and limits

- Whether LangGraph 1.2.2 joins its background writers before `invoke()` returns (§8, step 1).
- The unexplained late-load behavior recorded in `2517e9a`. It should be re-run under the protocol, where the late worker would be `contended`.
- TTL choice (proposed 120 s) against the longest step, and against Lambda's timeout.
- **Approval required before implementation:** the schema (two new item types), IAM (§10), and the transaction cost.
- **Out of scope here:** X1 and `IN_FLIGHT` reconciliation; operator resolution of `completion_unverified`; fencing of audit traces.
