import argparse
import sys
from getpass import getpass

from pydantic import ValidationError

from cloudops.features.access.service import provision, revoke_sessions, set_role
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
    sub.add_parser("sync-workloads", help="Synchronize configured development workload identities.")
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
    args = parser.parse_args()
    try:
        run(args)
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
    if args.command == "bootstrap":
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
    require_schema()
    with sessions()() as db:
        if args.command in {"sync-workloads", "bootstrap"}:
            sync_config(db)
            print("Development workload allowlist synchronized; historical identities preserved.")
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
        else:
            revoke_sessions(db, args.username)
            print("Sessions revoked.")


if __name__ == "__main__":
    main()
