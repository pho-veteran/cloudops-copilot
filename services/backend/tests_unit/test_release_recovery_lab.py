"""Owned recovery lab run from a release bundle: selection, overlay and bundle checks.

These tests use no Docker, Kubernetes or PostgreSQL.
"""

import hashlib
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
PROJECT = "cloudops-release-recovery-r1"
pytestmark = pytest.mark.skipif(
    not (SCRIPTS / "local_lab.py").is_file(),
    reason="Host lab tooling is tested from a checkout; runtime images omit host scripts.",
)
LAB_PROBE = (
    "import json, os, sys; sys.path.insert(0, sys.argv[1]); import local_lab; "
    "print(json.dumps({'compose': local_lab.COMPOSE, 'lab_dir': str(local_lab.LAB_DIR), "
    "'owned': sorted(local_lab.OWNED_PROJECTS), 'recovery': local_lab.RECOVERY_LAB, "
    "'release': local_lab.RELEASE_LAB, 'umask': os.umask(0)}))"
)


def clean_env(**values):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CLOUDOPS_", "RELEASE_"))
    }
    env.update({key: value for key, value in values.items() if value is not None})
    return env


def run(argv, tmp_path, **values):
    return subprocess.run(
        argv,
        env=clean_env(**values),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )


def select(tmp_path, **values):
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    base = {"CLOUDOPS_RELEASE_BUNDLE": str(bundle), "CLOUDOPS_LAB_PROJECT": PROJECT}
    base.update(values)
    return run([sys.executable, "-c", LAB_PROBE, str(SCRIPTS)], tmp_path, **base), bundle


def test_release_recovery_project_uses_lab_and_bundle_files_in_order(tmp_path):
    result, bundle = select(tmp_path)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    directory = ROOT / "tmp" / PROJECT
    assert value["lab_dir"] == str(directory)
    assert value["recovery"] is True and value["release"] is True
    assert PROJECT in value["owned"]
    assert value["compose"][:10] == [
        "docker",
        "compose",
        "--env-file",
        str(directory / "lab.env"),
        "--env-file",
        str(bundle.resolve() / "images.env"),
        "--env-file",
        str(bundle.resolve() / "lab-images.env"),
        "-p",
        PROJECT,
    ]
    files = value["compose"][value["compose"].index("-f") :: 2]
    assert files == ["-f"] * len(files)
    assert value["compose"][value["compose"].index("-f") + 1 :: 2] == [
        "compose.yaml",
        "compose.https.yaml",
        "compose.integration.yaml",
        "compose.alertmanager.yaml",
        "compose.lab.yaml",
        "compose.lab-recovery.yaml",
        "compose.lab-release.yaml",
    ]
    assert "compose.release.yaml" not in value["compose"]
    if os.name == "posix":
        assert value["umask"] == 0o077


@pytest.mark.parametrize(
    "values",
    [
        {"CLOUDOPS_RELEASE_BUNDLE": None},
        {"RELEASE_KRR_IMAGE": "cloudops-release-krr:other"},
        {"RELEASE_RECOVERY_HEALTHY_IMAGE": "cloudops-release-recovery_healthy:other"},
        {"RELEASE_BACKEND_IMAGE": "cloudops-release-backend:other"},
        {"CLOUDOPS_RELEASE_RESTORE": "1"},
        {"CLOUDOPS_LAB_PROJECT": "cloudops-e10-s3-lab"},
        {"CLOUDOPS_LAB_PROJECT": "cloudops-local-testing-lab"},
    ],
    ids=[
        "no-bundle",
        "inherited-krr",
        "inherited-recovery",
        "inherited-backend",
        "restore-mode",
        "source-recovery-lab",
        "source-testing-lab",
    ],
)
def test_release_recovery_selection_refuses_ambiguous_inputs(tmp_path, values):
    result, _ = select(tmp_path, **values)
    assert result.returncode != 0
    assert not result.stdout


def test_source_recovery_lab_is_unchanged_without_a_bundle(tmp_path):
    result = run(
        [sys.executable, "-c", LAB_PROBE, str(SCRIPTS)],
        tmp_path,
        CLOUDOPS_LAB_PROJECT="cloudops-e10-s3-lab",
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["release"] is False and value["recovery"] is True
    assert "compose.lab-release.yaml" not in value["compose"]
    assert not any(item.endswith("images.env") for item in value["compose"])


class ComposeLoader(yaml.SafeLoader):
    pass


ComposeLoader.add_constructor("!reset", lambda loader, node: None)
ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))


