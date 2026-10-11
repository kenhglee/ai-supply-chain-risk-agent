"""Operator CLI for runtime authority. Not used by the agent itself.

    python -m app.governance.authority_cli show
    python -m app.governance.authority_cli revoke --reason "..."
    python -m app.governance.authority_cli restore --reason "..."

Uses AUTHORITY_TABLE and the caller's own AWS credentials. Prints one JSON
line with the resulting authority state.
"""
import argparse
import getpass
import json
import os
import sys

from app.governance.authority import AGENT_SCOPE, AuthorityStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="authority_cli", description=__doc__.splitlines()[0])
    parser.add_argument("--scope", default=AGENT_SCOPE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    for name in ("revoke", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--reason", required=True)
        command.add_argument("--actor", default=None, help="recorded as updated_by (default: OS user)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    table_name = os.environ.get("AUTHORITY_TABLE")
    if not table_name:
        raise RuntimeError("AUTHORITY_TABLE is not configured")
    store = AuthorityStore(table_name)

    if args.command == "show":
        state = store.get(args.scope)
    else:
        actor = args.actor or getpass.getuser()
        change = store.revoke if args.command == "revoke" else store.restore
        state = change(args.scope, reason=args.reason, actor=actor)

    print(json.dumps({"command": args.command, **state}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
