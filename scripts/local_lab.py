"""One fixed disposable integration project; never selects a host kubectl context."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A separate disposable project keeps an existing lab's volumes untouched.
PROJECT = os.environ.get("CLOUDOPS_LAB_PROJECT", "cloudops-e01-https")
OWNED_PROJECT = "cloudops-local-testing-lab"
LAB_DIR = (
    ROOT / "tmp/local-testing-lab" if PROJECT == OWNED_PROJECT else ROOT / "tmp/e01-integration"
)
COMPOSE = [
    "docker",
    "compose",
    "-p",
    PROJECT,
    "-f",
    "compose.yaml",
    "-f",
    "compose.https.yaml",
    "-f",
    "compose.integration.yaml",
]
if PROJECT == OWNED_PROJECT:
    COMPOSE[2:2] = ["--env-file", str(LAB_DIR / "lab.env")]
    COMPOSE += ["-f", "compose.alertmanager.yaml", "-f", "compose.lab.yaml"]
# Optional: run a separate disposable project from verified release bundle images.
RELEASE_BUNDLE = os.environ.get("CLOUDOPS_RELEASE_BUNDLE")
if RELEASE_BUNDLE:
    if PROJECT in (OWNED_PROJECT, "cloudops-e01-https") or not os.environ.get(
        "CLOUDOPS_LAB_PROJECT"
    ):
        raise SystemExit(
            "Set CLOUDOPS_LAB_PROJECT to a new project for release images; the default and "
            "retained lab projects build their own images."
        )
    inherited = sorted(
        name
        for name in ("RELEASE_BACKEND_IMAGE", "RELEASE_DB_IMAGE", "RELEASE_HTTPS_IMAGE")
        if name in os.environ
    )
    if inherited:
        raise SystemExit(f"Unset {', '.join(inherited)}; the bundle images.env selects images.")
    HOST_ENV = os.environ.get("CLOUDOPS_HOST_ENV")
    if not HOST_ENV:
        raise SystemExit("Set CLOUDOPS_HOST_ENV to the private host.env for this release project.")
    # Generated reader, writer and dispatch credentials stay owner-only.
    os.umask(0o077)
    COMPOSE[2:2] = [
        "--env-file",
        str(Path(HOST_ENV).resolve()),
        "--env-file",
        str(Path(RELEASE_BUNDLE).resolve() / "images.env"),
    ]
    COMPOSE += ["-f", "compose.release.yaml", "-f", "compose.release-lab.yaml"]
    # Restore qualification: every connection uses the project's owned restore database.
    if os.environ.get("CLOUDOPS_RELEASE_RESTORE") == "1":
        COMPOSE += ["-f", "compose.release-restore.yaml"]
elif os.environ.get("CLOUDOPS_RELEASE_RESTORE"):
    raise SystemExit("CLOUDOPS_RELEASE_RESTORE requires CLOUDOPS_RELEASE_BUNDLE.")


def private_write(path, value):
    """Replace a private generated file without following a destination symlink."""
    import tempfile

    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError("Generated paths must not be symlinks.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
