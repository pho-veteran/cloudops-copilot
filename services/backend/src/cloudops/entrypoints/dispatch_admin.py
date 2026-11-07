"""Trusted host administration of DispatchPermit/v1. No HTTP route or browser issues permits.

Run only where ``JOB_DISPATCH_ROOT`` is mounted writable, as an allowlisted OS actor that owns
that directory. This entrypoint never reads PostgreSQL and needs no writer credentials.
"""

import argparse
import json
import os
import sys

from cloudops.entrypoints.dispatch import CLASSES
from cloudops.shared.dispatch_permit import (
    PermitError,
    audit_tail,
    bootstrap,
    dispatch_root,
    read_permit,
    transition,
)
from cloudops.shared.dispatch_permit import DispatchGate
from cloudops.entrypoints.dispatch import PERMIT_CLASSES


def trusted_actor(root, *, create: bool) -> dict:
    if os.name == "nt" or not hasattr(os, "geteuid"):
        raise PermitError("Dispatch permit administration requires the Linux deployment host.")
    uid = os.geteuid()
    try:
        allowed = {
            int(item) for item in os.environ.get("JOB_DISPATCH_ADMIN_UIDS", "0").split(",") if item
        }
    except ValueError:
        raise PermitError("JOB_DISPATCH_ADMIN_UIDS must list numeric user IDs.") from None
    if uid not in allowed:
        raise PermitError("This OS account is not an allowlisted dispatch authority actor.")
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o755)
    info = root.stat()
    if info.st_uid != uid or info.st_mode & 0o022:
        raise PermitError(
            "JOB_DISPATCH_ROOT must be owned by the acting account and not group/world-writable."
        )
    return {"uid": uid, "pid": os.getpid(), "channel": "local_os_account"}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Trusted job-dispatch authority (DispatchPermit/v1). No database access."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show the current permit, denial reason and recent audit.")
    for name, text in (
        ("bootstrap", "Issue the first normal permit for every class on a fresh install."),
        ("enter-recovery", "Deny every class before a supported restore (generation +1)."),
        ("resume", "Resume one class while in recovery mode (generation +1)."),
        ("exit-recovery", "Return to normal mode with every class (generation +1)."),
        ("renew", "Extend validity without changing the generation."),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("--reason", required=True)
        command.add_argument("--evidence", action="append", required=True)
        if name == "bootstrap":
            command.add_argument(
                "--fresh-install",
                action="store_true",
                required=True,
                help="Assert this is the supported fresh-install procedure.",
            )
        if name in ("bootstrap", "renew"):
            command.add_argument("--days", type=int, default=30)
        if name == "resume":
            command.add_argument("--class", dest="permit_class", choices=CLASSES, required=True)
    args = parser.parse_args(argv)
    root = dispatch_root()
    try:
        if root is None:
            raise PermitError("Set JOB_DISPATCH_ROOT to the trusted permit directory.")
        if args.command == "status":
            permit, problem = read_permit(root)
            _, kinds, reason = DispatchGate(PERMIT_CLASSES, root).current()
            remaining = None
            if permit:
                from datetime import datetime, timezone

                remaining = (permit.valid_until - datetime.now(timezone.utc)).days
            print(
                json.dumps(
                    {
                        "permit": permit.document() if permit else None,
                        "problem": problem,
                        "dispatchable_job_kinds": sorted(kinds),
                        "denial": reason if not kinds else None,
                        "days_remaining": remaining,
                        "renewal_due": remaining is not None and remaining < 7,
                        "audit": audit_tail(root),
                    },
                    indent=2,
                )
            )
            return
        actor = trusted_actor(root, create=args.command == "bootstrap")
        if args.command == "bootstrap":
            permit = bootstrap(
                root,
                CLASSES,
                reason=args.reason,
                evidence=args.evidence,
                actor=actor,
                days=args.days,
            )
        else:
            operation = args.command.replace("-", "_")
            permit = transition(
                root,
                operation,
                CLASSES,
                reason=args.reason,
                evidence=args.evidence,
                actor=actor,
                resume=getattr(args, "permit_class", None),
                days=getattr(args, "days", None),
            )
        print(json.dumps({"status": "applied", "permit": permit.document()}, indent=2))
    except PermitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