def test_release_lab_overlay_changes_image_sources_only():
    services = yaml.load((ROOT / "compose.lab-release.yaml").read_text(), Loader=ComposeLoader)[
        "services"
    ]
    backend = "${RELEASE_BACKEND_IMAGE:?Use the release bundle images.env}"
    for name in (
        "migrate",
        "api",
        "worker",
        "observer",
        "executor",
        "executor-init",
        "dispatch-admin",
        "test",
        "lab-audit-init",
    ):
        assert services[name] == {
            "image": backend,
            "build": None,
            "pull_policy": "never",
            "platform": "linux/amd64",
        }
    assert services["https"]["image"].startswith("${RELEASE_HTTPS_IMAGE:?")
    assert services["krr"]["image"].startswith("${RELEASE_KRR_IMAGE:?")
    assert services["db"]["image"].startswith("${RELEASE_DB_IMAGE:?")
    for name in ("sample", "load"):
        assert "image" not in services[name] and services[name]["pull_policy"] == "never"
    for service in services.values():
        assert not {"environment", "volumes", "user", "command", "entrypoint"} & set(service)


def test_release_lab_overlay_replaces_every_project_built_image():
    services = yaml.load((ROOT / "compose.lab-release.yaml").read_text(), Loader=ComposeLoader)[
        "services"
    ]
    built = {
        name
        for file in ("compose.lab.yaml", "compose.lab-recovery.yaml", "compose.integration.yaml")
        for name, service in yaml.load((ROOT / file).read_text(), Loader=ComposeLoader)[
            "services"
        ].items()
        if "CLOUDOPS_LAB_PROJECT" in str(service.get("image", ""))
        or "cloudops-local-" in str(service.get("image", ""))
        or service.get("build")
    }
    assert built
    for name in built:
        assert services[name]["pull_policy"] == "never"
        assert "build" not in services[name] or services[name]["build"] is None
        assert "image" in services[name] or name in {"sample", "load"}


