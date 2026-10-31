#!/usr/bin/env python3
"""
Read-only preflight for the same-thread ownership rollout.

docs/SAME_THREAD_OWNERSHIP_DESIGN.md §16. Inspects the deployed executor and
inventories checkpointed threads with their expected handling under the owned
lifecycle, without acquiring leases, writing markers or invoking workflows.

READ-ONLY IS ENFORCED: every botocore call is checked before it is sent;
anything but Get*/List*/Describe*/Query/Scan/BatchGet*/TransactGet* raises
ReadOnlyViolation (and the run fails).

USAGE
    AWS_PROFILE=ken-ai-dev AWS_DEFAULT_REGION=us-west-2 \\
        python scripts/ownership_preflight.py [--function ai-supply-chain-risk-agent] [--out preflight.json]

    Tables come from docker/worker.env unless set in the environment
    (CHECKPOINT_TABLE, RISK_STATE_TABLE, RISK_TRACES_TABLE, ACTIONS_TABLE).

OUTPUT
    {"identity", "executor": {...}, "threads": [...], "summary": {...}}
    Each thread is classified as one of:
      owned                 has a lease item (post-rollout)
      adopt_paused          pending tasks: adopted and continued on the next RESUME
      adopt_parked_legacy   parked at act with no assessment (legacy_unassessed)
      action_in_flight      its action is IN_FLIGHT: RESUME reports action_in_flight
      already_finalized     has a final marker
      recover_finalization  reached END under the protocol without a marker
      legacy_recognized     completed pre-ownership with E1-E4 evidence: marker only
      completion_unverified completed pre-ownership without enough evidence

EXIT
    0 inventory written; 2 configuration error; 3 read-only violation.
"""
import argparse
import json
import os
import socket
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

READ_PREFIXES = ("Get", "List", "Describe", "Query", "Scan", "BatchGet", "TransactGet")
ENV_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD")


class ReadOnlyViolation(RuntimeError):
    pass


def enforce_read_only(log: list | None = None, setattr_=setattr) -> None:
    """Refuse every non-read AWS operation in this process (setattr_ lets tests undo it)."""
    import botocore.client

    original = botocore.client.BaseClient._make_api_call

    def guarded(client, operation_name, api_params):
        service = client.meta.service_model.service_name
        if log is not None:
            log.append(f"{service}:{operation_name}")
        if not operation_name.startswith(READ_PREFIXES) and not (
                service in ("sso", "sts") and operation_name in ("GetRoleCredentials", "AssumeRole")):
            raise ReadOnlyViolation(f"refused non-read operation {service}:{operation_name}")
        return original(client, operation_name, api_params)

    setattr_(botocore.client.BaseClient, "_make_api_call", guarded)


def _env_from_file() -> None:
    for line in (PROJECT_ROOT / "docker" / "worker.env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
    os.environ.setdefault("ACTIONS_TABLE", "risk_agent_actions")


def _redact_env(env: dict) -> dict:
    return {k: ("<redacted>" if any(m in k.upper() for m in ENV_SECRET_MARKERS) else v) for k, v in env.items()}


# ---- Executor ----

def inspect_executor(function_name: str, region: str) -> dict:
    import boto3
    from botocore.exceptions import ClientError

    lam = boto3.client("lambda", region_name=region)
    out: dict = {"function_name": function_name}
    try:
        fn = lam.get_function(FunctionName=function_name)
    except ClientError as exc:
        return {**out, "error": exc.response["Error"]["Code"]}
    config, code = fn["Configuration"], fn.get("Code", {})
    out.update({
        "arn": config["FunctionArn"], "package_type": config.get("PackageType"),
        "image_uri": code.get("ImageUri"), "resolved_image_uri": code.get("ResolvedImageUri"),
        "last_modified": config.get("LastModified"), "role": config.get("Role"),
        "timeout_s": config.get("Timeout"), "memory_mb": config.get("MemorySize"),
        "environment": _redact_env(config.get("Environment", {}).get("Variables", {})),
    })
    for key, call in (("reserved_concurrency", lambda: lam.get_function_concurrency(FunctionName=function_name)),
                      ("function_url", lambda: lam.get_function_url_config(FunctionName=function_name)),
                      ("event_source_mappings", lambda: lam.list_event_source_mappings(FunctionName=function_name)),
                      ("resource_policy", lambda: json.loads(lam.get_policy(FunctionName=function_name)["Policy"])),
                      ("async_config", lambda: lam.get_function_event_invoke_config(FunctionName=function_name))):
        try:
            value = call()
            if isinstance(value, dict):
                value = {k: v for k, v in value.items() if k != "ResponseMetadata"}
            out[key] = value
        except ClientError as exc:
            out[key] = {"error": exc.response["Error"]["Code"]}

    events = boto3.client("events", region_name=region)
    try:
        rules = events.list_rule_names_by_target(TargetArn=config["FunctionArn"]).get("RuleNames", [])
        out["eventbridge_rules"] = [
            {k: v for k, v in events.describe_rule(Name=r).items() if k in ("Name", "State", "ScheduleExpression")}
            for r in rules]
    except ClientError as exc:
        out["eventbridge_rules"] = {"error": exc.response["Error"]["Code"]}
    try:
        scheduler = boto3.client("scheduler", region_name=region)
        schedules = []
        for page in scheduler.get_paginator("list_schedules").paginate():
            for s in page.get("Schedules", []):
                if s.get("Target", {}).get("Arn", "").startswith(config["FunctionArn"].rsplit(":", 1)[0]):
                    schedules.append({k: s.get(k) for k in ("Name", "GroupName", "State")})
        out["scheduler_schedules"] = schedules
    except ClientError as exc:
        out["scheduler_schedules"] = {"error": exc.response["Error"]["Code"]}

    iam = boto3.client("iam")
    role_name = config["Role"].rsplit("/", 1)[-1]
    try:
        out["role_inline_policies"] = {
            name: iam.get_role_policy(RoleName=role_name, PolicyName=name)["PolicyDocument"]
            for name in iam.list_role_policies(RoleName=role_name)["PolicyNames"]}
        out["role_attached_policies"] = [p["PolicyArn"] for p in
                                         iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]]
    except ClientError as exc:
        out["role_inline_policies"] = {"error": exc.response["Error"]["Code"]}
    return out


