"""Generate or check the single backend-owned HTTP contract, without runtime configuration."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from cloudops.entrypoints.api import create_app

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = (Path("contracts/openapi.json"), Path("apps/web/src/lib/api/generated/schema.d.ts"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="Update committed artifacts; default checks for drift."
    )
    args = parser.parse_args()
    cli = ROOT / "tools/contracts/node_modules/openapi-typescript/bin/cli.js"
    if not cli.is_file():
        parser.error("Run npm ci --prefix tools/contracts before generating contracts.")
    with tempfile.TemporaryDirectory(prefix="cloudops-contract-") as folder:
        temporary = Path(folder)
        schema = temporary / "openapi.json"
        types = temporary / "schema.d.ts"
        schema.write_text(json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n")
        subprocess.run(["node", str(cli), str(schema), "--output", str(types)], check=True)
        drift = []
        for relative, generated in zip(ARTIFACTS, (schema, types), strict=True):
            destination = ROOT / relative
            if args.write:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(generated.read_bytes())
            elif not destination.is_file() or destination.read_bytes() != generated.read_bytes():
                drift.append(str(relative))
        if drift:
            raise SystemExit(
                "Contract drift: "
                + ", ".join(drift)
                + ". Run scripts/contracts.py --write with the backend Python environment, then review producer and consumers."
            )
    print(
        "Contract artifacts updated." if args.write else "Contract artifacts match backend models."
    )


if __name__ == "__main__":
    main()
