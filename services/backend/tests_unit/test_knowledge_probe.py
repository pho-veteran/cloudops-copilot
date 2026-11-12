"""The lifecycle acceptance probe must retain every assertion before touching a lab."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("flags", "optimization"),
    [(["-O"], ""), (["-OO"], ""), ([], "1")],
)
def test_lifecycle_probe_rejects_optimization_before_external_operations(
    tmp_path, flags, optimization
):
    probe = Path(__file__).resolve().parents[3] / "scripts/probe-knowledge-lifecycle.py"
    program = """
import runpy
import sys
from unittest.mock import patch
probe, output = sys.argv[1:]
sys.argv = [probe, '--container', 'unused', '--output', output]
with patch('subprocess.run', side_effect=RuntimeError('unexpected external operation')):
    runpy.run_path(probe, run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, *flags, "-c", program, str(probe), str(tmp_path / "receipt.json")],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "DATABASE_URL": "postgresql+psycopg://unused@remote.invalid/production",
            "PYTHONOPTIMIZE": optimization,
        },
    )
    assert result.returncode == 2
    assert "requires Python assertions" in result.stderr
    assert "unexpected external operation" not in result.stderr
    assert not (tmp_path / "receipt.json").exists()
