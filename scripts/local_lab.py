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
