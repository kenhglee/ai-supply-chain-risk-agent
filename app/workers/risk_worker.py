"""Runtime-neutral worker for the checkpointed supplier risk workflow.

Runs START or RESUME from any ordinary Python process or container, using the
same lifecycle as the Lambda handler (app.workflows.risk_workflow):

    python -m app.workers.risk_worker start --headline "..."
    python -m app.workers.risk_worker resume --thread-id thread-<32 hex>
    python -m app.workers.risk_worker session

start/resume print one JSON outcome line to stdout. Exit codes: 0 success,
2 invalid arguments, 3 unknown thread, 4 authority denied. A missing
CHECKPOINT_TABLE raises (non-zero). Dependencies are built fresh per run;
continuity is DynamoDB + thread_id.

session is a long-lived executor: one process reads commands from stdin, one
JSON object per line ({"start": "<headline>"} or {"resume": "thread-..."}),
and prints one JSON outcome line per command until EOF. Its dependencies
are built once, so authority is shown to be re-read per privileged action,
not cached per process.
"""
import argparse
import json
import sys

from app.workflows.risk_workflow import (
    AUTHORITY_DENIED,
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
EXIT_AUTHORITY_DENIED = 4


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

    commands.add_parser("session", help="long-lived executor: read start/resume commands from stdin")
    return parser


def _run(command: str, value: str, deps: dict) -> dict:
    if command == "start":
        outcome = start_workflow({"headline": value}, pause_before=list(START_PAUSE_BEFORE), **deps)
    else:
        outcome = resume_workflow(value, **deps)
    return {
        "action": command,
        **{field: outcome.get(field) for field in OUTCOME_FIELDS},
        "executor_id": get_executor_id(),
    }


def _parse_session_line(line: str) -> tuple[str, str]:
    """One command per line: {"start": "<headline>"} or {"resume": "thread-..."}."""
    try:
        command = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON: {exc}") from exc
    if not isinstance(command, dict) or len(command) != 1:
        raise ValueError('expected exactly one of {"start": ...} or {"resume": ...}')
    [(name, value)] = command.items()
    if name == "start":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("headline must be a non-empty string")
        return name, value.strip()
    if name == "resume":
        if not isinstance(value, str) or not THREAD_ID_PATTERN.fullmatch(value):
            raise ValueError("thread_id must match thread-<32 lowercase hex>")
        return name, value
    raise ValueError(f"unknown command {name!r}")


def run_session(lines, deps: dict, out=None) -> int:
    """Process commands until EOF in this one process; bad lines don't end it."""
    out = out or sys.stdout
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            command, value = _parse_session_line(line)
            result = _run(command, value, deps)
        except Exception as exc:  # keep the executor alive; report and continue
            result = {"action": None, "error": f"{type(exc).__name__}: {exc}", "executor_id": get_executor_id()}
        print(json.dumps(result), file=out, flush=True)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse reports usage errors on stderr
        return EXIT_INVALID if exc.code else EXIT_OK

    deps = checkpoint_dependencies_from_env()
    if args.command == "session":
        return run_session(sys.stdin, deps)

    value = args.headline if args.command == "start" else args.thread_id
    result = _run(args.command, value, deps)
    print(json.dumps(result))
    if result["status"] == NOT_FOUND:
        return EXIT_NOT_FOUND
    if result["status"] == AUTHORITY_DENIED:
        return EXIT_AUTHORITY_DENIED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
