"""Export the backend-owned HTTP contract without config, database or worker startup."""
import json
from pathlib import Path
from cloudops.entrypoints.api import create_app

root = Path(__file__).resolve().parents[1]
(root / "contracts/openapi.json").write_text(json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n")
