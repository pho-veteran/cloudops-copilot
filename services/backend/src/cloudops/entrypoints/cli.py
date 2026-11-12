import argparse
import sys
from getpass import getpass

from pydantic import ValidationError

from cloudops.features.access.service import (
    provision,
    revoke_all_sessions,
    revoke_sessions,
    set_role,
)
from cloudops.features.workloads.service import sync_config
from cloudops.shared.database import BACKEND_ROOT, require_schema, sessions
from cloudops.shared.errors import DomainError


def main():
    parser = argparse.ArgumentParser(
        description="Local foundation maintenance. No executor capability."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "bootstrap", help="Apply migrations and synchronize configured development workloads."
    )
    sync = sub.add_parser(
        "sync-workloads", help="Synchronize host-configured workload scope without cluster writes."
    )
    sync.add_argument(
        "--previous-cluster-fingerprint",
        help="Exact old binding for an explicitly reviewed CA/server rotation; requires unchanged live UIDs.",
    )
    account = sub.add_parser(
        "account", help="Provision/reset a user and revoke their existing sessions."
    )
    account.add_argument("username")
    account.add_argument("--role", choices=["viewer", "operator"], required=True)
    account.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read password from standard input (automation only).",
    )
    account_role = sub.add_parser(
        "account-role", help="Change a user's role and revoke their existing sessions."
    )
    account_role.add_argument("username")
    account_role.add_argument("role", choices=["viewer", "operator"])
    revoke = sub.add_parser("revoke-sessions", help="Revoke all active sessions of one user.")
    revoke.add_argument("username")
    sub.add_parser(
        "revoke-all-sessions",
        help="Trusted restore step: discard every restored browser session before starting the API.",
    )
    purge = sub.add_parser(
        "evidence-purge", help="Report expired unpinned evidence; use --apply to clear content."
    )
    purge.add_argument("--apply", action="store_true", help="Permanently clear eligible content.")
    knowledge_index = sub.add_parser(
        "knowledge-index",
        help="Trusted: capture, validate and atomically activate a runbook corpus.",
    )
    knowledge_index.add_argument(
        "--root", default=".", help="Trusted source root containing runbooks/."
    )
    knowledge_index.add_argument("--json", action="store_true", help="Emit one JSON receipt.")
    knowledge_status = sub.add_parser(
        "knowledge-status", help="Trusted: read the current Knowledge index status."
    )
    knowledge_status.add_argument("--json", action="store_true", help="Emit one JSON status.")
    args = parser.parse_args()
    try:
        code = run(args)
        if code:
            sys.exit(code)
    except ValidationError as exc:
        # Pydantic echoes rejected input values; keep them (and any password) out of the output.
        details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        print(f"error: Invalid input. {details}", file=sys.stderr)
        sys.exit(1)
    except (DomainError, ValueError) as exc:
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        print(f"error: {' '.join(message.split())}", file=sys.stderr)
        sys.exit(1)


def run(args):
    if args.command in {"knowledge-index", "knowledge-status"}:
        from cloudops.features.knowledge.cli import run_knowledge

        return run_knowledge(args)
    if args.command == "bootstrap":
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
    require_schema()
    with sessions()() as db:
        if args.command in {"sync-workloads", "bootstrap"}:
            result = sync_config(db, getattr(args, "previous_cluster_fingerprint", None))
            print(
                f"Workload scope synchronized: {result.current} current, {result.missing} missing, {result.unavailable} unavailable, {result.removed} retired. Historical identities preserved; writer authority unchanged."
            )
            if result.unavailable:
                print(
                    "Partial observation: prior admission is preserved for unreadable identities; inspect scope status and retry explicitly.",
                    file=sys.stderr,
                )
        elif args.command == "evidence-purge":
            from cloudops.features.investigations.retention import purge_expired

            count = purge_expired(db, apply=args.apply)
            print(
                f"{'Purged' if args.apply else 'Would purge'} {count} expired unpinned evidence payloads."
            )
        elif args.command == "account":
            password = (
                sys.stdin.readline().rstrip("\r\n")
                if args.password_stdin
                else getpass("Password (12..256 characters): ")
            )
            provision(db, args.username, password, args.role)
            print("Account provisioned; previous sessions revoked.")
        elif args.command == "account-role":
            set_role(db, args.username, args.role)
            print("Account role updated; previous sessions revoked.")
        elif args.command == "revoke-all-sessions":
            revoke_all_sessions(db)
            print(
                "All browser sessions revoked; users must sign in and read current protected state again."
            )
        else:
            revoke_sessions(db, args.username)
            print("Sessions revoked.")


if __name__ == "__main__":
    main()
