"""Declarative agent policy (Phase 3F): intent, validation and materialization planning.

The policy file is reviewed intent only. Runtime enforcement never reads it;
enforcement points consume *materialized* state written by the materializer
(app/governance/policy_cli.py):

  application boundary  policy#<agent>#<capability> items in the authority
                        table, checked atomically at action admission
  AWS IAM boundary      attachment of a policy-layer Deny managed policy
                        (separate from the operator's emergency Deny)

Effective permission = ceiling AND policy AND NOT emergency. Deny overrides;
anything missing, malformed or unknown denies.

Policy file (JSON):
  {
    "policy_version": 3,
    "subject": "agent/supplier-risk-agent",
    "capabilities": {"open_review_ticket": "deny", "claude_inference": "allow"}
  }
Capabilities absent from the policy are denied.
"""
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from app.governance.authority import AGENT_ID

ALLOW = "allow"
DENY = "deny"
ABSENT = "absent"
EFFECTS = (ALLOW, DENY)

APPLICATION = "application"
AWS_IAM = "aws_iam"

POLICY_ITEM_KIND = "policy_capability"
MATERIALIZATION_KIND = "policy_materialization"
POINTER_KIND = "policy_materialization_pointer"


class PolicyError(ValueError):
    """Malformed or invalid policy: nothing may be materialized from it."""


class BroadeningNotApproved(RuntimeError):
    """The plan grants a capability that was not allowed before."""


class PointerBehind(PolicyError):
    """A newer materialization COMPLETED but the latest pointer never advanced
    (e.g. a crash between the two writes). Repairable: `policy_cli repair`, or
    re-apply exactly that version and hash."""

    def __init__(self, message: str, *, version: int, sha256: str, pointer_version: int | None) -> None:
        super().__init__(message)
        self.version = version
        self.sha256 = sha256
        self.pointer_version = pointer_version


# ---- Code-side catalog: subjects, capabilities, ceilings, materialization targets ----

SUBJECTS = {f"agent/{AGENT_ID}": AGENT_ID}


@dataclass(frozen=True)
class Capability:
    name: str
    boundary: str          # APPLICATION or AWS_IAM
    ceiling: str           # ALLOW if infrastructure/code can ever grant it, else DENY
    deny_policy_name: str | None = None   # AWS_IAM: policy-layer Deny attached when the policy denies
    iam_role_name: str | None = None      # AWS_IAM: role the Deny is attached to


GOVERNED_WORKER_ROLE = "RiskAgentGovernedWorkerRole"

CAPABILITIES = {
    "open_review_ticket": Capability("open_review_ticket", APPLICATION, ALLOW),
    "claude_inference": Capability(
        "claude_inference", AWS_IAM, ALLOW,
        deny_policy_name="RiskAgentPolicyClaudeDeny", iam_role_name=GOVERNED_WORKER_ROLE,
    ),
    # No Titan permission exists in the governed role's IAM ceiling, so it is
    # always denied; a policy that allows it is invalid.
    "titan_embeddings": Capability("titan_embeddings", AWS_IAM, DENY, iam_role_name=GOVERNED_WORKER_ROLE),
}


# ---- Policy document ----

@dataclass(frozen=True)
class Policy:
    version: int
    subject: str
    capabilities: dict          # capability -> ALLOW | DENY (explicit entries only)
    sha256: str

    @property
    def agent_id(self) -> str:
        return SUBJECTS[self.subject]

    def effect(self, capability: str) -> str:
        """Effective effect: absent capabilities are denied."""
        return self.capabilities.get(capability, DENY)

    def raw(self, capability: str) -> str:
        return self.capabilities.get(capability, ABSENT)

    def canonical(self) -> dict:
        return canonical_document(self.version, self.subject, self.capabilities)


def canonical_document(version: int, subject: str, capabilities: dict) -> dict:
    return {
        "policy_version": version,
        "subject": subject,
        "capabilities": dict(sorted(capabilities.items())),
    }


