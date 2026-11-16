"""Explicit trusted initialization and an external pin for the isolated writer journal."""

import argparse
import json
import os
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4


def identity_path(root, path=None):
    selected = Path(path or os.environ.get("EXECUTOR_AUTHORITY_PIN", str(root) + ".identity.json"))
    if root.resolve() in selected.resolve().parents:
        raise RuntimeError("Authority identity must be stored outside the journal directory.")
    return selected


def file_identity(path):
    value = path.stat()
    return {"device": value.st_dev, "inode": value.st_ino}


def read_identity(root, path=None):
    path = identity_path(root, path)
    try:
        if path.stat().st_mode & 0o022:
            raise ValueError
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError
        value = json.loads(raw)
        if (
            set(value) != {"schema_version", "deployment_id", "journal", "lock"}
            or value["schema_version"] != "ExecutorAuthority/v1"
        ):
            raise ValueError
        UUID(value["deployment_id"])
        if value["journal"] != file_identity(root / "authority.sqlite") or value[
            "lock"
        ] != file_identity(root / "admission.lock"):
            raise ValueError
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError(
            "Pinned executor authority is missing or changed; admission remains closed. Restore the original authority, never replace it automatically."
        ) from None


def initialize(root, path=None, *, legacy_id=None):
    root = Path(root)
    path = identity_path(root, path)
    database, lock = root / "authority.sqlite", root / "admission.lock"
    if path.exists():
        value = read_identity(root, path)
        with sqlite3.connect(f"file:{database}?mode=rw", uri=True) as db:
            actual = db.execute("SELECT value FROM control WHERE key='deployment_id'").fetchone()
        if not actual or actual[0] != value["deployment_id"]:
            raise RuntimeError("Pinned executor deployment identity does not match its journal.")
        return value
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if database.exists():
        if legacy_id is None or not lock.is_file():
            raise RuntimeError(
                "An existing journal needs an explicit matching legacy deployment ID; missing identity is not a fresh install."
            )
        with sqlite3.connect(f"file:{database}?mode=rw", uri=True) as db:
            row = db.execute("SELECT value FROM control WHERE key='deployment_id'").fetchone()
            pinned_before = db.execute(
                "SELECT value FROM control WHERE key IN ('identity_required', 'schema_version')"
            ).fetchone()
        if pinned_before:
            raise RuntimeError(
                "This authority was already initialized with a required external identity; missing pin cannot be adopted as legacy."
            )
        if not row or row[0] != str(legacy_id):
            raise RuntimeError(
                "Legacy deployment identity does not match; authority was not adopted."
            )
        deployment_id = row[0]
    else:
        if legacy_id is not None:
            raise RuntimeError("Cannot adopt a missing authority journal.")
        deployment_id = str(uuid4())
        lock.touch(exist_ok=False, mode=0o600)
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE control (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO control VALUES ('deployment_id', ?)", (deployment_id,))
            db.execute("INSERT INTO control VALUES ('identity_required', 'true')")
        database.chmod(0o600)
    with sqlite3.connect(f"file:{database}?mode=rw", uri=True) as db:
        db.execute("INSERT OR REPLACE INTO control VALUES ('identity_required', 'true')")
    value = {
        "schema_version": "ExecutorAuthority/v1",
        "deployment_id": deployment_id,
        "journal": file_identity(database),
        "lock": file_identity(lock),
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return value


def main():
    if os.getuid() != 0:
        raise SystemExit(
            "Authority initialization requires the isolated deployment-root invocation."
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--adopt-legacy-id", type=UUID)
    args = parser.parse_args()
    print(json.dumps(initialize(args.root, args.identity_file, legacy_id=args.adopt_legacy_id)))


if __name__ == "__main__":
    main()
