"""One fixed disposable integration project; never selects a host kubectl context."""

import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
# A separate disposable project keeps an existing lab's volumes untouched.
PROJECT = os.environ.get("CLOUDOPS_LAB_PROJECT", "cloudops-e01-https")
RELEASE_BUNDLE = os.environ.get("CLOUDOPS_RELEASE_BUNDLE")
# Owned labs managed by manage-local-lab.py. The E10 S3 lab runs the qualified E07
# recovery sample beside, never inside, the retained general-purpose owned lab.
OWNED_PROJECTS = {
    "cloudops-local-testing-lab": "tmp/local-testing-lab",
    "cloudops-e10-s3-lab": "tmp/e10-s3-lab",
}
RECOVERY_LAB_PROJECT = "cloudops-e10-s3-lab"
# A new owned recovery lab run from verified release bundle images. Keep the pattern in
# step with scripts/release_scenarios.py. It never adopts a source-built lab.
RELEASE_RECOVERY_LAB = re.compile(r"cloudops-release-recovery-[a-z0-9]{1,24}")
RELEASE_LAB = RELEASE_RECOVERY_LAB.fullmatch(PROJECT) is not None
RELEASE_IMAGE_VARIABLES = (
    "RELEASE_BACKEND_IMAGE",
    "RELEASE_DB_IMAGE",
    "RELEASE_HTTPS_IMAGE",
    "RELEASE_KRR_IMAGE",
    "RELEASE_SAMPLE_IMAGE",
    "RELEASE_RECOVERY_HEALTHY_IMAGE",
    "RELEASE_RECOVERY_BAD_IMAGE_IMAGE",
)
if RELEASE_LAB:
    if not RELEASE_BUNDLE:
        raise SystemExit(f"{PROJECT} runs only from a verified CLOUDOPS_RELEASE_BUNDLE.")
    OWNED_PROJECTS[PROJECT] = "tmp/" + PROJECT
OWNED_PROJECT = PROJECT if PROJECT in OWNED_PROJECTS else "cloudops-local-testing-lab"
RECOVERY_LAB = OWNED_PROJECT == RECOVERY_LAB_PROJECT or RELEASE_LAB
LAB_DIR = (
    ROOT / OWNED_PROJECTS[PROJECT] if PROJECT in OWNED_PROJECTS else ROOT / "tmp/e01-integration"
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
if RELEASE_LAB or (RELEASE_BUNDLE and PROJECT not in OWNED_PROJECTS):
    inherited = sorted(name for name in RELEASE_IMAGE_VARIABLES if name in os.environ)
    if inherited:
        raise SystemExit(f"Unset {', '.join(inherited)}; the bundle images.env selects images.")
elif RELEASE_BUNDLE:
    raise SystemExit(
        "The retained owned lab projects build their own images; select a new "
        "cloudops-release-recovery-NAME project for release images."
    )
if PROJECT in OWNED_PROJECTS:
    COMPOSE[2:2] = ["--env-file", str(LAB_DIR / "lab.env")]
    COMPOSE += ["-f", "compose.alertmanager.yaml", "-f", "compose.lab.yaml"]
    if RECOVERY_LAB:
        COMPOSE += ["-f", "compose.lab-recovery.yaml"]
    if RELEASE_LAB:
        bundle = Path(RELEASE_BUNDLE).resolve()
        COMPOSE[4:4] = [
            "--env-file",
            str(bundle / "images.env"),
            "--env-file",
            str(bundle / "lab-images.env"),
        ]
        COMPOSE += ["-f", "compose.lab-release.yaml"]
        os.umask(0o077)
# Optional: run a separate disposable project from verified release bundle images.
elif RELEASE_BUNDLE:
    if PROJECT == "cloudops-e01-https" or not os.environ.get("CLOUDOPS_LAB_PROJECT"):
        raise SystemExit(
            "Set CLOUDOPS_LAB_PROJECT to a new project for release images; the default and "
            "retained lab projects build their own images."
        )
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
        COMPOSE += ["-f", "compose.restore-admin.yaml", "-f", "compose.release-restore.yaml"]
elif os.environ.get("CLOUDOPS_RELEASE_RESTORE"):
    raise SystemExit("CLOUDOPS_RELEASE_RESTORE requires CLOUDOPS_RELEASE_BUNDLE.")
if PROJECT in OWNED_PROJECTS and os.environ.get("CLOUDOPS_RELEASE_RESTORE"):
    raise SystemExit("CLOUDOPS_RELEASE_RESTORE applies only to the disposable E01 release lab.")


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
