"""Client for the isolated OS-authenticated maintenance socket; accepts no actor override."""

import argparse
import json
import os
import socket
from pathlib import Path
from uuid import UUID, uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=[
            "status",
            "target_status",
            "enter_recovery",
            "enable_target",
            "release_uncertain_target",
            "exit_recovery_mode",
        ],
    )
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--reason", default="")
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument(
        "--request-id",
        type=UUID,
        default=None,
        help="Reuse an exact request identity to retrieve its durable receipt without repeating maintenance.",
    )
    args = parser.parse_args()
    path = Path(os.environ.get("EXECUTOR_ROOT", "/var/lib/cloudops-executor")) / "maintenance.sock"
    request = {
        "request_id": str(args.request_id or uuid4()),
        "operation": args.operation,
        "targets": args.target,
        "reason": args.reason,
        "evidence": args.evidence,
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(120)
            connection.connect(str(path))
            connection.sendall(json.dumps(request).encode() + b"\n")
            result = json.loads(connection.makefile("rb").readline(128_001))
    except (OSError, ValueError):
        raise SystemExit(
            "Maintenance channel unavailable; no successful state change is claimed."
        ) from None
    print(json.dumps(result, indent=2))
    if result.get("status") in ("denied", "outcome_unknown"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
