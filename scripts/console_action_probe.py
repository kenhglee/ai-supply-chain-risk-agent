"""Run one governed mock action for the operations console."""
import json
import socket
import uuid

from scripts.bedrock_authority_probe import (
    act_once,
    build_gate_from_env,
)


def main():
    run_id = uuid.uuid4().hex
    action_id = f"action#console-{run_id}#open_review_ticket"

    result = act_once(
        build_gate_from_env(),
        action_id=action_id,
        thread_id=f"console-{run_id}",
        executor_id=f"{socket.gethostname()}:console-probe",
    )

    print(json.dumps(result))


if __name__ == "__main__":
    main()