# ---- Threads ----

def classify_threads(region: str) -> list[dict]:
    import boto3
    from boto3.dynamodb.conditions import Key

    from app.governance.action_ledger import IN_FLIGHT, UnconfiguredActionGate
    from app.storage.checkpoint_store import DynamoCheckpointSaver
    from app.workflows import risk_workflow
    from app.workflows.supplier_risk_agent import build_app, review_ticket_action_id

    table = boto3.resource("dynamodb", region_name=region).Table(os.environ["CHECKPOINT_TABLE"])
    actions = boto3.resource("dynamodb", region_name=region).Table(os.environ["ACTIONS_TABLE"])
    sks: dict[str, set] = {}
    kwargs = {"ProjectionExpression": "thread_id, sk"}
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            sks.setdefault(item["thread_id"], set()).add(item["sk"])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    saver = DynamoCheckpointSaver(os.environ["CHECKPOINT_TABLE"], region)  # reads only (fenced writes not used)
    reader = type("NoReads", (), {"get": staticmethod(lambda *a: None)})()
    app = build_app(saver, action_gate=UnconfiguredActionGate(), risk_reader=reader)
    threads = []
    for thread_id in sorted(sks):
        keys = sks[thread_id]
        entry = {"thread_id": thread_id, "checkpoints": sum(1 for k in keys if k.startswith("chk#")),
                 "has_lease": "lease" in keys, "has_final": "final" in keys}
        if not entry["checkpoints"]:
            entry["classification"] = "no_checkpoints"
            threads.append(entry)
            continue
        snapshot = app.get_state({"configurable": {"thread_id": thread_id}})
        values, metadata = snapshot.values, snapshot.metadata or {}
        action = actions.get_item(Key={"action_id": review_ticket_action_id(thread_id)},
                                  ConsistentRead=True).get("Item")
        entry.update({
            "next": list(snapshot.next), "pending_tasks": [t.name for t in snapshot.tasks],
            "final_checkpoint_has_lease_epoch": metadata.get("lease_epoch") is not None,
            "action_status": (action or {}).get("status"),
            "supplier": (values.get("alert") or {}).get("supplier"),
            "risk_level": (values.get("alert") or {}).get("risk_level"),
        })
        if entry["has_lease"]:
            lease = table.query(KeyConditionExpression=Key("thread_id").eq(thread_id) & Key("sk").eq("lease"),
                                ConsistentRead=True)["Items"][0]
            entry.update(classification="owned", lease_epoch=int(lease["epoch"]), lease_state=lease["state"])
        elif snapshot.tasks:
            if (action or {}).get("status") == IN_FLIGHT and "act" in snapshot.next:
                entry["classification"] = "action_in_flight"
            elif "act" in snapshot.next and values.get("assessment") is None:
                entry["classification"] = "adopt_parked_legacy"
            else:
                entry["classification"] = "adopt_paused"
        elif entry["has_final"]:
            entry["classification"] = "already_finalized"
        elif entry["final_checkpoint_has_lease_epoch"]:
            entry["classification"] = "recover_finalization"
        else:
            evidence = risk_workflow.legacy_completion_evidence(saver, thread_id, snapshot)
            entry["evidence_reasons"] = evidence["reasons"]
            entry["source_trace_id"] = evidence["source_trace_id"]
            entry["classification"] = "legacy_recognized" if not evidence["reasons"] else "completion_unverified"
        threads.append(entry)
    return threads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--function", default="ai-supply-chain-risk-agent")
    parser.add_argument("--skip-executor", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    _env_from_file()
    for variable in ("CHECKPOINT_TABLE", "RISK_TRACES_TABLE", "ACTIONS_TABLE"):
        if not os.environ.get(variable):
            print(f"{variable} is not set", file=sys.stderr)
            return 2
    os.environ["TRACE_STORE_BACKEND"] = "dynamodb"
    region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
    calls: list = []
    enforce_read_only(calls)
    try:
        import boto3
        identity = boto3.client("sts", region_name=region).get_caller_identity()
        result = {
            "identity": {"account": identity["Account"], "arn": identity["Arn"]},
            "region": region,
            "executor": None if args.skip_executor else inspect_executor(args.function, region),
            "threads": classify_threads(region),
        }
    except ReadOnlyViolation as exc:
        print(f"read-only violation: {exc}", file=sys.stderr)
        return 3
    summary: dict = {}
    for thread in result["threads"]:
        summary[thread["classification"]] = summary.get(thread["classification"], 0) + 1
    result["summary"] = summary
    result["aws_operations"] = sorted(set(calls))
    from scripts.durable_resume_proof import redact_evidence_text
    text = redact_evidence_text(json.dumps(result, indent=2, default=str), hostname=socket.gethostname())
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
