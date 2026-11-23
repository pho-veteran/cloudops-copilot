import ast
import subprocess
import sys
from pathlib import Path


def test_evaluation_sources_do_not_import_authority_or_service_entrypoints():
    root = Path(__file__).parents[1] / "src/cloudops/features/evaluation"
    forbidden = {"fastapi", "httpx", "cloudops.shared.jobs", "cloudops.shared.database"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any(
            name == blocked or name.startswith(f"{blocked}.")
            for name in imported
            for blocked in forbidden
        ), path.name


def test_evaluation_runtime_imports_do_not_load_operational_modules():
    backend_root = Path(__file__).parents[1]
    script = """
import importlib
import sys

for module in (
    "cloudops.features.evaluation.rules",
    "cloudops.features.evaluation.scoring",
    "cloudops.features.evaluation.cli",
):
    importlib.import_module(module)

for forbidden in (
    "httpx",
    "sqlalchemy",
    "cloudops.features.diagnosis.service",
    "cloudops.features.diagnosis.transport",
    "cloudops.features.diagnosis.worker",
    "cloudops.shared.database",
    "cloudops.shared.jobs",
    "cloudops.features.recovery",
    "cloudops.features.investigations.service",
    "cloudops.features.knowledge.guidance",
):
    assert not any(
        name == forbidden or name.startswith(forbidden + ".")
        for name in sys.modules
    ), forbidden
"""
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_root,
        check=True,
        capture_output=True,
        text=True,
    )