@pytest.fixture
def bundler():
    spec = importlib.util.spec_from_file_location(
        "release_bundle_test", SCRIPTS / "release-bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_bundle(module, folder, *, lab=True):
    revision = "a" * 40
    folder.mkdir()

    def images(names, seed):
        return {
            name: {
                "ref": module.alias(name, revision, "sha256:" + f"{seed}{index}" * 32),
                "id": "sha256:" + f"{seed}{index}" * 32,
            }
            for index, name in enumerate(sorted(names))
        }

    manifest = {
        "schema": module.SCHEMA if lab else module.CORE_SCHEMA,
        "revision": revision,
        "platform": module.PLATFORM,
        "images": images({*module.DOCKERFILES, "db"}, "a"),
        "upstream": {},
    }
    (folder / "source.tar").write_bytes(b"source")
    (folder / "images.tar").write_bytes(b"images")
    (folder / "images.env").write_text(module.image_env(manifest["images"]))
    files = list(module.CORE_FILES)
    if lab:
        manifest["lab_images"] = images(module.LAB_DOCKERFILES, "b")
        (folder / "lab-images.tar").write_bytes(b"lab")
        (folder / "lab-images.env").write_text(module.image_env(manifest["lab_images"]))
        files += module.LAB_FILES
    manifest["sha256"] = {name: module.checksum(folder / name) for name in files}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return manifest


@pytest.mark.parametrize("lab", [False, True], ids=["core-v1", "lab-v2"])
def test_bundle_verifier_accepts_core_and_lab_bundles(bundler, tmp_path, capsys, lab):
    fake_bundle(bundler, tmp_path / "bundle", lab=lab)
    bundler.verify(tmp_path / "bundle", loaded=False)
    assert ("with lab images" in capsys.readouterr().out) is lab


def test_lab_images_env_selects_every_lab_component(bundler, tmp_path):
    manifest = fake_bundle(bundler, tmp_path / "bundle")
    names = {
        line.split("=", 1)[0]
        for line in (tmp_path / "bundle/lab-images.env").read_text().splitlines()
    }
    assert names == {
        "RELEASE_KRR_IMAGE",
        "RELEASE_SAMPLE_IMAGE",
        "RELEASE_RECOVERY_HEALTHY_IMAGE",
        "RELEASE_RECOVERY_BAD_IMAGE_IMAGE",
    }
    source = (SCRIPTS / "local_lab.py").read_text()
    assert all(f'"{name}"' in source for name in names)
    assert set(manifest["lab_images"]) == {
        "krr",
        "sample",
        "recovery_healthy",
        "recovery_bad_image",
    }


@pytest.mark.parametrize(
    "tamper",
    ["lab-env", "lab-tar", "missing-lab-group", "extra-group", "v1-with-lab-files", "bad-ref"],
)
def test_bundle_verifier_rejects_tampered_lab_bundles(bundler, tmp_path, tamper):
    folder = tmp_path / "bundle"
    manifest = fake_bundle(bundler, folder)
    if tamper == "lab-env":
        (folder / "lab-images.env").write_text("RELEASE_KRR_IMAGE=other\n")
        manifest["sha256"]["lab-images.env"] = bundler.checksum(folder / "lab-images.env")
    elif tamper == "lab-tar":
        (folder / "lab-images.tar").write_bytes(b"changed")
    elif tamper == "missing-lab-group":
        del manifest["lab_images"]
    elif tamper == "extra-group":
        manifest["other_images"] = {}
    elif tamper == "v1-with-lab-files":
        manifest["schema"] = bundler.CORE_SCHEMA
    else:
        manifest["lab_images"]["krr"]["ref"] = "cloudops-release-krr:other"
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises((ValueError, KeyError)):
        bundler.verify(folder, loaded=False)


DRIVER_PROBE = (
    "import importlib.util, json, sys; sys.path.insert(0, sys.argv[1]); "
    "spec = importlib.util.spec_from_file_location('driver', sys.argv[1] + '/manage-local-lab.py'); "
    "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
    "manifest, digest = module.release_bundle(); "
    "print(json.dumps({'revision': manifest['revision'], 'digest': digest, "
    "'files': module.FILES, 'directory': str(module.DIRECTORY)}))"
)


@pytest.mark.skipif(os.name != "posix", reason="The owned lab driver requires POSIX locks.")
@pytest.mark.parametrize("tamper", [None, "images-env", "core-only"])
def test_driver_binds_the_bundle_image_selection(bundler, tmp_path, tamper):
    folder = tmp_path / "bundle"
    manifest = fake_bundle(bundler, folder, lab=tamper != "core-only")
    if tamper == "images-env":
        (folder / "images.env").write_text("RELEASE_BACKEND_IMAGE=other\n")
    result = run(
        [sys.executable, "-c", DRIVER_PROBE, str(SCRIPTS)],
        tmp_path,
        CLOUDOPS_RELEASE_BUNDLE=str(folder),
        CLOUDOPS_LAB_PROJECT=PROJECT,
    )
    if tamper:
        assert result.returncode != 0 and not result.stdout
        return
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["revision"] == manifest["revision"]
    assert value["digest"] == hashlib.sha256((folder / "manifest.json").read_bytes()).hexdigest()
    assert value["files"][-2:] == ["compose.lab-recovery.yaml", "compose.lab-release.yaml"]
    assert value["directory"] == str(ROOT / "tmp" / PROJECT)


@pytest.mark.skipif(os.name != "posix", reason="The owned lab driver requires POSIX locks.")
def test_driver_refuses_release_images_for_source_projects(tmp_path):
    result = run(
        [
            sys.executable,
            str(SCRIPTS / "manage-local-lab.py"),
            "--project",
            "cloudops-e10-s3-lab",
            "--directory",
            str(tmp_path / "lab"),
            "status",
        ],
        tmp_path,
        CLOUDOPS_RELEASE_BUNDLE=str(tmp_path),
        CLOUDOPS_LAB_PROJECT="cloudops-e10-s3-lab",
    )
    assert result.returncode != 0
    assert "cloudops-release-recovery-NAME" in result.stderr
    assert not (tmp_path / "lab").exists()


CAMPAIGN_PROBE = (
    "import sys; sys.path[:0] = [sys.argv[1], sys.argv[2]]; import e10_campaign; "
    "print(e10_campaign.OWNED_PROJECT)"
)


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        (None, "cloudops-e10-s3-lab"),
        ("cloudops-e10-s3-lab", "cloudops-e10-s3-lab"),
        (PROJECT, PROJECT),
        ("cloudops-local-testing-lab", "cloudops-e10-s3-lab"),
        ("cloudops-release-recovery-Bad", "cloudops-e10-s3-lab"),
    ],
)
def test_campaign_binds_the_selected_recovery_lab(tmp_path, selected, expected):
    result = run(
        [sys.executable, "-c", CAMPAIGN_PROBE, str(SCRIPTS), str(ROOT / "services/backend/src")],
        tmp_path,
        CLOUDOPS_LAB_PROJECT=selected,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
