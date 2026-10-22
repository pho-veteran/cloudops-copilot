"""Prove that unsynchronized backend schema changes fail and regenerated consumers pass."""

import shutil
import sys
import tempfile
from pathlib import Path

import contracts as harness
from fastapi import FastAPI
from pydantic import BaseModel


def main():
    root = harness.ROOT
    with tempfile.TemporaryDirectory(prefix="cloudops-drift-proof-") as folder:
        temporary = Path(folder)
        (temporary / "tools").symlink_to(root / "tools", target_is_directory=True)
        for artifact in harness.ARTIFACTS:
            (temporary / artifact).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / artifact, temporary / artifact)
        harness.ROOT = temporary
        sys.argv = ["contracts.py"]
        harness.main()
        app: FastAPI = harness.create_app()

        class Probe(BaseModel):
            required_field: str

        app.add_api_route(
            "/api/contract-probe",
            lambda: Probe(required_field="probe"),
            response_model=Probe,
            operation_id="contract_probe",
        )
        harness.create_app = lambda: app
        try:
            harness.main()
        except SystemExit as error:
            if "Contract drift:" not in str(error):
                raise
            print("Deliberate backend schema drift rejected.")
        else:
            raise AssertionError("Unsynchronized schema change was accepted.")
        sys.argv = ["contracts.py", "--write"]
        harness.main()
        sys.argv = ["contracts.py"]
        harness.main()
        print("Synchronized schema and TypeScript artifacts accepted; working tree untouched.")


if __name__ == "__main__":
    main()
