"""Build or verify the pinned Linux/amd64 Compose and HTTPS application bundle.

Bundles also carry the owned recovery lab images (KRR runner, sample and recovery
variants) built from the same revision. Version 1 bundles hold the core images only.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "linux/amd64"
CORE_SCHEMA = "CloudOpsReleaseBundle/v1"
SCHEMA = "CloudOpsReleaseBundle/v2"
DOCKERFILES = {
    "backend": "services/backend/Dockerfile",
    "https": "apps/web/Dockerfile.https",
}
# Owned lab images: component -> (Dockerfile, build arguments). Upstream service images
# (K3s, Prometheus, Alertmanager, kube-state-metrics) stay pinned by digest in the
# reviewed Compose and manifest files and are pulled by that digest.
LAB_DOCKERFILES = {
    "krr": ("services/krr/Dockerfile", {}),
    "sample": ("config/lab/Dockerfile.sample", {}),
    "recovery_healthy": ("config/lab/Dockerfile.recovery", {"SAMPLE_VARIANT": "healthy"}),
    "recovery_bad_image": ("config/lab/Dockerfile.recovery", {"SAMPLE_VARIANT": "bad-image"}),
}
CORE_FILES = ("source.tar", "images.tar", "images.env")
LAB_FILES = ("lab-images.tar", "lab-images.env")


def run(*args):
    return subprocess.check_output(args, text=True, cwd=ROOT).strip()


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def image_id(ref):
    items = json.loads(run("docker", "image", "inspect", ref))
    require(len(items) == 1, "Expected one image for " + ref)
    image = items[0]
    require(
        f"{image['Os']}/{image['Architecture']}" == PLATFORM
        and re.fullmatch(r"sha256:[0-9a-f]{64}", image["Id"]),
        "Unexpected image platform or identity for " + ref,
    )
    return image["Id"]


def pinned(ref, upstream):
    if ref not in upstream:
        value = json.loads(
            run("docker", "buildx", "imagetools", "inspect", "--format", "{{json .Manifest}}", ref)
        )["digest"]
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", value), "Missing registry digest for " + ref)
        name, sep, prior = ref.partition("@")
        require(not sep or prior == value, "Registry digest mismatch for " + ref)
        upstream[ref] = f"{name}@{value}"
    return upstream[ref]


def alias(component, revision, identity):
    return f"cloudops-release-{component}:{revision}-{identity.removeprefix('sha256:')}"


def image_env(images):
    return "".join(
        f"RELEASE_{component.upper()}_IMAGE={item['ref']}\n"
        for component, item in sorted(images.items())
    )


def lock_dockerfile(path, upstream):
    pattern = r"(?m)^(FROM )([^\s]+)((?: AS [A-Za-z][A-Za-z0-9_-]*)?)$"
    source = path.read_text()
    locked, count = re.subn(
        pattern, lambda match: match[1] + pinned(match[2], upstream) + match[3], source
    )
    require(
        count > 0 and count == len(re.findall(r"(?im)^\s*from\s+", source)),
        "Unsupported FROM instruction in " + str(path.name),
    )
    path.write_text(locked)


def build_image(context, dockerfile, arguments, component, revision):
    staging = f"cloudops-release-stage:{uuid4().hex}"
    subprocess.run(
        [
            "docker",
            "buildx",
            "build",
            "--load",
            "--pull",
            "--platform",
            PLATFORM,
            "--label",
            f"org.opencontainers.image.revision={revision}",
            *(
                argument
                for name, value in sorted(arguments.items())
                for argument in ("--build-arg", f"{name}={value}")
            ),
            "--file",
            str(context / dockerfile),
            "--tag",
            staging,
            str(context),
        ],
        check=True,
    )
    identity = image_id(staging)
    ref = alias(component, revision, identity)
    run("docker", "image", "tag", staging, ref)
    run("docker", "image", "rm", staging)
    return {"ref": ref, "id": identity}


def build(revision, output):
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Use the full reviewed Git commit SHA.")
    require(run("git", "rev-parse", f"{revision}^{{commit}}") == revision, "Unknown commit.")
    for name in ("compose.release.yaml", "compose.lab-release.yaml", "scripts/release-bundle.py"):
        require(
            run("git", "cat-file", "-t", f"{revision}:{name}") == "blob",
            "Build a reviewed revision containing the release package.",
        )
    output.mkdir(mode=0o700)
    archive = output / "source.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", "-o", str(archive), revision], cwd=ROOT, check=True
    )
    upstream, images, lab_images = {}, {}, {}
    with TemporaryDirectory(prefix="cloudops-release-build-") as tmp:
        context = Path(tmp)
        with tarfile.open(archive) as source:
            source.extractall(context, filter="data")
        for dockerfile in {*DOCKERFILES.values(), *(item[0] for item in LAB_DOCKERFILES.values())}:
            lock_dockerfile(context / dockerfile, upstream)
        for component, dockerfile in DOCKERFILES.items():
            images[component] = build_image(context, dockerfile, {}, component, revision)
        for component, (dockerfile, arguments) in LAB_DOCKERFILES.items():
            lab_images[component] = build_image(context, dockerfile, arguments, component, revision)
        db_source = re.search(
            r"(?m)^  db:\n    image: (\S+)\n", (context / "compose.yaml").read_text()
        )
        require(db_source is not None, "Cannot find the base Compose database image.")
        db = pinned(db_source[1], upstream)
        run("docker", "pull", "--platform", PLATFORM, db)
        identity = image_id(db)
        ref = alias("db", revision, identity)
        run("docker", "image", "tag", db, ref)
        images["db"] = {"ref": ref, "id": identity}

    run(
        "docker",
        "image",
        "save",
        "--output",
        str(output / "images.tar"),
        *(item["ref"] for item in images.values()),
    )
    (output / "images.env").write_text(image_env(images))
    run(
        "docker",
        "image",
        "save",
        "--output",
        str(output / "lab-images.tar"),
        *(item["ref"] for item in lab_images.values()),
    )
    (output / "lab-images.env").write_text(image_env(lab_images))
    manifest = {
        "schema": SCHEMA,
        "revision": revision,
        "platform": PLATFORM,
        "images": images,
        "lab_images": lab_images,
        "upstream": upstream,
        "sha256": {name: checksum(output / name) for name in (*CORE_FILES, *LAB_FILES)},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def verify(bundle, loaded):
    manifest = json.loads((bundle / "manifest.json").read_text())
    revision = manifest["revision"]
    lab = manifest.get("schema") == SCHEMA
    groups = {
        "images": {*DOCKERFILES, "db"},
        **({"lab_images": set(LAB_DOCKERFILES)} if lab else {}),
    }
    require(
        manifest["schema"] in {CORE_SCHEMA, SCHEMA}
        and manifest["platform"] == PLATFORM
        and re.fullmatch(r"[0-9a-f]{40}", revision)
        and set(manifest) - {"upstream"} == {"schema", "revision", "platform", "sha256", *groups}
        and all(set(manifest[group]) == names for group, names in groups.items())
        and set(manifest["sha256"]) == {*CORE_FILES, *(LAB_FILES if lab else ())},
        "Invalid release manifest.",
    )
    for name, digest in manifest["sha256"].items():
        require(checksum(bundle / name) == digest, "Checksum mismatch for " + name)
    for group, env in (("images", "images.env"), ("lab_images", "lab-images.env")):
        if group not in groups:
            continue
        require(
            (bundle / env).read_text() == image_env(manifest[group]),
            "Compose references do not match the manifest.",
        )
        for name, item in manifest[group].items():
            require(
                re.fullmatch(r"sha256:[0-9a-f]{64}", item["id"])
                and item["ref"] == alias(name, revision, item["id"]),
                "Invalid image identity for " + name,
            )
            if loaded:
                require(
                    image_id(item["ref"]) == item["id"],
                    "Loaded image differs from manifest for " + name,
                )
    print(
        f"Verified {revision} {PLATFORM}"
        + (" with lab images" if lab else "")
        + (" and loaded image IDs" if loaded else "")
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--revision", required=True)
    builder.add_argument("--output", type=Path, required=True)
    verifier = commands.add_parser("verify")
    verifier.add_argument("bundle", type=Path)
    verifier.add_argument("--loaded", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        build(args.revision, args.output.absolute())
    else:
        verify(args.bundle, args.loaded)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Release bundle: {error}") from None
