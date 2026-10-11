"""Runtime-neutral worker for the checkpointed supplier risk workflow.

Runs START or RESUME from any ordinary Python process or container, using the
same lifecycle as the Lambda handler (app.workflows.risk_workflow):

    python -m app.workers.risk_worker start --headline "..."
    python -m app.workers.risk_worker resume --thread-id thread-<32 hex>

Prints one JSON outcome line to stdout. Exit codes: 0 success, 2 invalid
arguments, 3 unknown thread. A missing CHECKPOINT_TABLE raises (non-zero).
Dependencies are built fresh per run; continuity is DynamoDB + thread_id.
"""
import argparse
import json
import sys

from app.workflows.risk_workflow import (
    NOT_FOUND,
    OUTCOME_FIELDS,
    START_PAUSE_BEFORE,
    THREAD_ID_PATTERN,
    checkpoint_dependencies_from_env,
    resume_workflow,
    start_workflow,
)
from app.workflows.supplier_risk_agent import get_executor_id

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_NOT_FOUND = 3


def _headline(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("headline must be a non-empty string")
    return value.strip()


def _thread_id(value: str) -> str:
    if not THREAD_ID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("thread_id must match thread-<32 lowercase hex>")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="risk_worker", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help=f"start a new workflow, pausing before {START_PAUSE_BEFORE}")
    start.add_argument("--headline", required=True, type=_headline)

    # RESUME takes only the workflow identity; no new workflow input.
    resume = commands.add_parser("resume", help="resume an existing workflow by thread_id")
    resume.add_argument("--thread-id", required=True, type=_thread_id)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse reports usage errors on stderr
        return EXIT_INVALID if exc.code else EXIT_OK

    deps = checkpoint_dependencies_from_env()
    if args.command == "start":
        action = "start"
        outcome = start_workflow({"headline": args.headline}, pause_before=list(START_PAUSE_BEFORE), **deps)
    else:
        action = "resume"
        outcome = resume_workflow(args.thread_id, **deps)

    print(json.dumps({
        "action": action,
        **{field: outcome.get(field) for field in OUTCOME_FIELDS},
        "executor_id": get_executor_id(),
    }))
    return EXIT_NOT_FOUND if outcome["status"] == NOT_FOUND else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
