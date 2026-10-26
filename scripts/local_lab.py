"""One fixed disposable integration project; never selects a host kubectl context."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "cloudops-e01-https",
    "-f",
    "compose.yaml",
    "-f",
    "compose.https.yaml",
    "-f",
    "compose.integration.yaml",
]
