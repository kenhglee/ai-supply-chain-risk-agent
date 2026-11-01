# Same-Thread Ownership: AWS Rollout Plan (for review)

**Status:** step 1 (read-only preflight and plan) and step 3 are done: experiments A, B and C (owned DR1) are accepted against AWS. They ran as local workers with developer credentials (`ken-ai-dev`); see [Results](#9-results). Still not done:
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

## 6. Manual owned experiments (run; results in §9)

A, B and C have been run and accepted ([§9](#9-results)). The accepted runs used the active virtualenv (`$VIRTUAL_ENV/bin/python`, i.e. `venv/`) in place of `.venv/bin/python`. B's accepted run included `record-exit`, and C ran with `--expect-action skipped`.

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

Terminal 1, after it prints `owner exit=137`, and **before any contender attempt**:
```bash
$PY $C record-exit --events $E --pid <PID> --exit-status 137   # confirms <PID> is gone and records it
$PY $C snapshot --thread-id $T --events $E --label after-kill
$PY $C attempt --thread-id $T --events $E --hold none     # within 30 s of the last renewal: contended
sleep 35
$PY $C attempt --thread-id $T --events $E --hold none     # takeover: completed at a higher epoch
$PY $C verdict --thread-id $T --events $E --expect c1 --out $D/verdict.json
```
Acceptance:
- **The c1 checks:** the analysis re-runs with a second inference, and retrieval is not repeated.
- **`ownership_contended_until_expiry`:** a contender that started **after** the recorded exit saw the dead owner's epoch, while that lease was still valid. If no exit is recorded, or no contender follows it, this check is `not_evaluated` and the verdict is incomplete. The record's time is an upper bound on the death, not the kill time.
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
3. **Run experiments A, B and C locally** with `ken-ai-dev`. This needs no IAM change or deploy. **Done:** all three are accepted (§9).
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

### Experiment B: kill, expiry and takeover

**Accepted run `OWN-takeover-20261101T153802Z`** ([evidence](evidence/OWN-takeover-20261101T153802Z/)).
- **Code:** execution commit `974c21e`, clean tree, recorded in every attempt's events (`git_dirty: false`).
- **The run** (thread `thread-cb6b4b31…`, all times UTC on 2026-11-01):
  - START (trace `e8b4427a…`) paused before `analyze` at epoch 1, checkpoint `1f1d60ab-…` at step 3.
  - The owner (pid 93738, trace `c79de035…`, epoch 2) inferred once (Bedrock request `ed2b31ed-…`) and held at `after-inference` from 15:42:14.607.
  - The operator killed it. `record-exit` confirmed pid 93738 was no longer running and recorded that at **15:42:46.438**. The time of the kill itself was not recorded, so this is an upper bound on the death.
  - A contender started at **15:42:47.160**, after the recorded exit. It returned `contended`, holder epoch 2, owner executor pid 93738, lease expiry **15:43:12.789** (25.6 s after the contender started). It executed nothing: its only AWS calls were `dynamodb:Query` ×3 and `dynamodb:UpdateItem` ×1 (the refused lease acquisition).
  - The `after-kill` snapshot (15:42:50.464, before recovery) shows checkpoint `1f1d60ab-…` unchanged at step 3, `analyze` next, no analyze writes stored, and the epoch-2 lease still `HELD` with that expiry.
  - The takeover (trace `1ebe1ca3…`) started at 15:43:48.455, after expiry. It acquired **epoch 3**, re-ran `analyze` with a second Bedrock call (`d3a908f0-…`), and completed `suppressed`. No ticket was opened.
  - **Finalization:** the final marker is `finalized` at epoch 3 by the takeover. The lease is `RELEASED` at epoch 3 by the same attempt. The graph is at step 6 with no pending tasks.
  - Retrieval was START's own (started 15:41:26.243). There were no tripwire hits.
- **Verdict** (`verdict.json`, `--expect c1`, produced at the run by the driver at `974c21e`): **complete and passed (`complete: true`, `passed: true`), 11 of 11 acceptance checks.** These include `ownership_contended_until_expiry` (1 contender after the recorded exit and 0 before it, lease valid at its start, nothing executed), `ownership_takeover_epoch_increased` (2 → 3) and `ownership_final_marker_and_lease`.
- **Notes:**
  - `record-exit` was run without `--exit-status`, so the record's `exit_status` is `null`. The check relies on the confirmed-not-running record, not on the status.
  - The evidence is redacted with `durable_resume_proof.redact_evidence_text`; only the host name was replaced. The raw originals are kept unchanged outside the repository.

**What this run doesn't show:**
- The exact kill time. It is bounded only by the recorded exit.
- Anything about simultaneous contenders, transaction conflicts or the deployed Lambda (see experiment A).

**Earlier incomplete run `OWN-takeover-20261031T233348Z`** ([evidence](evidence/OWN-takeover-20261031T233348Z/)). Code: commit `4b3ee87`, clean tree. It is superseded by the accepted run above and kept as the record of why `record-exit` is required.

**Demonstrated:**
- **Recovery at epoch 3.**
  - START paused before `analyze` at epoch 1.
  - The owner (trace `5e34d227…`, epoch 2) inferred once (Bedrock request `20a65392-…`), held at `after-inference`, and was killed. It has no exit event and wrote nothing. The checkpoint `1f1d5838-…` is unchanged, still at step 3, `analyze` next.
  - The takeover attempt (trace `d73d3d0b…`) acquired at **epoch 3** and completed.
- **Repeated inference.** The takeover entered `analyze` again and made a second Bedrock call (`e068a476-…`): two inferences in total, at least once as expected for a node interrupted before its writes were stored.
- **Retrieval reused.** The final trace's `retrieve` step is START's own (started 23:33:51.409Z). There was no new retrieval and no tripwire hit.
- **Finalization.** The outcome was `suppressed`, with final marker `finalized` at epoch 3 (by the takeover attempt). The lease was `RELEASED` at epoch 3 by that attempt, and the graph is at step 6 with no pending tasks.

**Not verified: post-kill, pre-expiry contention.**
- One attempt (started 23:35:29.343Z) returned `contended`, with holder epoch 2 and lease expiry 23:35:58.427Z.
- The kill time and the owner's exit were **not recorded**.
- The `after-kill` snapshot came later (23:36:28.094Z). It shows the epoch-2 lease still `HELD`, with that expiry unchanged.
- The run therefore can't show whether that contender met the owner **before or after** it died. No kill timestamp is inferred here.

**Verdicts:**
- The original `verdict.json` passed `ownership_contended_until_expiry` under the earlier, weaker rule.
- Regenerated read-only from the same events with the corrected verdict (`1ad2d5e`), as `verdict.regenerated.json`:
  - that check is **`not_evaluated`** (no recorded exit);
  - the verdict is **incomplete** (`complete: false`, `passed: false`);
  - the other 10 acceptance checks pass.

**The remaining claim** was then verified by repeating B with `record-exit` between the kill and the contender (§6), in the accepted run above. This run's thread is finalized and can't be reused.

### Experiment C: DR1 on the owned lifecycle

**Accepted run `DR1-owned-20261101T155104Z`** ([evidence](evidence/DR1-owned-20261101T155104Z.json)). The evidence file is the harness's own output, byte-identical to the original. The harness redacts before writing (host name, STS session name), so no unredacted original exists.
- **Command:** `scripts/durable_resume_proof.py run --expect-action skipped`, TSMC earthquake headline, fresh thread `thread-587bc251…`. Credentials: `ken-ai-dev` SSO (`AWSReservedSSO_AdministratorAccess_*`), us-west-2. Bedrock Claude Haiku 4.5 and Titan v2 embeddings (FAISS). Every table was DynamoDB.
- **Code:** the evidence records `git_commit: 68af710`, clean tree (`git_dirty: false`). This is the code that ran.
  - `code_revision` is **stale**: it says `974c21e`, because `CODE_REVISION` was still exported from an earlier shell.
  - `974c21e..68af710` changes only documentation and evidence, so no executed code differs between the two.
- **The run** (times UTC on 2026-11-01):
  - **Process A** (pid 98348, 15:51:06.162–15:51:12.892, exit 0, trace `3302dd93…`) ran infer, decide, retrieve and analyze. It paused before `validate` at **epoch 1**. Checkpoint `1f1d60c1-00d9-…` is at step 4 with the analysis saved: TSMC, earthquake, High.
  - **Process B** (pid 98393, 15:51:13.810–15:51:16.765, exit 0, trace `c819702e…`) started after A exited, in a new interpreter with tripwires installed. It resumed at **epoch 2**, ran `validate` and `assess`, and completed `suppressed` (`eligibility_basis: assessed`, not action-eligible). There was no action and no ledger item. Its trace event is `resumed_completed`, and the final checkpoint `1f1d60c1-24f6-…` is at step 6.
  - **B made no retrieval, embedding or model call.** There were no tripwire hits, and its only AWS services were `dynamodb` (18 calls) and `sso` (one `GetRoleCredentials`).
  - **Finalization:** the final marker is by B's trace at epoch 2. The lease is `RELEASED` at epoch 2.
- **Verdict:** `passed: true`, **20 of 20 checks**:
  - resume: 13 of 13;
  - ownership: 4 of 4 (`a_started_at_epoch_1`, `b_resumed_at_the_next_epoch`, `b_trace_and_final_marker_at_b_epoch`, `lease_released_at_b_epoch`);
  - action: 3 of 3 (`eligibility_assessed_by_b`, `action_matches_eligibility`, `expected_action_mode`).

**What this run doesn't show:** the action-eligible route (`new_alert` / `escalated` through `act`) under ownership. The scenario assessed `suppressed`, as expected.

### What the accepted experiments cover, and what remains open

A, B and C ran the owned lifecycle **as local worker processes against the dev AWS tables, with developer (admin) credentials**. Still open:
- **IAM apply** of the reviewed plan (§3);
- **deployment** of an ownership image (§7, step 7);
- **verification on a deployed executor**: the Lambda, or a worker under its own role (§7, step 8);
- the real-DynamoDB gaps in §4: simultaneous contenders and transaction-conflict counts.
