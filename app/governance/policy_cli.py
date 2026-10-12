"""Policy materializer CLI (Phase 3F). Not used by the agent itself.

    python -m app.governance.policy_cli validate <policy.json>
    python -m app.governance.policy_cli plan     <policy.json> [--approve-broadening]
    python -m app.governance.policy_cli apply    <policy.json> [--approve-broadening] [--actor NAME]
    python -m app.governance.policy_cli status
    python -m app.governance.policy_cli repair

validate and plan never write. apply materializes tighten-first into the
authority table (policy#... items) and the policy-layer IAM Deny attachments,
recording a durable materialization record and then advancing the
latest-completed pointer (policymat#<agent>#latest). It never touches the
operator's emergency authority item or emergency Deny. repair advances a
pointer left behind by a crash after COMPLETED; it never re-runs steps.

Exit codes: 0 ok; 2 invalid policy (or stale version); 3 broadening not
approved; 1 materialization failed part-way (see the failed record).

ENV: AUTHORITY_TABLE (required for plan/apply/status), AWS_ACCOUNT_ID
(optional; otherwise sts:GetCallerIdentity), AWS_DEFAULT_REGION.
"""
import argparse
import getpass
import json
import os
import sys

import boto3

from app.governance.authority import AGENT_ID
from app.governance.policy import (
    APPLICATION,
    CAPABILITIES,
    BroadeningNotApproved,
    IamPolicyLayer,
    PointerBehind,
    PolicyError,
    PolicyStore,
    load_policy,
    materialize,
    plan,
    policy_item_key,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_NEEDS_APPROVAL = 3


def build_backends() -> tuple[PolicyStore, IamPolicyLayer]:
    """Real AWS backends; tests replace this function."""
    table_name = os.environ.get("AUTHORITY_TABLE")
    if not table_name:
        raise RuntimeError("AUTHORITY_TABLE is not configured")
    account_id = os.environ.get("AWS_ACCOUNT_ID") or boto3.client("sts").get_caller_identity()["Account"]
    return PolicyStore(table_name), IamPolicyLayer(boto3.client("iam"), account_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="policy_cli", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate").add_argument("policy")
    for name in ("plan", "apply"):
        command = commands.add_parser(name)
        command.add_argument("policy")
        command.add_argument("--approve-broadening", action="store_true")
        if name == "apply":
            command.add_argument("--actor", default=None)
    commands.add_parser("status")
    commands.add_parser("repair")
    return parser


def _emit(record: dict) -> None:
    print(json.dumps(record, default=str))


def status(store: PolicyStore, iam: IamPolicyLayer) -> dict:
    pointer_state, pointer_error = "consistent", None
    try:
        current = store.current_policy(AGENT_ID)
    except PointerBehind as behind:
        current, pointer_state, pointer_error = None, "pointer_behind", str(behind)
    except PolicyError as exc:
        current, pointer_state, pointer_error = None, "inconsistent", str(exc)
    capabilities = {}
    mismatches = []
    for name, cap in sorted(CAPABILITIES.items()):
        desired = current.effect(name) if current else None
        if cap.boundary == APPLICATION:
            item = store.get(policy_item_key(AGENT_ID, name))
            observed = {"effect": item["effect"], "status": item["status"],
                        "policy_version": item["policy_version"], "policy_sha256": item["policy_sha256"]}
            in_sync = current is not None and item["effect"] == desired and item["policy_version"] == current.version \
                and item["policy_sha256"] == current.sha256
        elif cap.deny_policy_name:
            attached = iam.is_attached(cap)
            observed = {"deny_attached": attached, "policy_arn": iam.policy_arn(cap)}
            in_sync = current is not None and attached == (desired != "allow")
        else:
            observed = {"materialization": "none (IAM ceiling denies)"}
            in_sync = desired in (None, "deny")
        if not in_sync:
            mismatches.append(name)
        capabilities[name] = {"boundary": cap.boundary, "desired": desired, "observed": observed, "in_sync": in_sync}
    if pointer_state != "consistent":
        mismatches.append("latest_pointer")
    return {
        "command": "status",
        "pointer_state": pointer_state,
        "pointer_error": pointer_error,
        "materialized_version": current.version if current else None,
        "materialized_sha256": current.sha256 if current else None,
        "capabilities": capabilities,
        "mismatches": mismatches,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "validate":
        try:
            policy = load_policy(args.policy)
        except PolicyError as exc:
            _emit({"command": "validate", "valid": False, "error": str(exc)})
            return EXIT_INVALID
        _emit({"command": "validate", "valid": True, "policy_version": policy.version,
               "policy_sha256": policy.sha256, "subject": policy.subject,
               "capabilities": {n: policy.effect(n) for n in sorted(CAPABILITIES)}})
        return EXIT_OK

    store, iam = build_backends()

    if args.command == "status":
        _emit(status(store, iam))
        return EXIT_OK

    if args.command == "repair":
        try:
            _emit({"command": "repair", **store.repair_pointer(AGENT_ID)})
        except PolicyError as exc:
            _emit({"command": "repair", "repaired": False, "error": str(exc)})
            return EXIT_INVALID
        return EXIT_OK

    try:
        policy = load_policy(args.policy)
        current = store.current_policy(policy.agent_id)
        the_plan = plan(current, policy, approve_broadening=args.approve_broadening)
    except PolicyError as exc:
        _emit({"command": args.command, "valid": False, "error": str(exc)})
        return EXIT_INVALID

    if args.command == "plan":
        _emit({"command": "plan", **the_plan})
        return EXIT_OK

    try:
        record = materialize(policy, store=store, iam=iam, actor=args.actor or getpass.getuser(),
                             approve_broadening=args.approve_broadening, source=args.policy)
    except BroadeningNotApproved as exc:
        _emit({"command": "apply", "applied": False, "error": str(exc), **the_plan})
        return EXIT_NEEDS_APPROVAL
    except PolicyError as exc:
        _emit({"command": "apply", "applied": False, "error": str(exc)})
        return EXIT_INVALID
    except Exception as exc:  # partial failure: the failed record holds completed steps
        _emit({"command": "apply", "applied": False, "error": f"{type(exc).__name__}: {exc}"})
        return EXIT_FAILED
    _emit({"command": "apply", "applied": True, **record})
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
