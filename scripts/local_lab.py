"""One fixed disposable integration project; never selects a host kubectl context."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A separate disposable project keeps an existing lab's volumes untouched.
PROJECT = os.environ.get("CLOUDOPS_LAB_PROJECT", "cloudops-e01-https")
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
