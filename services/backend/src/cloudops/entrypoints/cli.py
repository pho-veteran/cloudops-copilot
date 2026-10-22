import argparse
import sys
from getpass import getpass

from cloudops.features.access.service import provision
from cloudops.features.workloads.service import sync_config
from cloudops.shared.database import BACKEND_ROOT, require_schema, sessions


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
    args = parser.parse_args()
    if args.command == "bootstrap":
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
    require_schema()
    with sessions()() as db:
        if args.command in {"sync-workloads", "bootstrap"}:
            sync_config(db)
            print("Development workload allowlist synchronized; historical identities preserved.")
        else:
            password = (
                sys.stdin.readline().rstrip("\r\n")
                if args.password_stdin
                else getpass("Password (12..256 characters): ")
            )
            provision(db, args.username, password, args.role)
            print("Account provisioned; previous sessions revoked.")


if __name__ == "__main__":
    main()
