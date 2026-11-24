"""Release-bundle selection for the disposable integration lab, without Docker."""

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
    not (SCRIPTS / "local_lab.py").is_file(),
    reason="Host lab tooling is tested from a checkout; runtime images omit host scripts.",
)
PROBE = (
    "import json, os, sys; sys.path.insert(0, sys.argv[1]); import local_lab; "
    "print(json.dumps({'compose': local_lab.COMPOSE, 'lab_dir': str(local_lab.LAB_DIR), "
    "'umask': os.umask(0)}))"
)


def lab(tmp_path, **values):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CLOUDOPS_", "RELEASE_"))
    }
    env.update(values)
    return subprocess.run(
        [sys.executable, "-c", PROBE, str(SCRIPTS)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )


def release(tmp_path, **values):
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    host = tmp_path / "host.env"
    host.write_text("POSTGRES_PASSWORD=test-only\n")
    base = {
        "CLOUDOPS_RELEASE_BUNDLE": str(bundle),
        "CLOUDOPS_HOST_ENV": str(host),
        "CLOUDOPS_LAB_PROJECT": "cloudops-release-lab-test",
    }
    base.update(values)
    return (
        lab(tmp_path, **{key: value for key, value in base.items() if value is not None}),
        bundle,
        host,
    )


def test_default_lab_is_unchanged(tmp_path):
    result = lab(tmp_path)
    assert result.returncode == 0, result.stderr
    compose = json.loads(result.stdout)["compose"]
    assert compose[:4] == ["docker", "compose", "-p", "cloudops-e01-https"]
    assert "--env-file" not in compose
    assert "compose.release.yaml" not in compose and "compose.release-lab.yaml" not in compose


def test_release_bundle_adds_env_files_and_overlays_in_order(tmp_path):
    result, bundle, host = release(tmp_path)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["compose"][:8] == [
        "docker",
        "compose",
        "--env-file",
        str(host.resolve()),
        "--env-file",
        str(bundle.resolve() / "images.env"),
        "-p",
        "cloudops-release-lab-test",
    ]
    assert value["compose"][-4:] == [
        "-f",
        "compose.release.yaml",
        "-f",
        "compose.release-lab.yaml",
    ]
    if os.name == "posix":
        assert value["umask"] == 0o077


@pytest.mark.parametrize(
    "values",
    [
        {"CLOUDOPS_LAB_PROJECT": None},
        {"CLOUDOPS_LAB_PROJECT": "cloudops-e01-https"},
        {"CLOUDOPS_LAB_PROJECT": "cloudops-local-testing-lab"},
        {"CLOUDOPS_LAB_PROJECT": "cloudops-e10-s3-lab"},
        {"CLOUDOPS_HOST_ENV": None},
        {"RELEASE_BACKEND_IMAGE": "cloudops-release-backend:other"},
    ],
    ids=[
        "implicit-project",
        "default-project",
        "owned-project",
        "recovery-lab",
        "no-host-env",
        "inherited-image",
    ],
)
def test_release_bundle_refuses_ambiguous_targets(tmp_path, values):
    result, _, _ = release(tmp_path, **values)
    assert result.returncode != 0
    assert not result.stdout


@pytest.mark.skipif(os.name != "posix", reason="The owned lab driver requires POSIX locks.")
@pytest.mark.parametrize("complete", [False, True], ids=["import-guard", "driver-guard"])
def test_owned_lab_driver_refuses_release_images(tmp_path, complete):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CLOUDOPS_", "RELEASE_"))
    }
    env["CLOUDOPS_RELEASE_BUNDLE"] = str(tmp_path)
    if complete:
        host = tmp_path / "host.env"
        host.write_text("POSTGRES_PASSWORD=test-only\n")
        env.update(CLOUDOPS_LAB_PROJECT="cloudops-release-lab-test", CLOUDOPS_HOST_ENV=str(host))
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "manage-local-lab.py"),
            "--project",
            "cloudops-local-testing-lab",
            "--directory",
            str(tmp_path / "lab"),
            "status",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert ("CLOUDOPS_RELEASE_BUNDLE" if complete else "CLOUDOPS_LAB_PROJECT") in result.stderr
    assert not (tmp_path / "lab").exists()


class ComposeLoader(yaml.SafeLoader):
    pass


ComposeLoader.add_constructor("!reset", lambda loader, node: None)
ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))


def test_release_lab_overlay_runs_bundle_images_only():
    overlay = yaml.load((ROOT / "compose.release-lab.yaml").read_text(), Loader=ComposeLoader)
    services = overlay["services"]
    for name in ("executor", "executor-init"):
        assert (
            services[name]["image"] == "${RELEASE_BACKEND_IMAGE:?Use the release bundle images.env}"
        )
        assert services[name]["build"] is None
        assert services[name]["pull_policy"] == "never"
        assert services[name]["platform"] == "linux/amd64"
        assert "volumes" not in services[name] and "environment" not in services[name]
    for name in ("migrate", "api", "worker"):
        assert services[name] == {"environment": {"COLLECTION_SOURCE_MODE": "live_kubernetes"}}


@pytest.mark.parametrize(
    ("project", "directory", "recovery"),
    [
        ("cloudops-local-testing-lab", "tmp/local-testing-lab", False),
        ("cloudops-e10-s3-lab", "tmp/e10-s3-lab", True),
    ],
)
def test_owned_labs_use_separate_directories_and_overlays(tmp_path, project, directory, recovery):
    result = lab(tmp_path, CLOUDOPS_LAB_PROJECT=project)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["lab_dir"] == str(ROOT / directory)
    assert value["compose"][2:6] == ["--env-file", str(ROOT / directory / "lab.env"), "-p", project]
    assert ("compose.lab-recovery.yaml" in value["compose"]) is recovery


def test_owned_lab_images_are_named_by_project():
    overlay = yaml.load((ROOT / "compose.lab.yaml").read_text(), Loader=ComposeLoader)
    for name in ("api", "worker", "executor", "migrate", "https"):
        assert overlay["services"][name]["image"].startswith(
            "${CLOUDOPS_LAB_PROJECT:-cloudops-local-testing-lab}-"
        )
    recovery = yaml.safe_load((ROOT / "compose.lab-recovery.yaml").read_text())
    for name in ("api", "worker", "executor"):
        service = recovery["services"][name]
        assert service["environment"]["RECOVERY_POLICY_FILE"].startswith("/run/cloudops-recovery/")
        assert service["volumes"] == [
            "${CLOUDOPS_LAB_DIR:?Use generated lab.env}/recovery:/run/cloudops-recovery:ro"
        ]
