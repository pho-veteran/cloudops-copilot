"""Regenerate #71 fixtures from the real producer and owned behavioral tests."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owned-test-lab", action="store_true", required=True)
    args = parser.parse_args()
    if not args.owned_test_lab or not urlsplit(
        os.environ.get("TEST_DATABASE_URL", "")
    ).path.endswith("_test"):
        parser.error(
            "Set TEST_DATABASE_URL to an owned database ending in _test; tests clear its data."
        )
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["DIAGNOSIS_FIXTURE_OUTPUT"] = str(root / "contracts/fixtures/diagnosis")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "services/backend/tests/test_diagnosis_lifecycle.py",
        ],
        cwd=root,
        env=environment,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
