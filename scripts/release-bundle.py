"""Build or verify the pinned Linux/amd64 Compose and HTTPS application bundle."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "linux/amd64"
SCHEMA = "CloudOpsReleaseBundle/v1"
DOCKERFILES = {
    "backend": "services/backend/Dockerfile",
    "https": "apps/web/Dockerfile.https",
}


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


def alias(component, revision):
    return f"cloudops-release-{component}:{revision}"


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


def build(revision, output):
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Use the full reviewed Git commit SHA.")
    require(run("git", "rev-parse", f"{revision}^{{commit}}") == revision, "Unknown commit.")
    output.mkdir(mode=0o700)
    archive = output / "source.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", "-o", str(archive), revision], cwd=ROOT, check=True
    )
    upstream, images = {}, {}
    with TemporaryDirectory(prefix="cloudops-release-build-") as tmp:
        context = Path(tmp)
        with tarfile.open(archive) as source:
            source.extractall(context, filter="data")
        for component, dockerfile in DOCKERFILES.items():
            lock_dockerfile(context / dockerfile, upstream)
            ref = alias(component, revision)
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
                    "--file",
                    str(context / dockerfile),
                    "--tag",
                    ref,
                    str(context),
                ],
                check=True,
            )
            images[component] = {"ref": ref, "id": image_id(ref)}
        db_source = re.search(
            r"(?m)^  db:\n    image: (\S+)\n", (context / "compose.yaml").read_text()
        )
        require(db_source is not None, "Cannot find the base Compose database image.")
        db = pinned(db_source[1], upstream)
        run("docker", "pull", "--platform", PLATFORM, db)
        ref = alias("db", revision)
        run("docker", "image", "tag", db, ref)
        images["db"] = {"ref": ref, "id": image_id(ref)}

    run(
        "docker",
        "image",
        "save",
        "--output",
        str(output / "images.tar"),
        *(item["ref"] for item in images.values()),
    )
    (output / "images.env").write_text(image_env(images))
    manifest = {
        "schema": SCHEMA,
        "revision": revision,
        "platform": PLATFORM,
        "images": images,
        "upstream": upstream,
        "sha256": {
            name: checksum(output / name) for name in ("source.tar", "images.tar", "images.env")
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def verify(bundle, loaded):
    manifest = json.loads((bundle / "manifest.json").read_text())
    revision = manifest["revision"]
    require(
        manifest["schema"] == SCHEMA
        and manifest["platform"] == PLATFORM
        and re.fullmatch(r"[0-9a-f]{40}", revision)
        and set(manifest["images"]) == {*DOCKERFILES, "db"}
        and set(manifest["sha256"]) == {"source.tar", "images.tar", "images.env"},
        "Invalid release manifest.",
    )
    for name, digest in manifest["sha256"].items():
        require(checksum(bundle / name) == digest, "Checksum mismatch for " + name)
    require(
        (bundle / "images.env").read_text() == image_env(manifest["images"]),
        "Compose references do not match the manifest.",
    )
    for name, item in manifest["images"].items():
        ref = alias(name, revision)
        require(
            item["ref"] == ref and re.fullmatch(r"sha256:[0-9a-f]{64}", item["id"]),
            "Invalid image identity for " + name,
        )
        if loaded:
            require(image_id(ref) == item["id"], "Loaded image differs from manifest for " + name)
    print(f"Verified {revision} {PLATFORM}" + (" and loaded image IDs" if loaded else ""))


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
