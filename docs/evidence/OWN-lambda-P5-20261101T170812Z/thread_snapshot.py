"""Read-only durable snapshot of one thread (evidence tooling; not part of the repo).

usage: python thread_snapshot.py <thread_id> <label> <out_dir>
Writes <out_dir>/snapshot-<label>.json. Every AWS call is checked by the
ownership preflight's read-only guard (non-read calls raise)."""
import datetime as dt, json, sys
from pathlib import Path
REPO = Path.home() / "projects/supplychain-ai-agent"
sys.path[:0] = [str(REPO), str(REPO / "scripts")]
from ownership_preflight import enforce_read_only
ops = []; enforce_read_only(ops)
import boto3
from boto3.dynamodb.conditions import Attr, Key
from app.storage.checkpoint_store import DynamoCheckpointSaver

thread, label, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
ddb = boto3.resource("dynamodb")
items = ddb.Table("risk_agent_checkpoints").query(KeyConditionExpression=Key("thread_id").eq(thread), ConsistentRead=True)["Items"]
saver = DynamoCheckpointSaver("risk_agent_checkpoints", "us-west-2")
chain = [{"checkpoint_id": c.config["configurable"]["checkpoint_id"],
          "parent": (c.parent_config or {}).get("configurable", {}).get("checkpoint_id"),
          "step": (c.metadata or {}).get("step"), "lease_epoch": (c.metadata or {}).get("lease_epoch")}
         for c in saver.list({"configurable": {"thread_id": thread, "checkpoint_ns": ""}})]
latest = saver.get_tuple({"configurable": {"thread_id": thread, "checkpoint_ns": ""}})
parents = [c["parent"] for c in chain if c["parent"]]
traces, kw = [], {"FilterExpression": Attr("thread_id").eq(thread), "ConsistentRead": True,
                  "ProjectionExpression": "trace_id, workflow_event, lease_epoch, executor_id, change_type"}
while True:
    r = ddb.Table("risk_traces").scan(**kw); traces += r["Items"]
    if "LastEvaluatedKey" not in r: break
    kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
lam = boto3.client("lambda"); fn = lam.get_function(FunctionName="ai-supply-chain-risk-agent")
snap = {
    "label": label, "taken_at": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"), "thread_id": thread,
    "lease": next((i for i in items if i["sk"] == "lease"), None),
    "final": next((i for i in items if i["sk"] == "final"), None),
    "checkpoint_chain": chain,
    "fork": len(parents) != len(set(parents)),
    "latest": None if latest is None else {"checkpoint_id": latest.config["configurable"]["checkpoint_id"],
        "step": (latest.metadata or {}).get("step"), "lease_epoch": (latest.metadata or {}).get("lease_epoch"),
        "next_branches": sorted(k for k in latest.checkpoint["channel_values"] if k.startswith("branch:")),
        "pending_writes": [[w[0], w[1]] for w in latest.pending_writes or []]},
    "wr_item_count": sum(1 for i in items if i["sk"].startswith("wr#")),
    "traces": sorted(traces, key=lambda t: t["trace_id"]),
    "tsmc_earthquake_row": ddb.Table("supplier_risk_state").get_item(Key={"pk": "TSMC#earthquake"}, ConsistentRead=True).get("Item"),
    "ledger_item": ddb.Table("risk_agent_actions").get_item(Key={"action_id": f"action#{thread}#open_review_ticket"}, ConsistentRead=True).get("Item"),
    "lambda": {"image": fn["Code"]["ResolvedImageUri"].split("@")[1], "state": fn["Configuration"]["State"],
               "governance_env": sorted(k for k in fn["Configuration"]["Environment"]["Variables"] if k in ("AUTHORITY_TABLE", "ACTIONS_TABLE")),
               "reserved_concurrency": lam.get_function_concurrency(FunctionName="ai-supply-chain-risk-agent").get("ReservedConcurrentExecutions", "unreserved")},
    "aws_operations": sorted(set(ops)),
}
out.mkdir(parents=True, exist_ok=True)
(out / f"snapshot-{label}.json").write_text(json.dumps(snap, indent=1, default=str, sort_keys=True))
L = snap["lease"] or {}
print(f"[{label}] lease epoch={L.get('epoch')} state={L.get('state')} owner={str(L.get('owner'))[:8]} | final={'yes' if snap['final'] else 'no'}"
      f" | checkpoints={len(chain)} fork={snap['fork']} latest_step={(snap['latest'] or {}).get('step')}"
      f" | traces={[(t['trace_id'][:8], t['workflow_event']) for t in snap['traces']]}"
      f" | TSMC last_seen={(snap['tsmc_earthquake_row'] or {}).get('last_seen_at')} | ledger={'yes' if snap['ledger_item'] else 'no'}")
