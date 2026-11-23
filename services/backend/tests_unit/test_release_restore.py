"""Release restore qualification tooling, without Docker, Kubernetes or PostgreSQL."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
pytestmark = pytest.mark.skipif(
    not (SCRIPTS / "probe-release-restore.py").is_file(),
    reason="Host lab tooling is tested from a checkout; runtime images omit host scripts.",
)


@pytest.fixture(scope="module")
def driver():
    spec = importlib.util.spec_from_file_location(
        "release_restore_test", SCRIPTS / "probe-release-restore.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def entry(rows, md5, ids_md5, row_md5=None):
    value = {"rows": rows, "md5": md5, "ids_md5": ids_md5}
    if row_md5 is not None:
        value["row_md5"] = row_md5
    return value


def test_compare_classifies_restored_tables(driver):
    manifest = {
        "fingerprint": {
            "incidents": entry(1, "a", "i1", {"1": "x"}),
            "recovery_executions": entry(1, "b", "e1", {"9": "queued"}),
            "sessions": entry(2, "c", "s1"),
            "baselines": entry(1, "d", "b1"),
            "evidence_pins": entry(1, "e", "p1"),
            "jobs": entry(2, "f", "j2", {"1": "a", "2": "b"}),
        }
    }
    restored = {
        "incidents": entry(1, "a", "i1", {"1": "x"}),
        "recovery_executions": entry(1, "changed", "e1", {"9": "unknown"}),
        "sessions": entry(3, "other", "s2"),
        "baselines": entry(1, "changed", "b1"),
        "jobs": entry(1, "g", "j1", {"1": "a"}),
    }
    tables, _ = driver.compare(manifest, restored, {"fingerprint": {}})
    assert tables["incidents"]["result"] == "identical"
    assert tables["recovery_executions"]["result"].startswith("identifiers retained")
    assert tables["sessions"]["result"].startswith("not compared")
    assert tables["baselines"]["result"] == "unexpected change"
    assert tables["evidence_pins"]["result"] == "missing"
    # A cleanup table that lost an identifier is not explained by cleanup.
    assert tables["jobs"]["result"] == "unexpected change"


def test_compare_reports_inserted_changed_and_cleaned_rows(driver):
    manifest = {"fingerprint": {"recovery_executions": entry(1, "a", "e1", {"9": "queued"})}}
    restored = {"recovery_executions": entry(1, "b", "e1", {"9": "outcome_unknown"})}
    live = {
        "fingerprint": {
            "recovery_executions": {"row_md5": {"9": "acknowledged", "10": "acknowledged"}},
            "incidents": {"row_md5": {"5": "x"}},
        }
    }
    _, rows = driver.compare(manifest, restored, live)
    assert rows["recovery_executions"] == {
        "lost_after_cutoff": ["10"],
        "changed_after_cutoff": ["9"],
        "changed_by_restore_cleanup": ["9"],
        "missing_from_cutoff": [],
    }
    assert rows["incidents"]["lost_after_cutoff"] == ["5"]


def test_written_ignores_status_only_updates(driver):
    base = {"uid": "u", "generation": 3, "replicas": 2, "fence": {}, "resource_version": "10"}
    before = {"checkout": {**base, "ready": 1}}
    status = {"checkout": {**base, "resource_version": "11", "ready": 2}}
    assert driver.written(before, status) == {"checkout": False}
    scaled = {"checkout": {**base, "generation": 4, "replicas": 3}}
    assert driver.written(before, scaled) == {"checkout": True}
    fenced = {"checkout": {**base, "fence": {"cloudops-copilot.local/writer-fence": "n"}}}
    assert driver.written(before, fenced) == {"checkout": True}


def clean_env(**values):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLOUDOPS_", "RELEASE_"))}
    env.update(values)
    return env


def test_driver_refuses_without_the_restore_overlay(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "probe-release-restore.py"), "prepare"],
        env=clean_env(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    expected = "CLOUDOPS_RELEASE_RESTORE=1" if os.name == "posix" else "Linux host or WSL"
    assert expected in result.stderr


def lab_compose(tmp_path, **values):
    probe = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); import local_lab; "
        "print(json.dumps(local_lab.COMPOSE))"
    )
    return subprocess.run(
        [sys.executable, "-c", probe, str(SCRIPTS)],
        env=clean_env(**values),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_restore_overlay_requires_release_mode(tmp_path):
    result = lab_compose(tmp_path, CLOUDOPS_RELEASE_RESTORE="1")
    assert result.returncode != 0
    assert "CLOUDOPS_RELEASE_BUNDLE" in result.stderr


def test_restore_overlays_are_added_last_in_release_mode(tmp_path):
    host = tmp_path / "host.env"
    host.write_text("POSTGRES_PASSWORD=test-only\n")
    result = lab_compose(
        tmp_path,
        CLOUDOPS_RELEASE_BUNDLE=str(tmp_path),
        CLOUDOPS_HOST_ENV=str(host),
        CLOUDOPS_LAB_PROJECT="cloudops-release-restore-test",
        CLOUDOPS_RELEASE_RESTORE="1",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[-4:] == [
        "-f",
        "compose.restore-admin.yaml",
        "-f",
        "compose.release-restore.yaml",
    ]


def test_restore_admin_keeps_the_installation_database():
    admin = yaml.safe_load((ROOT / "compose.restore-admin.yaml").read_text())["services"][
        "restore-admin"
    ]
    assert admin["environment"]["DATABASE_URL"].endswith("@db:5432/cloudops")
    assert admin["profiles"] == ["restore-admin"]
    assert admin["pull_policy"] == "never"
    assert "job_dispatch:/run/cloudops-job-permit:ro" in admin["volumes"]
    assert admin["command"] == ["cloudops.entrypoints.recovery_restore"]


def test_drill_overlay_connects_only_to_the_owned_restore_database():
    services = yaml.safe_load((ROOT / "compose.release-restore.yaml").read_text())["services"]
    assert services["db"]["environment"]["POSTGRES_DB"] == "cloudops_restore_test"
    assert "cloudops_restore_test" in services["db"]["healthcheck"]["test"][-1]
    urls = {
        name: service["environment"]["DATABASE_URL"]
        for name, service in services.items()
        if "DATABASE_URL" in service.get("environment", {})
    }
    assert set(urls) == {
        "migrate",
        "api",
        "worker",
        "observer",
        "executor",
        "restore-admin",
        "test",
    }
    assert all(url.endswith("@db:5432/cloudops_restore_test") for url in urls.values())
    assert services["test"]["environment"]["TEST_DATABASE_URL"].endswith(
        "/cloudops_restore_checks_test"
    )


def test_restore_standby_writer_scope_names_only_the_standby():
    documents = list(yaml.safe_load_all((ROOT / "config/lab/restore-standby.yaml").read_text()))
    role = next(d for d in documents if d["kind"] == "Role")
    assert role["rules"] == [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "resourceNames": ["checkout-standby"],
            "verbs": ["get", "patch"],
        }
    ]
    binding = next(d for d in documents if d["kind"] == "RoleBinding")
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "cloudops-writer", "namespace": "demo"}
    ]
