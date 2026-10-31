# Same-Thread Ownership: AWS Rollout Plan (for review)

**Status:** step 1 (read-only preflight and plan) and experiment A are done; see [Results](#9-results). Still not done:
- No Terraform has been applied.
- Nothing has been deployed or adopted.
- The Lambda has not been invoked, and no historical thread has been resumed.

Design: `docs/SAME_THREAD_OWNERSHIP_DESIGN.md` (§16 lists the requirements). Code: branch `assess-before-act`.

## 1. Executors and what could not be inspected

| Executor | What was inspected | Result |
|---|---|---|
| Lambda `ai-supply-chain-risk-agent` | Terraform: image by digest (`var.risk_agent_image_digest`), timeout 123 s, memory 1028 MB, role `RiskAgentLambdaRole` | Env sets `RISK_STATE_BACKEND` and `TRACE_STORE_BACKEND` to `dynamodb`, plus `CHECKPOINT_TABLE`. **No `AUTHORITY_TABLE` / `ACTIONS_TABLE`**, so governed actions are denied (fail closed). The deployed image predates Phase 3D (Architecture.md) and therefore predates ownership |
| Lambda triggers | Terraform | **None defined in Terraform.** Any EventBridge rule, Scheduler schedule, function URL or async configuration was created outside Terraform and is only visible to the live preflight |
| Local Docker workers | `docker ps` on this machine | Docker daemon not running: no containers |
| Kubernetes workers | `kubectl` / `kind` on this machine | No kube contexts configured, and no kind clusters (needs Docker) |
| Local worker processes | process list on this machine | None running (`risk_worker`, drivers, harnesses) |

**Not inspectable from here:**
- **All live AWS state.** The `ken-ai-dev` SSO session had expired at the time of this review. That covers deployed digest and environment, triggers, roles, tables and threads.
- **Other machines,** and any Kubernetes cluster not configured locally.
- **Whether anyone else runs workers** against the dev tables.

**Live read-only inspection, after `aws sso login --profile ken-ai-dev`:**
```bash
AWS_PROFILE=ken-ai-dev AWS_DEFAULT_REGION=us-west-2 \
  .venv/bin/python scripts/ownership_preflight.py --out ~/ownership-runs/preflight-$(date -u +%Y%m%dT%H%M%SZ).json
```
`scripts/ownership_preflight.py` refuses, inside the process, every AWS operation that isn't a read (`Get*`, `List*`, `Describe*`, `Query`, `Scan`, `BatchGet*`, `TransactGet*`); a test covers this. It reports:
- **Executor:** digest, environment (secrets redacted), role and its inline policies, reserved concurrency, function URL, event source mappings, async configuration, EventBridge rules and Scheduler schedules that target the function.
- **Threads:** the per-thread inventory below.

## 2. Legacy thread inventory

The preflight scans the checkpoint table (`thread_id`, `sk` only), reads each thread's state, and classifies it **without acquiring a lease or writing a marker**. It uses the lifecycle's own read-only evidence function, `risk_workflow.legacy_completion_evidence`:

| Classification | Expected handling on the first owned RESUME |
|---|---|
| `adopt_paused` | Lease created at epoch 1 and the thread continues |
| `adopt_parked_legacy` | Parked at `act` with no assessment: continues as `legacy_unassessed` |
| `action_in_flight` | The action is `IN_FLIGHT`: RESUME reports `action_in_flight`; reconciliation is out of scope |
| `legacy_recognized` | Completed before ownership, with E1–E4 evidence: a `legacy_recognized` marker is written, with no business write |
| `completion_unverified` | Completed before ownership without enough evidence: nothing is written, and it's reported (reasons listed) |
| `already_finalized` / `recover_finalization` / `owned` | Not expected before rollout. If present, someone already ran owned code against dev |

Expected population: threads from DR1, C1/C2, the 3D3 and 3F experiments, the manual AWS verifications, and C2b c1/c3. Runs whose traces went only to local JSONL will be `completion_unverified`, which is the intended, conservative result.

## 3. Terraform: minimal IAM diff (not applied)

**The one change:** a new inline policy `RiskCheckpointOwnership` on `RiskAgentLambdaRole` (`terraform/iam.tf`):
- Actions: `dynamodb:GetItem`, `dynamodb:UpdateItem`, `dynamodb:ConditionCheckItem`.
- Resource: `risk_agent_checkpoints`.
- Key restriction: `ForAllValues:StringLike dynamodb:LeadingKeys = ["thread-*"]`, plus `Null dynamodb:LeadingKeys = false`.
- Existing grants are unchanged:
  - `RiskCheckpointAccess` (`Query`, `PutItem`, unrestricted) already covers the transactions' Puts.
  - `RiskStateAccess` and `RiskTraceAccess` already cover the supplier-state and trace reads and writes.
- `TerraformDeploymentRole` already has `iam:PutRolePolicy` on this role, so it needs no extension.

**Not in the minimal diff:** these are separate decisions, and none is required for ownership.
- **`AUTHORITY_TABLE` / `ACTIONS_TABLE` in the Lambda environment, plus governance IAM.** Without them, the Lambda's governed action stays denied (fail closed). Adding them turns on real ticketing from Lambda: a behavior change.
- **`LEASE_TTL_MS`.** The default of 120 s is close to the 123 s Lambda timeout (see §7).
- **Extending `RiskAgentGovernedWorkerRole`** to the checkpoint, state and trace tables and `action#thread-*`. Only needed if workers move off developer credentials.
- **Image digest:** a deploy-time change (`var.risk_agent_image_digest`), not part of this review.

**Plan, read-only:**
```bash
cd terraform && AWS_PROFILE=ken-ai-dev terraform plan -lock=false -no-color -out=/dev/null
```
- `-lock=false` matters: with `use_lockfile = true`, a normal plan writes a lock object to the state bucket.
- Expected: one resource to add (`aws_iam_role_policy.risk_checkpoint_ownership`). **Any other change in the plan is drift, and must be reviewed separately before apply.**
- `terraform validate` passes offline. Its only warning is a pre-existing `hash_key` deprecation in `traces.tf`, unrelated to this change.

## 4. Concurrency evidence: moto lock scope and gaps

**Lock scope.** `tests/conftest.py` locks **each moto backend call** (`put_item`, `update_item`, `transact_write_items`, `query`, ...), never an attempt. `test_moto_serialization_is_per_operation_not_per_attempt` holds an owned attempt inside the model call while another thread reads, writes and deletes DynamoDB items. Those finish in under 2 s, and the attempt then completes.

**Shown only by SQLite cross-process tests, or by moto (whose calls are serialized, and which models no transaction conflicts). These still need real-DynamoDB verification:**
1. Truly simultaneous conditional lease acquisitions from separate processes yield exactly one owner. SQLite uses a database-wide lock; DynamoDB conditions are per item.
2. A fenced transaction racing a takeover's `UpdateItem` on the same lease item: one is rejected, and the stale write never lands.
3. **Self-conflict.** A task's pending-write transaction and the next checkpoint transaction run concurrently from LangGraph's writer threads, and both ConditionCheck the same lease item. Real DynamoDB may cancel one with `TransactionConflict`, which is retried with backoff. The heartbeat's `UpdateItem` can likewise get `TransactionConflictException`. The lease store now retries that (`test_lease_operations_retry_transaction_conflicts_from_in_flight_fenced_writes`), but the frequency and the latency cost are unknown.
4. `ClientRequestToken` idempotency (moto ignores it), and the real shape and order of `CancellationReasons`.
5. IAM `dynamodb:LeadingKeys` evaluation for ConditionCheck items inside `TransactWriteItems`.
6. The heartbeat thread under Lambda freeze, thaw and timeout, and clock skew between executors.

## 5. Driver compatibility review (DR1, C2b)

| Concern | Finding | Change made |
|---|---|---|
| Leases (C2b) | Every `attempt` now owns the thread. A second attempt during a hold is `contended` | The verdict collects contended attempts separately. Previously the first resume attempt was taken as "the resume" |
| Heartbeat during holds | The heartbeat is a daemon thread and keeps renewing while the main thread (c1) or a writer thread (c3) holds, so contention during a hold is expected | none needed |
| Takeover delay after a kill | A killed owner's lease is only taken over after it expires (`LEASE_TTL_MS`). An earlier attempt is `contended` | c1/c3 verdicts gain ownership checks on owned runs: contended before expiry, a higher epoch at takeover, and the marker and lease at the new epoch |
| Fenced writes | The driver's saver probes wrap `put` / `put_writes` and call through to the fenced methods | none needed |
| `--direct-invoke` | Bypasses the lifecycle and carries no ownership token, so fenced writes would fail | Now refused up front on owned dependencies |
| Owner must finish after contention | `_hold` only ever waited to be killed | New `--release-file`: the hold continues once the file exists |
| Evidence identity | Events had no code or image identity and no epochs | Events record the git commit, dirty flag, `CODE_REVISION` and `IMAGE_DIGEST`. Outcomes record the lease epoch, holder and finalizer. Snapshots record the lease and the final marker |
| DR1 | Process A START (epoch 1, released), process B RESUME (epoch 2); B's AWS calls stay DynamoDB plus SSO | New `ownership` check group (epochs, marker, lease state), and code identity in the evidence |

Local rehearsals on the owned lifecycle (separate processes, SQLite stand-ins) in `tests/test_c2b_owned.py`:
- contention passes;
- a contender that ran inference is rejected;
- kill, then contended until expiry, then takeover passes the c1 verdict with the ownership checks;
- `--direct-invoke` is refused.

## 6. Manual owned experiments (proposed commands; not run)

**Common setup, in both terminals:**
```bash
cd ~/projects/supplychain-ai-agent
export AWS_PROFILE=ken-ai-dev AWS_DEFAULT_REGION=us-west-2
set -a; . docker/worker.env; set +a
export AUTHORITY_TABLE=risk_agent_authority ACTIONS_TABLE=risk_agent_actions
export CODE_REVISION=$(git rev-parse HEAD)
PY=.venv/bin/python; C=scripts/c2b_analysis_kill.py
```
These run with `ken-ai-dev` (admin) credentials, so they need **no IAM change and no deployment**. Neither involves the Lambda.

**A. Owned contention.** The TSMC earthquake headline assesses `suppressed` in dev, so no ticket is opened.

Terminal 1:
```bash
RUN=OWN-contention-$(date -u +%Y%m%dT%H%M%SZ); D=~/ownership-runs/$RUN; mkdir -p $D; E=$D/events.jsonl
$PY $C start --headline "TSMC halts production after earthquake in Taiwan" --events $E
T=thread-...                       # THREAD_ID printed above
$PY $C attempt --thread-id $T --events $E --hold after-inference --release-file $D/release-owner; echo "owner exit=$?"
```

Terminal 2, after Terminal 1 prints `HOLDING`:
```bash
D=~/ownership-runs/<RUN>; E=$D/events.jsonl; T=thread-...
$PY $C snapshot --thread-id $T --events $E --label before-contender
$PY $C attempt --thread-id $T --events $E --hold none    # read "status": exit is 0 for any outcome
$PY $C snapshot --thread-id $T --events $E --label after-contender
touch $D/release-owner
```

Terminal 1, after `owner exit=0`:
```bash
$PY $C verdict --thread-id $T --events $E --expect contention --out $D/verdict.json
```
Both contender snapshots are **required**: `before-contender` immediately before the contender attempt, and `after-contender` immediately after it, while the owner still holds. Without them, the durable no-write check is `not_evaluated` and the verdict is incomplete.

The driver's exit code reports the driver's health, not the workflow outcome, so a `contended` attempt exits 0. Read the outcome's `status`. `risk_worker` exits 5 for `contended`.

Acceptance:
- the owner was held, then released, and completed;
- the contender ran during the hold and was `contended` by the owner (same owner and epoch);
- the contender executed nothing: no analyze, no model call, no workflow write, no trace;
- exactly one inference in total;
- the checkpoint and lease were unchanged across the contender;
- the owner finalized at its epoch, and the lease was released at that epoch.

**B. Kill, expiry and takeover** (c1 on the owned lifecycle). Add `export LEASE_TTL_MS=30000` in both terminals.

Terminal 1:
```bash
RUN=OWN-takeover-$(date -u +%Y%m%dT%H%M%SZ); D=~/ownership-runs/$RUN; mkdir -p $D; E=$D/events.jsonl
$PY $C start --headline "TSMC halts production after earthquake in Taiwan" --events $E
T=thread-...
$PY $C snapshot --thread-id $T --events $E --label before-kill
$PY $C attempt --thread-id $T --events $E --hold after-inference; echo "owner exit=$?"   # expect 137
```

Terminal 2, after `HOLDING ... pid=<PID>`:
```bash
kill -9 <PID>
```

Terminal 1:
```bash
$PY $C snapshot --thread-id $T --events $E --label after-kill
$PY $C attempt --thread-id $T --events $E --hold none     # within 30 s: contended
sleep 35
$PY $C attempt --thread-id $T --events $E --hold none     # takeover: completed at a higher epoch
$PY $C verdict --thread-id $T --events $E --expect c1 --out $D/verdict.json
```
Acceptance:
- **The c1 checks:** the analysis re-runs with a second inference, and retrieval is not repeated.
- **`ownership_contended_until_expiry`:** the early attempt saw the dead owner's epoch.
- **`ownership_takeover_epoch_increased`.**
- **`ownership_final_marker_and_lease`:** the marker and the released lease are at the takeover epoch.

**C. DR1 on the owned lifecycle.** This is the existing command; it now also checks epochs 1 → 2:
```bash
AWS_PROFILE=ken-ai-dev AWS_DEFAULT_REGION=us-west-2 AUTHORITY_TABLE=risk_agent_authority \
  ACTIONS_TABLE=risk_agent_actions .venv/bin/python scripts/durable_resume_proof.py run --out <file>
```

**Evidence:**
- Copy the event logs and verdicts into `docs/evidence/<RUN>/`.
- Redact them with `durable_resume_proof.redact_evidence_text`, as for C2b.
- They record the code identity and every lease epoch.

## 7. Rollout order

1. **Log in, then run the read-only preflight (§1–§2) and `terraform plan -lock=false` (§3).** Review:
   - the deployed digest and triggers;
   - the thread inventory, especially `action_in_flight` and `completion_unverified`;
   - any plan drift.
2. **Decide what can write dev tables during the experiments.** If an EventBridge schedule runs the Phase 2 RSS pipeline, it writes `supplier_risk_state` without ownership. That doesn't conflict with thread ownership, but it can change what an experiment assesses. Pausing it would be a write and needs your approval.
3. **Run experiments A, B and C locally** with `ken-ai-dev`. This needs no IAM change or deploy.
4. **Collect real-DynamoDB evidence for the gaps in §4:**
   - repeat A several times;
   - record `TransactionConflict` retries;
   - confirm exactly one owner under truly simultaneous contenders, by starting two `attempt --hold none` processes together against a released thread. The local gate barrier (`tests/race_harness.py`) has no AWS counterpart yet.
5. **Approve the schema** (`lease` and `final` items), **the IAM diff** (§3) **and the transaction cost** (about twice the write units per step).
6. **`terraform apply`** of the reviewed plan only.
7. **Build one image from the reviewed commit; record its digest.** Stop or drain every pre-ownership worker (design §6). Deploy that digest to the Lambda and to any container or Kubernetes worker.
8. **Repeat A on the deployed executor** (owner on Lambda, contender local, or the reverse).
9. **Only then treat adoption and `completion_unverified` reports as authoritative.**

## 8. Unresolved prerequisites

- **An active AWS session** for every live read: preflight, plan, triggers, threads.
- **The trigger inventory:** triggers aren't defined in Terraform.
- **Confirmation that no other machine or cluster runs workers** against the dev tables.
- **A decision on governance for the Lambda** (`AUTHORITY_TABLE` / `ACTIONS_TABLE` plus IAM). This is separate from ownership.
- **`LEASE_TTL_MS` against the Lambda timeout** (120 s vs 123 s). A Lambda killed at its timeout releases nothing, so a retry within about 2 minutes is `contended`.
- **Real-DynamoDB verification of the gaps in §4.**
- **Explicitly out of scope:** X1 cross-thread deduplication and `IN_FLIGHT` reconciliation.

## 9. Results

### Preflight and plan (read-only)

Evidence: [`docs/evidence/OWN-preflight-20261031T224222Z.json`](evidence/OWN-preflight-20261031T224222Z.json), redacted. Every call the preflight made was a read (the list is in the evidence file).

- **Identity:** account `714781437949`, `AWSReservedSSO_AdministratorAccess_*`, us-west-2.
- **Lambda `ai-supply-chain-risk-agent`:**
  - image `sha256:a0500bbef2e4f5d1939b2d7815b37d2faa2f0f9ed065cbb2e9d11c8cb0a7b224` (pre-Phase 3D, pre-ownership), last modified 2026-10-11T20:46:55Z;
  - timeout 123 s; 1028 MB;
  - role `RiskAgentLambdaRole` (inline policies `BedrockInvoke`, `RiskCheckpointAccess`, `RiskStateAccess`, `RiskTraceAccess`, plus `AWSLambdaBasicExecutionRole`);
  - both backends `dynamodb`; no governance tables; no `LEASE_TTL_MS`.
- **Invocation sources: none.**
  - No function URL, resource-based policy, event source mappings or async configuration.
  - No EventBridge rules on the default bus, and no Scheduler schedules.
  - The function can only be invoked directly by IAM principals. Rules on non-default event buses weren't listed; without a resource policy they couldn't invoke it anyway.
- **Threads: 13, all `legacy_recognized`.**
  - All are completed, with no lease and no marker, and all four evidence checks passed.
  - No fork, no missing trace, no `IN_FLIGHT` or `DENIED` action.
  - Limit: the evidence shows a completion trace exists. It can't rule out a business write that a later run overwrote (every `TSMC#earthquake` row has been rewritten since).
- **Unfenced writers:**
  - The deployed Lambda, only if invoked directly.
  - Any pre-ownership code on other hosts, which isn't visible from here.
  - The governed and Bedrock worker roles have no checkpoint-table access.
- **`terraform plan -lock=false`:** `1 to add, 0 to change, 0 to destroy`, namely `aws_iam_role_policy.risk_checkpoint_ownership` exactly as in §3. No drift. The only warning is the pre-existing `hash_key` deprecation in `traces.tf`. **Not applied.**

### Experiment A: owned contention

**Accepted run `OWN-contention-20261031T231350Z`** ([evidence](evidence/OWN-contention-20261031T231350Z/)).
- **Code:** commit `e699705`, clean tree, recorded in every attempt's events.
- **The run:**
  - START paused before `analyze` at epoch 1.
  - The owner (trace `722b5f5b…`, epoch 2) inferred once (Bedrock request `ad28d7a7-…`) and held at `after-inference`.
  - A contender attempt started during the hold and returned `contended`, with holder `722b5f5b…` at epoch 2. Its only AWS calls were `dynamodb:Query` ×3 and `dynamodb:UpdateItem` ×1 (the refused lease acquisition).
  - Released, the owner completed: `suppressed`, final marker `finalized` at epoch 2, lease `RELEASED` at epoch 2 and still held by the owner at release.
- **Verdict:**
  - The original `verdict.json` (driver at `e699705`) failed `no_workflow_write_by_the_contender`, because it compared the whole lease item. The owner's heartbeat had advanced `expires_at_ms` by 40,162 ms; owner, epoch, state and checkpoint were unchanged.
  - With the corrected check (`4b3ee87`), regenerated read-only from the same events as `verdict.regenerated.json`: **passed and complete, 9 of 9 acceptance checks.** Durable state was unchanged around the contender (no changed fields, lease held throughout), and the expiry advance is reported as a diagnostic.

**Superseded run `OWN-contention-20261031T225042Z`** ([evidence](evidence/OWN-contention-20261031T225042Z/)).
- Commit `e43c54c`, clean tree.
- The contender snapshots were not taken. So, regenerated with `e699705`, `no_workflow_write_by_the_contender` is `not_evaluated` and the verdict is **incomplete** (`complete: false`, `passed: false`). The other 8 checks pass.
- It is kept as the record of why both contender snapshots are now required.

**What these runs don't show:**
- Exactly one owner under **simultaneous** contenders on real DynamoDB: the contender here started during a hold, well after acquisition.
- Transaction-conflict behavior between the owner's concurrent writes. They completed, but conflicts weren't counted.
- Anything about the deployed Lambda, which wasn't involved.

### Experiment B: kill, expiry and takeover (not reviewed)

`~/ownership-runs/OWN-takeover-20261031T233348Z` exists locally, with a `verdict.json` reporting 11 of 11 passed. It has **not been reviewed and is not preserved here.**

There is one open question. Its `contended` attempt (23:35:29Z) precedes the `after-kill` snapshot (23:36:28Z), and the kill time isn't in the event log. `ownership_contended_until_expiry` only requires a contended attempt between the hold and the takeover, so the run may show contention against a **live** owner rather than a dead one. Before accepting it, establish the kill time:
- from the operator's shell record of `kill -9` and its exit status;
- or by requiring an `after-kill` snapshot before any contender attempt.