def content_hash(version: int, subject: str, capabilities: dict) -> str:
    """Deterministic over meaning: key order and whitespace don't matter."""
    canonical = json.dumps(canonical_document(version, subject, capabilities),
                           sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def parse_policy(text: str) -> Policy:
    """Parse and fully validate a policy document; raise PolicyError on anything doubtful."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"malformed policy: not JSON ({exc.msg})") from exc
    if not isinstance(doc, dict):
        raise PolicyError("malformed policy: top level must be an object")
    expected = {"policy_version", "subject", "capabilities"}
    if set(doc) != expected:
        raise PolicyError(f"malformed policy: keys must be exactly {sorted(expected)}, got {sorted(doc)}")

    version = doc["policy_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PolicyError("policy_version must be a positive integer")

    subject = doc["subject"]
    if subject not in SUBJECTS:
        raise PolicyError(f"unknown subject {subject!r}")

    capabilities = doc["capabilities"]
    if not isinstance(capabilities, dict):
        raise PolicyError("capabilities must be an object of capability -> effect")
    for name, effect in capabilities.items():
        if name not in CAPABILITIES:
            raise PolicyError(f"unknown capability {name!r}")
        if effect not in EFFECTS:
            raise PolicyError(f"unsupported effect {effect!r} for {name!r} (expected {list(EFFECTS)})")
        if effect == ALLOW and CAPABILITIES[name].ceiling != ALLOW:
            raise PolicyError(f"{name!r} cannot be allowed: beyond the capability ceiling")

    return Policy(version, subject, dict(capabilities), content_hash(version, subject, capabilities))


def load_policy(path: str) -> Policy:
    try:
        with open(path, encoding="utf-8") as f:
            return parse_policy(f.read())
    except OSError as exc:
        raise PolicyError(f"policy source unavailable: {exc}") from exc


# ---- Diff, broadening and tighten-first planning ----

@dataclass(frozen=True)
class CapabilityChange:
    capability: str
    boundary: str
    before: str      # ALLOW | DENY | ABSENT (as written in the previous policy)
    after: str
    broadening: bool  # effective deny/absent -> allow


def diff(current: Policy | None, new: Policy) -> list[CapabilityChange]:
    """Per-capability changes over the whole catalog. Broadening is judged per
    capability: any effective deny/absent -> allow, regardless of totals."""
    changes = []
    for name, cap in sorted(CAPABILITIES.items()):
        before = current.raw(name) if current else ABSENT
        after = new.raw(name)
        if before == after:
            continue
        broadening = new.effect(name) == ALLOW and (current is None or current.effect(name) != ALLOW)
        changes.append(CapabilityChange(name, cap.boundary, before, after, broadening))
    return changes


@dataclass(frozen=True)
class Step:
    order: int
    phase: str            # "tighten" | "restamp" | "broaden"
    boundary: str
    capability: str
    effect: str           # effective effect being materialized
    operation: str        # put_policy_item | attach_deny | detach_deny | none


def check_version(current: Policy | None, new: Policy) -> None:
    if current is not None and new.version <= current.version:
        raise PolicyError(
            f"policy_version must increase: materialized v{current.version}, proposed v{new.version}"
        )


def plan(current: Policy | None, new: Policy, *, approve_broadening: bool = False) -> dict:
    """Ordered materialization plan: every tightening at every boundary first,
    then application re-stamps (unchanged effect, new version), then
    broadenings last. A failure part-way therefore leaves the system no less
    restrictive than either version. This is ordering, not atomicity."""
    check_version(current, new)
    changes = diff(current, new)
    broadening = [c.capability for c in changes if c.broadening]

    tighten, restamp, broaden = [], [], []
    for name, cap in sorted(CAPABILITIES.items()):
        target = new.effect(name)
        was_allowed = current is not None and current.effect(name) == ALLOW
        if cap.boundary == APPLICATION:
            bucket = broaden if (target == ALLOW and not was_allowed) else (
                tighten if (target == DENY and (was_allowed or current is None)) else restamp)
            bucket.append((name, cap, target, "put_policy_item"))
        elif cap.deny_policy_name:
            # attach/detach are idempotent "ensure" operations; the phase
            # reflects whether the effective permission changes.
            if target == DENY:
                (tighten if (was_allowed or current is None) else restamp).append((name, cap, target, "attach_deny"))
            else:
                (broaden if not was_allowed else restamp).append((name, cap, target, "detach_deny"))
        else:
            # No materialization target: the IAM ceiling already denies it.
            restamp.append((name, cap, target, "none"))

    steps, order = [], 0
    for phase, bucket in (("tighten", tighten), ("restamp", restamp), ("broaden", broaden)):
        for name, cap, target, operation in bucket:
            order += 1
            steps.append(Step(order, phase, cap.boundary, name, target, operation))

    return {
        "from_version": current.version if current else None,
        "to_version": new.version,
        "policy_sha256": new.sha256,
        "changes": [c.__dict__ for c in changes],
        "broadening": broadening,
        "requires_approval": bool(broadening),
        "approved": approve_broadening or not broadening,
        "steps": [s.__dict__ for s in steps],
    }


# ---- Materialized application state (authority table, policy# namespace) ----

def policy_item_key(agent_id: str, capability: str) -> str:
    return f"policy#{agent_id}#{capability}"


def policy_key_for_scope(scope: str, capability: str) -> str:
    """Admission helper: agent#<id> emergency scope -> policy#<id>#<capability>."""
    kind, _, agent_id = scope.partition("#")
    if kind != "agent" or not agent_id:
        raise PolicyError(f"unsupported authority scope {scope!r}")
    return policy_item_key(agent_id, capability)


def materialization_key(agent_id: str, version: int) -> str:
    return f"policymat#{agent_id}#v{version:010d}"


def pointer_key(agent_id: str) -> str:
    """Latest-completed pointer: the only way the materializer discovers the
    current baseline (GetItem, never Scan)."""
    return f"policymat#{agent_id}#latest"


APPLICATION_CAPABILITIES = tuple(sorted(n for n, c in CAPABILITIES.items() if c.boundary == APPLICATION))


def policy_snapshot(item: dict | None, key: str) -> dict:
    """Plain view of a materialized policy item. Missing or malformed -> not allow."""
    if item is None:
        return {"key": key, "effect": None, "status": "POLICY_MISSING", "policy_version": None, "policy_sha256": None}
    effect = item.get("effect")
    version = item.get("policy_version")
    sha = item.get("policy_sha256")
    if item.get("kind") != POLICY_ITEM_KIND or effect not in EFFECTS or version is None or not sha:
        return {"key": key, "effect": None, "status": "POLICY_INVALID",
                "policy_version": int(version) if version is not None else None, "policy_sha256": sha}
    return {"key": key, "effect": effect, "status": "POLICY_ALLOW" if effect == ALLOW else "POLICY_DENY",
            "policy_version": int(version), "policy_sha256": sha}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class PolicyStore:
    """Reads/writes materialized policy items in the authority table."""

    def __init__(self, table_name: str, region: str | None = None) -> None:
        self.table_name = table_name
        self._table = boto3.resource(
            "dynamodb", region_name=region or os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
        ).Table(table_name)

    def get(self, key: str) -> dict:
        return policy_snapshot(self._table.get_item(Key={"scope_id": key}, ConsistentRead=True).get("Item"), key)

    def put_capability(self, policy: Policy, capability: str, *, actor: str) -> dict:
        """Write one capability item. Never overwrites a newer materialization;
        re-writing the same version and hash (retry after a partial failure)
        is allowed."""
        key = policy_item_key(policy.agent_id, capability)
        item = {
            "scope_id": key,
            "kind": POLICY_ITEM_KIND,
            "subject": policy.subject,
            "capability": capability,
            "effect": policy.effect(capability),
            "policy_version": policy.version,
            "policy_sha256": policy.sha256,
            "materialized_at": _now(),
            "materialized_by": actor,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(scope_id) OR policy_version < :v "
                    "OR (policy_version = :v AND policy_sha256 = :h)"
                ),
                ExpressionAttributeValues={":v": policy.version, ":h": policy.sha256},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise PolicyError(f"{key} already materialized at a newer version or a different v{policy.version}") from exc
            raise
        return item

    def put_record(self, record: dict) -> None:
        self._table.put_item(Item=record)

    def get_record(self, agent_id: str, version: int) -> dict | None:
        return self._table.get_item(Key={"scope_id": materialization_key(agent_id, version)},
                                    ConsistentRead=True).get("Item")

    def get_pointer(self, agent_id: str) -> dict | None:
        """The latest-completed pointer, validated; None if it doesn't exist."""
        item = self._table.get_item(Key={"scope_id": pointer_key(agent_id)}, ConsistentRead=True).get("Item")
        if item is None:
            return None
        version, sha, key = item.get("policy_version"), item.get("policy_sha256"), item.get("materialization_key")
        if (item.get("kind") != POINTER_KIND or version is None or not sha or not str(sha).startswith("sha256:")
                or key != materialization_key(agent_id, int(version))):
            raise PolicyError(f"malformed latest-completed pointer {pointer_key(agent_id)}")
        return {"policy_version": int(version), "policy_sha256": sha, "materialization_key": key}

    def _newest_item_version(self, agent_id: str) -> int | None:
        """Highest policy_version on the application capability items. Every
        materialization writes all of them, so this reveals the newest attempt."""
        versions = []
        for name in APPLICATION_CAPABILITIES:
            item = self._table.get_item(Key={"scope_id": policy_item_key(agent_id, name)},
                                        ConsistentRead=True).get("Item")
            if item is not None and item.get("policy_version") is not None:
                versions.append(int(item["policy_version"]))
        return max(versions) if versions else None

    def latest_completed(self, agent_id: str) -> dict | None:
        """The latest completed materialization record, via the pointer.

        None only when nothing has ever been materialized (no pointer and no
        materialized items). Fails closed (PolicyError) on a malformed pointer,
        a pointer whose record is missing / not COMPLETED / has a different
        hash, or a missing pointer next to materialized items. Raises
        PointerBehind when a newer materialization COMPLETED but the pointer
        never advanced.
        """
        pointer = self.get_pointer(agent_id)
        newest = self._newest_item_version(agent_id)

        if pointer is None:
            if newest is None:
                return None
            behind = self.get_record(agent_id, newest)
            if behind is not None and behind.get("status") == "completed":
                raise PointerBehind(
                    f"no latest pointer, but v{newest} completed; run policy_cli repair",
                    version=newest, sha256=behind["policy_sha256"], pointer_version=None)
            raise PolicyError(f"no latest pointer but materialized items exist at v{newest}; "
                              "baseline unknown (run policy_cli repair / status)")

        record = self.get_record(agent_id, pointer["policy_version"])
        if record is None or record.get("status") != "completed":
            raise PolicyError(f"latest pointer v{pointer['policy_version']} references a "
                              f"{'missing' if record is None else record.get('status')} materialization")
        if record.get("policy_sha256") != pointer["policy_sha256"] or \
                parse_policy(record["policy_document"]).sha256 != pointer["policy_sha256"]:
            raise PolicyError(f"latest pointer v{pointer['policy_version']} hash does not match its record")

        if newest is not None and newest > pointer["policy_version"]:
            behind = self.get_record(agent_id, newest)
            if behind is not None and behind.get("status") == "completed":
                raise PointerBehind(
                    f"v{newest} completed but the latest pointer is still v{pointer['policy_version']}; "
                    "run policy_cli repair", version=newest, sha256=behind["policy_sha256"],
                    pointer_version=pointer["policy_version"])
            # A newer attempt that never completed (failed/started): the pointer
            # remains the authoritative baseline.
        return record

    def current_policy(self, agent_id: str) -> Policy | None:
        record = self.latest_completed(agent_id)
        if record is None:
            return None
        return parse_policy(record["policy_document"])

    def advance_pointer(self, record: dict) -> dict:
        """Point at a COMPLETED record. Monotonic: never moves backwards; the
        same version and hash is an idempotent no-op; same version with a
        different hash is rejected."""
        if record.get("status") != "completed":
            raise PolicyError("only a completed materialization may advance the pointer")
        agent_id = SUBJECTS[record["subject"]]
        version, sha = int(record["policy_version"]), record["policy_sha256"]
        item = {
            "scope_id": pointer_key(agent_id),
            "kind": POINTER_KIND,
            "subject": record["subject"],
            "policy_version": version,
            "policy_sha256": sha,
            "materialization_key": materialization_key(agent_id, version),
            "updated_at": _now(),
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(scope_id) OR policy_version < :v "
                    "OR (policy_version = :v AND policy_sha256 = :h)"
                ),
                ExpressionAttributeValues={":v": version, ":h": sha},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            current = self.get_pointer(agent_id)
            if current and current["policy_version"] == version:
                raise PolicyError(f"latest pointer is v{version} with a different hash") from exc
            raise PolicyError(
                f"latest pointer is already v{current['policy_version'] if current else '?'}; "
                f"v{version} may not replace it") from exc
        return item

    def repair_pointer(self, agent_id: str) -> dict:
        """Advance a pointer left behind by a crash after COMPLETED. Never
        re-runs steps; only points at a record that is already COMPLETED."""
        try:
            self.latest_completed(agent_id)
        except PointerBehind as behind:
            record = self.get_record(agent_id, behind.version)
            self.advance_pointer(record)
            return {"repaired": True, "from_version": behind.pointer_version, "to_version": behind.version}
        return {"repaired": False, "reason": "pointer already consistent"}


# ---- Materialized AWS state (policy-layer Deny attachment) ----

class IamPolicyLayer:
    """Attaches/detaches policy-layer Deny policies. Never touches emergency Denies."""

    def __init__(self, iam_client, account_id: str) -> None:
        self._iam = iam_client
        self._account_id = account_id

    def policy_arn(self, cap: Capability) -> str:
        return f"arn:aws:iam::{self._account_id}:policy/{cap.deny_policy_name}"

    def is_attached(self, cap: Capability) -> bool:
        response = self._iam.list_attached_role_policies(RoleName=cap.iam_role_name)
        return any(p["PolicyArn"] == self.policy_arn(cap) for p in response.get("AttachedPolicies", []))

    def attach(self, cap: Capability) -> str | None:
        response = self._iam.attach_role_policy(RoleName=cap.iam_role_name, PolicyArn=self.policy_arn(cap))
        return response.get("ResponseMetadata", {}).get("RequestId")

    def detach(self, cap: Capability) -> str | None:
        try:
            response = self._iam.detach_role_policy(RoleName=cap.iam_role_name, PolicyArn=self.policy_arn(cap))
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchEntity":
                return None  # already detached
            raise
        return response.get("ResponseMetadata", {}).get("RequestId")


def materialize(policy: Policy, *, store: PolicyStore, iam: IamPolicyLayer, actor: str,
                approve_broadening: bool = False, source: str | None = None) -> dict:
    """Validate against the materialized version, plan, and apply tighten-first.

    Ordering: versioned record STARTED -> tighten/restamp/broaden steps ->
    versioned record COMPLETED -> latest pointer advances (conditionally,
    monotonically). A failure before COMPLETED leaves the record FAILED and the
    pointer untouched. A crash between COMPLETED and the pointer update is
    detected by latest_completed() (PointerBehind) and repaired by
    repair_pointer() or by re-applying the same version and hash. Raises
    BroadeningNotApproved before any write if the plan broadens without approval.
    """
    try:
        current = store.current_policy(policy.agent_id)
    except PointerBehind as behind:
        # Crash after COMPLETED, before the pointer advanced. Re-applying exactly
        # that version and hash repairs it (steps are idempotent); anything
        # else must repair first.
        if (behind.version, behind.sha256) != (policy.version, policy.sha256):
            raise
        record = store.get_record(policy.agent_id, policy.version)
        store.advance_pointer(record)
        return record
    the_plan = plan(current, policy, approve_broadening=approve_broadening)
    if the_plan["requires_approval"] and not approve_broadening:
        raise BroadeningNotApproved(
            f"v{policy.version} broadens {the_plan['broadening']}; re-run with --approve-broadening"
        )

    record = {
        "scope_id": materialization_key(policy.agent_id, policy.version),
        "kind": MATERIALIZATION_KIND,
        "subject": policy.subject,
        "policy_version": policy.version,
        "policy_sha256": policy.sha256,
        "policy_document": json.dumps(policy.canonical(), sort_keys=True),
        "source": source,
        "from_version": the_plan["from_version"],
        "broadening": the_plan["broadening"],
        "broadening_approved": approve_broadening if the_plan["broadening"] else None,
        "status": "started",
        "started_at": _now(),
        "actor": actor,
        "steps": [],
    }
    store.put_record(record)

    try:
        for step in the_plan["steps"]:
            cap = CAPABILITIES[step["capability"]]
            outcome = {**step, "at": _now()}
            if step["operation"] == "put_policy_item":
                store.put_capability(policy, step["capability"], actor=actor)
            elif step["operation"] == "attach_deny":
                outcome["request_id"] = iam.attach(cap)
                outcome["policy_arn"] = iam.policy_arn(cap)
            elif step["operation"] == "detach_deny":
                outcome["request_id"] = iam.detach(cap)
                outcome["policy_arn"] = iam.policy_arn(cap)
            record["steps"].append(outcome)
    except Exception as exc:
        record.update(status="failed", failed_at=_now(), error=f"{type(exc).__name__}: {exc}")
        store.put_record(record)
        raise

    record.update(status="completed", completed_at=_now())
    store.put_record(record)
    store.advance_pointer(record)
    return record
