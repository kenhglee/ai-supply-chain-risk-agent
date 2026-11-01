# Same-Thread Ownership: Deployment Review

**Status:** Phase 1 is deployed to the dev Lambda (2026-11-01), and checks P2–P4 and P6 have passed; see [§11](#11-phase-1-rollout-record-2026-11-01-dev-account-714781437949).
- P5, P7 and P8 are not run yet.
- Phase 2 (ticketing) is not implemented, and governance stays unconfigured on the Lambda.
- The review itself was prepared from branch `assess-before-act` at `a3a710d`.

**Inputs, all read-only:**
- The ownership preflight ([evidence](evidence/OWN-preflight-20261101T155611Z.json)). It refuses every non-read AWS call inside the process.
- `terraform plan -lock=false`.
- Guarded reads of Lambda versions and aliases, ECR images, the supplier-state rows, and the authority and action-ledger items.
- Local inspection of the tested virtualenv and `uv.lock`.

**Related:** the rollout plan is [`OWNERSHIP_ROLLOUT_PLAN.md`](OWNERSHIP_ROLLOUT_PLAN.md) and the design is [`SAME_THREAD_OWNERSHIP_DESIGN.md`](SAME_THREAD_OWNERSHIP_DESIGN.md).

**Phase 1** deploys ownership with Lambda ticketing still off. **Phase 2** enables Lambda ticketing. They are separate changes with separate approvals, and Phase 2 is not implemented.

## 1. Inventory (2026-11-01T15:56Z)

**Lambda `ai-supply-chain-risk-agent`:**
- Image `sha256:a0500bbe…`, ECR tag `3b18c1a`, last modified 2026-10-11. That code predates ownership.
- Timeout 123 s, 1028 MB.
- Role `RiskAgentLambdaRole`, with inline policies `BedrockInvoke`, `RiskCheckpointAccess`, `RiskStateAccess` and `RiskTraceAccess`, plus `AWSLambdaBasicExecutionRole`.
- No `AUTHORITY_TABLE` / `ACTIONS_TABLE`, so governed actions are denied (fail closed).

**Ways to invoke it:**
- None besides direct IAM invocation.
- There's no function URL, resource policy, event source mapping, async configuration, EventBridge rule or Scheduler schedule.
- Only `$LATEST` exists: no published versions and no aliases.

**Threads:** 18.
- 13 are `legacy_recognized`, unchanged since the first preflight.
- 5 are `owned`, from experiments A, B and C. All are finalized with their lease `RELEASED`, and none has an action `IN_FLIGHT`.

**Other checkpoint writers:**
- In this repository, only `risk_workflow` can write checkpoints. It's reached through the Lambda handler, `risk_worker` and three scripts.
- The running `mcp_server.py` has no checkpoint access.
- On this machine, Docker isn't running, there are no kube contexts, and no worker or driver processes are running.

## 2. IAM and drift

`terraform plan -lock=false`: **`1 to add, 0 to change, 0 to destroy`, so there is no drift.** The only warning is the existing `hash_key` deprecation in `traces.tf`.

| Resource | Role | Actions | Resource and condition |
|---|---|---|---|
| `aws_iam_role_policy.risk_checkpoint_ownership` (`RiskCheckpointOwnership`) | `RiskAgentLambdaRole` | `dynamodb:GetItem`, `UpdateItem`, `ConditionCheckItem` | `table/risk_agent_checkpoints`; `ForAllValues:StringLike dynamodb:LeadingKeys = thread-*`; `Null dynamodb:LeadingKeys = false` |

The existing grants cover the rest of the owned lifecycle: `Query` and `PutItem` on checkpoints, and `GetItem` and `PutItem` on supplier state and traces. The policy only adds permissions, so it changes nothing for the deployed old code.

## 3. Runtime dependencies: lock and build proposal (not implemented)

### What was tested, and what the current image would build

| | Tested (experiments A, B, C) | Current `Dockerfile` |
|---|---|---|
| Interpreter | CPython 3.13.9 (Anaconda), macOS 26 arm64 (`venv/`) | `public.ecr.aws/lambda/python:3.12`, linux/amd64 |
| Resolution | 91 pinned packages | `requirements.txt`: only `langgraph==1.2.2` and `langgraph-checkpoint==4.1.1` are pinned; the other 12 use `>=` and resolve at build time |

Building from the current `Dockerfile` would produce a different interpreter and a dependency set nobody has tested.

### The committed `uv.lock` already matches the tested venv

- `uv lock --check --offline`: the lock is current with `pyproject.toml` (`requires-python >= 3.13`).
- It is a universal lock: one resolution with environment markers and sha256 hashes for every platform's files.
- **Version mismatches between the venv and `uv.lock`: 0** across the packages they share.

| Difference | Packages | Treatment |
|---|---|---|
| In the venv, not in the lock | `langchain==1.2.12` | A stray install: nothing in the repository imports it and no package requires it. Not shipped |
| In the venv, not in the runtime set | `pytest`, `pluggy`, `iniconfig`, `pygments` | Test tooling (dev group). Not shipped |
| Linux x86_64 only (excluded on macOS arm64 by marker) | `greenlet==3.5.1`, a SQLAlchemy dependency whose marker lists `x86_64`/`aarch64` but not `arm64` | Installed in the image. **The only package in the image that wasn't in the tested venv** |
| Windows only | `pywin32==311`, `colorama==0.4.6` | Excluded by marker on Linux |
| macOS-only packages | none | Nothing macOS-specific to exclude: the lock carries no `sys_platform == 'darwin'` markers |

**Platform-specific binaries:** 27 runtime packages ship compiled wheels:
- `aiohttp`, `cffi`, `charset-normalizer`, `cryptography`, `faiss-cpu`, `frozenlist`, `greenlet`, `httptools`, `jiter`, `multidict`, `numpy`, `orjson`, `ormsgpack`, `propcache`, `pydantic-core`, `pyyaml`, `regex`, `rpds-py`, `sqlalchemy`, `tiktoken`, `uuid-utils`, `uvloop`, `watchfiles`, `websockets`, `xxhash`, `yarl`, `zstandard`.
- On Linux they're the same versions as the venv, but different binaries. The lock's hashes cover the Linux wheels, so `pip freeze` on the Mac must not be copied into the image. Use the lock export, which evaluates markers on the target.

**Python 3.12 can't use this lock:**
- 16 compiled runtime packages have no CPython 3.12 manylinux x86_64 wheel at the locked versions: `aiohttp`, `cffi`, `httptools`, `jiter`, `numpy`, `orjson`, `ormsgpack`, `pydantic-core`, `pyyaml`, `regex`, `rpds-py`, `tiktoken`, `uuid-utils`, `watchfiles`, `xxhash`, `zstandard`.
- Under 3.12 they would be built from source or would fail, and the lock itself requires 3.13.
- On 3.13, every runtime package has a manylinux x86_64 wheel except **`sgmllib3k`** (a pure-Python `feedparser` dependency that ships only as a source archive).

### Proposal

1. **Add `requirements.lock.txt`**, generated rather than hand-edited:
   ```zsh
   uv export --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.lock.txt
   ```
   It has 89 pins, each with sha256 hashes and its environment marker; 87 of them apply on linux x86_64 with CPython 3.13. `--frozen` reads the lock without changing it. Regenerate whenever `uv.lock` changes.
2. **Change the `Dockerfile`:**
   ```dockerfile
   FROM public.ecr.aws/lambda/python:3.13@sha256:<digest recorded at review time>
   COPY requirements.lock.txt .
   RUN pip install --require-hashes --no-deps --only-binary=:all: --no-binary=sgmllib3k \
         -r requirements.lock.txt -t ${LAMBDA_TASK_ROOT}
   ```
   - `--require-hashes --no-deps` installs exactly the locked set.
   - `--only-binary=:all:` refuses any source build except `sgmllib3k`.
   - Pinning the base image by digest fixes the interpreter build.
   - `requirements.txt` stays for local development, but the image no longer uses it.
3. **Verify the built image before pushing:**
   - `docker run --rm --entrypoint python <img> -c "import sys; print(sys.version)"` reports 3.13.x.
   - `docker run --rm --entrypoint python <img> -m pip freeze --path /var/task` equals the 87-package Linux set.
   - Compared with the tested venv, the only differences are `+greenlet`, `-langchain` and `-pytest`/`pluggy`/`iniconfig`/`pygments`, all with identical versions.
   - Optional, and only with approval because it's a workflow run: DR1 from the image as a local container with developer credentials, as in Phase 3C1. That exercises the Linux binaries before any Lambda change.

**Residual difference:** Anaconda's CPython 3.13.9 on arm64 versus the Lambda base's CPython 3.13 on x86_64. The deployed-executor checks (§7) are what prove the image.

### Implemented (build preparation; nothing pushed or deployed)

**Changes:**
- `requirements.lock.txt` is exported from `uv.lock` as in step 1. Its body is identical to the reviewed export, and `.gitignore` now makes an exception so this one file is tracked.
- The `Dockerfile` follows step 2.

**Base image:** `public.ecr.aws/lambda/python:3.13`, pinned to its **linux/amd64** manifest `sha256:2f2661d61213e6690f7613bee9d041592c0e92d977168fe6322077e378ade073`.
- Looked up in the public registry on 2026-11-01. The index also lists an arm64 manifest, `sha256:241d405a…`, which isn't used.
- The manifest's content hash matches its digest. Its config says `linux`/`amd64`, created 2026-10-31.

**Validation, run without AWS credentials:**
- **Offline:**
  - `uv lock --check --offline` passes.
  - A hash-checked `uv pip install --target` for `x86_64-manylinux_2_28`, CPython 3.13, installed 87 packages from wheels, plus `sgmllib3k` built from its source archive.
  - All 61 native libraries are x86-64 ELF, and none is a macOS wheel.
  - A dependency check equivalent to `pip check`, using linux markers: no problems.
  - `pip install --dry-run` accepts the `Dockerfile`'s exact options with this lock.
- **Local image build:** `docker buildx --platform linux/amd64`, built from `git archive` plus the changed files, with no credentials and a local-only tag. All of the following ran with `--network none`:
  - **Interpreter:** CPython **3.13.18**, x86_64, glibc 2.34.
  - **Packages:** 87 in `/var/task`, identical to the offline target install.
  - **`pip check`:** "No broken requirements found".
  - **Precedence over the base image:** the base image's own `boto3 1.42.97`, `botocore`, `urllib3 2.8.0`, `s3transfer`, `jmespath`, `dateutil` and `six` are shadowed. With `/var/task` on `PYTHONPATH`, the locked versions import from `/var/task`.
  - **Compiled packages:** all 27 import. FAISS search and an `ormsgpack` round trip work.
  - **LangGraph:** `langgraph 1.2.2` and `langgraph-checkpoint 4.1.1` import, and a `JsonPlusSerializer` msgpack round trip works.
  - **Application modules:** the handler, `risk_worker`, `risk_workflow`, the checkpoint, lease and finalization stores, and the action ledger all import offline, in 0.8 s.
  - **Lambda bootstrap:** a request the handler rejects, sent through the base image's runtime interface emulator, initialized successfully and returned `400` before any AWS client was built.
- **Compared with the tested venv:** `+greenlet 3.5.1`; `-langchain 1.2.12`, `-pytest`, `-pluggy`, `-iniconfig`, `-pygments`; 0 version differences.

**Not verified here:**
- The `sys.path` order inside the real Lambda runtime process. Lambda places `LAMBDA_TASK_ROOT` first, but the shadowing check above used `PYTHONPATH`.
- Any AWS call from the image: Bedrock, DynamoDB, the lease and finalization transactions under the Lambda role. Those are §7.
- Performance under amd64 emulation is not representative.

**Not changed:** `Dockerfile.rss` and `Dockerfile.github` still use Python 3.12 and `requirements.txt`. The Lambda doesn't use them.

## 4. Target commit, build and rollback

**Target commit:** the commit that adds the lock and the `Dockerfile` change (§3).
- Since `e699705`, the application code (`app/`, `handlers/`) is unchanged, so experiments A, B and C ran the code that will ship. The image's dependency set is what changes.
- Decision: the branch is 24 commits ahead of `main`.

**Build:**
```zsh
SHA=$(git rev-parse <target>); B=$(mktemp -d); git archive $SHA | tar -x -C $B
REPO=714781437949.dkr.ecr.us-west-2.amazonaws.com/ai-supply-chain-risk-agent
docker buildx build --platform linux/amd64 --provenance=false \
  --label org.opencontainers.image.revision=$SHA -t $REPO:${SHA[1,7]} --load $B
```
- `git archive` builds exactly the committed tree.
- The image contains no `.git`. Its identity is the digest, plus the revision label.
- Run the checks in §3 step 3, push after an ECR login, and record the pushed digest.
- **Deploy by digest only.** The repository's tags are mutable.

**Rollback:**
- Set `risk_agent_image_digest` back to `sha256:a0500bbe…` (still in ECR; the repository has no lifecycle policy, so keep it that way) and apply.
- **Rollback is not neutral.**
  - The old code ignores the lease and the `final` marker, so it would resume owned threads without fencing.
  - Keep the function throttled (§5) for as long as the old digest is deployed.
- The IAM policy can stay; removing it is optional.

## 5. Stopping unfenced executors

**Throttle mechanism (implemented in Terraform; not applied).**
- `lambda.tf` sets `reserved_concurrent_executions = var.risk_agent_reserved_concurrency`.
- In `variables.tf`, the variable is a `number` with default **`-1`** (unreserved), the value the state already records. A validation rejects anything that isn't a whole number ≥ -1: `-2` and `1.5` are both refused at plan time.
- A throttle set by hand with `put-function-concurrency` would be reverted by the next Terraform apply. That's why the throttle lives in Terraform.

**Plans** (`terraform plan -lock=false` with read-only credentials, 2026-11-01; neither applied):

| Plan | Variable | Result | Changed attributes |
|---|---|---|---|
| Default | `-1` (from the default) | `1 to add, 0 to change, 0 to destroy` | Only `aws_iam_role_policy.risk_checkpoint_ownership` is created. The new variable changes nothing on the live function |
| Rollout hold | `0` | `1 to add, 1 to change, 0 to destroy` | The same IAM policy, plus `aws_lambda_function.risk_agent`: `reserved_concurrent_executions -1 → 0`, the only attribute that changes |

- Both plans report three refresh-time `resource_drift` entries with no planned action. They are `tags` read back as `{}` instead of null, on `RiskAgentPolicyClaudeDeny`, `RiskAgentGovernedWorkerRole` and `RiskAgentPolicyMaterializerRole`, and the two roles' computed `inline_policy`, read back from their separately managed policy resources.
- These are normalizations, not configuration drift, and the text plans show no "changed outside of Terraform" section.

**Keeping the hold at 0 through the IAM and image updates:**
1. Add this line to `terraform/terraform.tfvars`, next to the digest:
   ```hcl
   risk_agent_reserved_concurrency = 0
   ```
2. Terraform loads `terraform.tfvars` automatically on every plan and apply in `terraform/`, so the IAM apply and the image apply both carry `0`.
   - Once the state records `0`, those plans show **no** change to `reserved_concurrent_executions`. The image plan's only change should be `image_uri`.
   - The hold applies to every run from `terraform/`, so it doesn't depend on a flag that has to be repeated.
3. **Precedence:** only `-var` / `-var-file` on the command line, or a `*.auto.tfvars` file, can override `terraform.tfvars`. A `TF_VAR_risk_agent_reserved_concurrency` environment variable can't.
   - During the rollout, pass neither `-var` nor `-var-file` for this variable, and add no `*.auto.tfvars`. None exists today.
4. **Apply only saved plans** (`terraform plan -out=<file>`, review it, then `terraform apply <file>`). Before every apply, confirm the plan doesn't contain `reserved_concurrent_executions = 0 -> -1`.
   - An apply that dropped the line, or ran from a checkout without it, would show exactly that change. It isn't silent in a reviewed plan, but `apply -auto-approve` would skip the review.
5. **Lift the hold only after readiness is verified (§6 step 5).** Remove the line (or set `-1`) as its own change. That plan must show only `reserved_concurrent_executions 0 → -1`.

**Invocations already running:**
- A throttle of 0 refuses new invocations, but doesn't stop ones already running. A code update doesn't stop them either.
- An old-code invocation can keep running for up to the 123 s timeout after either change. So, after the throttle is in place and **before the image deploy**:
  1. Wait at least 123 s, plus a margin.
  2. Confirm in CloudWatch that `ConcurrentExecutions` (maximum) is 0. Confirm there's no new `START RequestId` in the function's log group for longer than the timeout.
- **Async events:** an async event submitted while the function is throttled is retried by Lambda for up to 6 h by default, and would run on the new code once the throttle is removed.
  - None are expected, since there are no triggers. Before removing the throttle, check `AsyncEventsReceived` (it should be 0).

| Executor | State | Control |
|---|---|---|
| Lambda (old digest) | Direct invocation only | Terraform throttle to 0, then drain (above) |
| Local workers and drivers | Only HEAD code (owned); none running | `pgrep -fl "risk_worker\|c2b_analysis\|durable_resume"` |
| Docker and kind (C1/C2 images on `a0500bbe`) | Docker not running | Keep it stopped, or confirm no `ai-supply-chain-risk-agent` container is running |
| Other machines and clusters | Not visible from here | Operator attestation (rollout plan §8) |

## 6. Phase 1 sequence

Each step's apply goes through a saved, reviewed plan.
1. Re-run the preflight and `terraform plan -lock=false`. The expected results are in §1, §2 and §5 (the default plan).
2. **Hold and IAM:** add `risk_agent_reserved_concurrency = 0` to `terraform.tfvars` (§5), then plan and apply.
   - Expect the rollout-hold plan: the policy is created, and `reserved_concurrent_executions` goes `-1 → 0`.
   - Applying the two together is safe: the IAM policy only adds permissions and changes nothing for the old code.
   - To throttle first and then apply IAM separately, use two plans, with `-target=aws_lambda_function.risk_agent` on the first.
3. Drain (§5): wait past the timeout and confirm nothing executes.
4. Build, verify and push (§3–§4). Set the digest in `terraform.tfvars`, keeping the hold line. The plan should change only `image_uri`, with no `reserved_concurrent_executions` change. Apply.
5. **Verify image readiness while throttled; this needs no invocation:**
   - `GetFunction` shows `State: Active` and `LastUpdateStatus: Successful`.
   - `ResolvedImageUri` equals the pushed digest.
   - The environment is unchanged (no governance tables).
   - The role has `RiskCheckpointOwnership`.
   - `GetFunctionConcurrency` reports `0`.
   - Limit: Lambda initializes the image only on its first invocation, so a failure at import time can only show up in step 7.
6. **Lift the hold** in its own change: remove the line, or set `-1`. The plan must change only `reserved_concurrent_executions 0 → -1`.
7. Run the checks in §7.

## 7. Phase 1 checks (executed as the Lambda execution role)

All invocations are **synchronous** (`RequestResponse`). The handler reports its outcome in the response body; an async (`Event`) invocation returns only `202` and doesn't wait for or return the outcome.

**For every CLI invocation:**
- Set `AWS_MAX_ATTEMPTS=1`, so that a client-side timeout retry can't create a second invocation.
- Set `--cli-read-timeout 150`, which is longer than the 123 s timeout.

| # | Check | Expected result | What it proves |
|---|---|---|---|
| P2 | `checkpoint_start` with the TSMC earthquake headline | 200 `paused`, `lease_epoch: 1` | The image initializes on Lambda. Under the role's `LeadingKeys` condition, lease creation and fenced checkpoint transactions work (rollout plan §4 gap 5) |
| P3 | `checkpoint_resume` on that thread | 200 `completed`, `suppressed`, `lease_epoch: 2`. Afterwards, the final marker is at epoch 2 and the lease `RELEASED` at epoch 2 | The **finalization transaction** succeeds under the role: lease ConditionCheck, `final` marker Put, and the supplier-row Put (a `suppressed` result still updates `last_seen_at`). The completion trace (a conditional Put) and the **lease release** (a conditional `UpdateItem`) are **separate writes after** that transaction, and each is checked on its own |
| P4 | `checkpoint_resume` on the same finalized thread | 200 `already_completed`, reporting the finalizer's trace and epoch 2 from the marker | **This attempt acquires and releases ownership:** the lease goes to epoch 3 and back to `RELEASED`. It then reads the marker and stops. Expect no Bedrock call, no checkpoint, no state write and no marker change; confirm all of that. It shows that a finalized thread produces **no repeated effect or business write in this case**. It is not a general exactly-once guarantee (see below) |
| P5 | Two synchronous `checkpoint_resume` invocations of one fresh paused thread, started in parallel (two background CLI processes, then `wait`) | One 200 `completed` and one 409 `contended`, with the winner's epoch as holder | The two invocations' run times overlapped (both requests sent before either response), and exactly one became owner. This narrows rollout plan §4 gap 1. It doesn't prove the two conditional writes reached DynamoDB at the same instant, and one run is not a rate |
| P6 | A local owner (`c2b attempt --hold after-inference --release-file …`, developer credentials), then a Lambda `checkpoint_resume` during the hold | 409 `contended`, holder at the local owner's epoch. After release, the owner finalizes | The deployed executor respects another executor's lease (rollout plan §7 step 8) |
| P7 | Controlled eligible alert (§9), START then RESUME on Lambda | 200 `authority_denied`, denial layer for unconfigured governance, `eligibility_change_type: new_alert`, `next: ["act"]`. No ledger item, no state row for that supplier, no final marker, lease `RELEASED` at epoch 2 | Eligible alerts fail closed under ownership on the deployed executor. Phase 1 doesn't enable ticketing. The thread stays parked at `act` for Phase 2 |
| P8 | CloudWatch Logs for each invocation; re-run the preflight | No `AccessDenied`. Count the `TransactionConflict` retries. The 13 legacy threads are unchanged; no `IN_FLIGHT` | A first measurement of rollout plan §4 gap 3. Shows nothing was adopted unexpectedly |

**The guarantee these checks support:**
- The ledger admits at most one claim per `action_id`, and a `COMPLETED` action replays its stored result.
- An action left `IN_FLIGHT` is never re-run. The executor reports `action_in_flight`, and reconciliation is out of scope.
- An effect admitted before a takeover may still complete.
- So the effect is **at most once per action, with possibly unrecorded completion**: not exactly once. A repeat RESUME shows only that this path doesn't repeat it.

## 8. Rollback triggers (Phase 1)

Roll back (§4) if any of these happen:
- any `AccessDenied` or initialization failure in P2–P7;
- a P3 or P4 write that differs from the table above;
- P5 producing two owners;
- P7 producing a ledger item or a state row.

Throttle first, then roll back.

## 9. Phase 2: Lambda ticketing (separate; not implemented)

**Controlled case:** dev supplier state has rows only for `TSMC#earthquake` and `TSMC#outage`. Murata and Foxconn have none.
- Any alert attributed to either one is `new_alert`, whatever `risk_type` the model picks. Example headline: *"Murata halts production at Malaysia plant after fire"*.
- Before going further, check `eligibility_change_type` in the outcome; if it isn't `new_alert`, use Foxconn.
- Each eligible run uses up a fresh key.

**Current governance:**
- `agent#supplier-risk-agent` is `ACTIVE` (version 9).
- `policy#supplier-risk-agent#open_review_ticket` is `allow` (policy v4).
- No materialization is needed. The effect is the in-memory ServiceNow mock, so it writes only the ledger item.

**Required change** (Terraform, after approval):
- **Lambda environment:** `AUTHORITY_TABLE=risk_agent_authority`, `ACTIONS_TABLE=risk_agent_actions`.
- **A new inline policy on `RiskAgentLambdaRole`:**
  - `dynamodb:GetItem` and `ConditionCheckItem` on `risk_agent_authority`, with `ForAllValues:StringEquals LeadingKeys = [agent#supplier-risk-agent, policy#supplier-risk-agent#open_review_ticket]`;
  - `dynamodb:GetItem` and `UpdateItem` on `risk_agent_actions`, with `StringLike LeadingKeys = action#thread-*`.
  - The lease ConditionCheck inside admission is covered by `RiskCheckpointOwnership`.
- **Decision:** the IAM Deny switches attach only to the governed and Bedrock worker roles. For the Lambda, revoking the authority item is the only emergency stop.

**Checks:**

| # | Check | What it proves |
|---|---|---|
| Q1 (optional; an operator write) | Revoke, RESUME the P7 thread, then restore. Expect `authority_denied` at the authority layer, a fenced `DENIED` ledger item, and the thread still at `act` | The deny transaction works under the role, and revoke stops the Lambda |
| Q2 | RESUME the P7 thread. Expect `completed`, `new_alert`, ledger `COMPLETED` claimed by this attempt (the Lambda executor) at `lease_epoch: 3`; the marker at epoch 3 with the state row in the finalization transaction; then a separate lease release at epoch 3 | Owned admission (authority + policy + ledger claim + lease check, in one transaction) and finalization under the role. The earlier denial didn't use up the action |
| Q3 | RESUME again. Expect `already_completed` (with its own lease acquire and release), and the ledger unchanged | No repeated effect in this case. It is not exactly once (§7) |
| Q4 (optional) | A more severe Murata headline. Expect `escalated` | The escalation route. Unreliable, because the model sets the risk level |

**Rollback:** revoke the authority item. It takes effect within seconds and needs no deploy. Reverting the environment change fails closed. Denied threads stay parked at `act`.

## 10. Still untested after both phases

- **Lambda dying mid-attempt:** killed at the 123 s timeout or frozen mid-heartbeat, followed by a takeover (rollout plan §4 gap 6). With `LEASE_TTL_MS` at its default of 120 s, a retry within about 2 minutes gets `contended`.
- **Clock skew** between the Lambda and local executors.
- **Contention under load** and the rate of `TransactionConflict`.
- **`ClientRequestToken` idempotency** and the shape of `CancellationReasons`.
- **Reconciling `IN_FLIGHT` actions** and X1 cross-thread deduplication (out of scope).
- **Unfenced workers on other machines:** attestation only.
- **The scheduled RSS path:** it isn't checkpointed, and has no trigger.

## 11. Phase 1 rollout record (2026-11-01, dev account 714781437949)

Every apply below used a saved plan, after its SHA-256 and the caller's identity were checked.

**Deployed:** image `714781437949.dkr.ecr.us-west-2.amazonaws.com/ai-supply-chain-risk-agent@sha256:7701dd3365cae6e753d2864d051d105bcee31fe3bfa41bb16fe5ea3a250662bd`.
- Built from source `a117c8cf2d300296e33de04dfd5c707ef26df78d` (`git archive`, linux/amd64, pinned Python 3.13 base, hashed lock). ECR tag `a117c8c`.
- Its OCI label `org.opencontainers.image.revision` carries the same commit.
- Image identity, manifest and package list: [evidence](evidence/OWN-image-a117c8c/).
- **Rollback target:** `sha256:a0500bbe…` (§4).

**Rollout steps:**

| Step | Change | Identity | Result |
|---|---|---|---|
| Deploy-role bootstrap (`a117c8c`) | `TerraformDeploymentPolicy`: `RiskAgentLambdaConcurrency` (`lambda:Get/Put/DeleteFunctionConcurrency` on this function only); targeted apply | `ken-ai-dev` (admin) | 1 changed. IAM simulation: all three actions `allowed` on the function, `implicitDeny` on any other function |
| Hold + ownership IAM (`2be27e3`, plan made at `a117c8c`) | `RiskCheckpointOwnership` created; reserved concurrency `-1 → 0` | `TerraformDeploymentRole` | 1 added, 1 changed. Concurrency 0 confirmed at 16:29:27Z. The policy matches the reviewed document |
| Drain | Waited until 16:31:57Z (150 s after the hold) | read-only | Function logs: 0 events since 2026-10-11 (an observed zero). Invocation, concurrency, throttle and async metrics: **no datapoints** (missing telemetry, not an observed zero). The function was `Inactive` (`Idle`). No invocation sources |
| Image (`5ec2e48`) | `image_uri` → `7701dd33…`, hold kept at 0 | `TerraformDeploymentRole` | 1 changed (16:38Z). `LastUpdateStatus: Successful`, `State: Active`. Role, environment and ownership IAM unchanged; `AUTHORITY_TABLE` / `ACTIONS_TABLE` absent. A fresh plan showed no changes |
| Hold release (`8b9a64c`) | Reserved concurrency `0 → -1` | `TerraformDeploymentRole` | 1 changed (16:42:37Z). Re-checked first: still no async or throttle datapoints, no log events, no invocation sources |

**Deployed checks** (`RiskAgentLambdaRole`; synchronous `aws lambda invoke` with `AWS_MAX_ATTEMPTS=1`, each run once, none retried):

| Check | Evidence | Result |
|---|---|---|
| **P2** `checkpoint_start`, TSMC earthquake | [OWN-lambda-P2-20261101T164251Z](evidence/OWN-lambda-P2-20261101T164251Z/) | Lambda 200, no `FunctionError`; handler 200 **`paused`**, `next: ["analyze"]`, **`lease_epoch: 1`**, `thread-556d48cfd1a44c488e39f4b685330d88`. The lease is `RELEASED` at epoch 1 with no final marker. Checkpoint at step 3 with `lease_epoch: 1` in its metadata and no pending writes. Trace `paused` (`infer`, `decide`, `retrieve`). One Claude call (Converse) and 4 Titan embedding calls. No ledger item and no business write. No AccessDenied or import errors; the FAISS AVX2 message is an informational fallback |
| **P3** `checkpoint_resume` | [OWN-lambda-P3-20261101T164525Z](evidence/OWN-lambda-P3-20261101T164525Z/) | Lambda 200, no `FunctionError`; handler 200 **`completed`**, **`suppressed`** (`eligibility_basis: assessed`, `action_eligible: false`), **`lease_epoch: 2`**. START's retrieval steps are reused byte for byte, `analyze` ran (one Converse call, no embeddings), then `validate` and `assess`. **Finalization under `RiskAgentLambdaRole`:** final marker `finalized` at epoch 2 by RESUME trace `8cbf0626…`, and the `TSMC#earthquake` row is written (`last_seen_at` 16:45:28Z). The lease is then `RELEASED` at epoch 2. Step 6, 0 pending writes. No ledger item. No AccessDenied or transaction errors. Warm, 2.5 s |
| **P4** repeat `checkpoint_resume` | [OWN-lambda-P4-20261101T164718Z](evidence/OWN-lambda-P4-20261101T164718Z/) (`before.json` / `after.json`) | Lambda 200; handler 200 **`already_completed`**, reporting the finalizer's trace `8cbf0626…` and epoch 2, `state_written: false`. No model or embedding calls. Business row, final marker, ledger, checkpoints and the thread's traces are **unchanged**. **Only the lease changed: epoch 2 → 3**, owned by this attempt's own trace id, which appears nowhere else, and `RELEASED`. Warm, 249 ms |

**What P2–P4 show:**
- Owned START, RESUME and the repeat RESUME work on the deployed executor under its own scoped role. That includes `LeadingKeys`-conditioned lease updates, fenced checkpoint transactions and the finalization transaction (rollout plan §4 gap 5).
- A finalized thread produced no repeated effect or business write in this case. This is not an exactly-once guarantee (§7).

**Open observations; not addressed, and no code was changed for them:**
1. **Cold-start initialization timeout.**
   - P2's first cold start logged `INIT_REPORT Init Duration: 9999.71 ms Phase: init Status: timeout`. Lambda then re-ran initialization inside the invocation, and the request succeeded (`Duration` 18,977 ms).
   - Locally, under the base image's runtime interface emulator, initialization took 0.8 s.
   - **The cause is not determined.** Nothing collected here establishes it, so it isn't attributed to image fetching or anything else. It needs init-phase evidence from further cold starts.
2. **`state_written` is null in the completion trace.** P3's completion trace (`resumed_completed`) records `state_written: null`. The handler outcome reported `state_written: true`, and the final marker and state row confirm the write.

**Recording quirk:** P2's `invoke-times.txt` start line reads `16:42:51.3NZ`, because BSD `date` doesn't support `%3N`. The time is accurate to the second, and the Lambda log gives the precise times.

### P6: a local owner holds the thread while the Lambda contends

**Run `OWN-lambda-P6-20261101T165151Z`** ([evidence](evidence/OWN-lambda-P6-20261101T165151Z/)).
- **Local side:** commit `b67efe4` (clean tree), whose runtime code matches the deployed image's. It ran with developer credentials.
- **Contender:** one synchronous Lambda `checkpoint_resume` under `RiskAgentLambdaRole`.
- **Thread:** `thread-c4693a44854641d3b308463ee15f6870`.

**Timeline (UTC):**

| Time | Event |
|---|---|
| 16:51:52–16:51:56 | Local START (trace `8084c240…`) pauses before `analyze` at epoch 1 |
| 16:52:31 | The local owner (pid 23703, trace `4809782e…`) starts. It acquires **epoch 2**, enters `analyze`, makes one Bedrock Converse call (request `a9f70cc9-…`, CloudTrail 16:52:35), and **holds** at `after-inference` from 16:52:35.560 |
| 16:52:58.848 | Snapshot `before-contender` |
| about 16:53:11–16:53:15 | Lambda cold start on a new environment (`…d77de612…`): `Init Duration: 4111.74 ms` |
| 16:53:15.579–16:53:16.298 | Lambda request `069651b0-…`. Transport `StatusCode 200`, **no `FunctionError`**. Handler **`statusCode 409`, `status: contended`**, holder `{owner: 4809782e…, epoch: 2, expires 16:55:13.139}` (the local owner). Duration 711 ms, billed 4,824 ms |
| 16:53:31.919 | Snapshot `after-contender` |
| 16:54:57.785 | The owner is released, then completes. Final marker `finalized` at **epoch 2** by `4809782e…`, `suppressed`; lease **`RELEASED` at epoch 2** at 16:54:58.901 |

**The two snapshots.** Both are in `events.jsonl` as `event: "durable_snapshot"` with `label` `before-contender` and `after-contender`, so neither is missing.
- **Unchanged between them:** checkpoint `1f1d6148-…` (step 3, `next: ["analyze"]`), no analyze writes stored, no pending tasks, no final marker, and the lease's owner, epoch 2 and state `HELD`.
- **Only difference:** the lease's `expires_at_ms`, 16:54:32.954 → 16:55:13.139. That's the owner's heartbeat; a renewal around 16:53:13 is implied by the 120 s TTL.
- The contender's reported holder expiry equals the after-snapshot's, so the contender read the lease **after** that renewal.

**Other evidence:**
- **No Lambda completion trace:** the contender's trace `a91b4003…` doesn't exist in `risk_traces`. The thread's only traces are START's `paused` and the owner's `resumed_completed`.
- **No model call by the Lambda:**
  - The contender's log has no Converse, Titan or other model lines.
  - CloudTrail's Bedrock events for 16:52–16:56 show exactly one `Converse`, at 16:52:35 by the developer SSO role: the owner's call. None came from `RiskAgentLambdaRole`.
- **Owner:** exactly one inference, `completed` at epoch 2, no ledger item. The `TSMC#earthquake` row was written at the owner's finalization (16:54:58Z).
- **Errors:** none in the Lambda log (no AccessDenied, transaction or throttling lines).

**What P6 demonstrates:** the deployed executor respects a lease held by another executor. The Lambda attempt met the local owner's valid epoch-2 lease, returned `contended`, and changed no checkpoint, lease ownership, epoch or state. Afterwards the owner completed and finalized at its own epoch.

**Claims without direct evidence:**
- **The contender's own AWS calls.** The Lambda path isn't instrumented, and DynamoDB data events aren't in CloudTrail. The expected `Query` calls plus one refused `UpdateItem` are inferred from the code. What *is* observed is that the lease didn't change apart from the owner's expiry.
- **The business row around the contender.** The snapshots don't include `supplier_risk_state`. The row's current value is the owner's later write, so it can't directly show that the contender didn't write. There's indirect evidence: the status was `contended`, there's no Lambda trace, and no graph node ran.
- **The action ledger** wasn't snapshotted around the contender either. It has no item for the thread now.
- **No CLI exit code or client-side timing** was recorded for the Lambda invocation. The Lambda log provides the server-side times.

**Redaction:** the host name in `owner_executor` is redacted in every file. In `lambda-invoke-meta.json`, the base64 `LogResult` was decoded, redacted and re-encoded, and the file re-serialized as JSON. The empty `release-owner` trigger file isn't kept.

**Cold start:** this environment's initialization took **4,111.74 ms** and completed within the limit (there's no `INIT_REPORT` timeout line). That doesn't resolve P2's 9,999.71 ms init timeout, whose cause is still undetermined.

**Not yet done:**
- P5 (parallel synchronous contention), P7 (an eligible alert failing closed) and P8 (logs and conflict counts, preflight re-run).
- Phase 2 (ticketing).
- The items in §10.
