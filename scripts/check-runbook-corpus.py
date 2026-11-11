#!/usr/bin/env python3
"""Author-side RunbookCatalog/v1 preflight.

This validates repository corpus files only. It does not index, retrieve or
activate knowledge and is not a replacement for the later importer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = "RunbookCatalog/v1"
CHECKER_VERSION = "3"
MAX_DOCUMENTS = 100
MAX_FILE_BYTES = 256 * 1024
MAX_CORPUS_BYTES = 8 * 1024 * 1024
MAX_KEY = 64
MAX_VERSION = 64
MAX_TITLE = 200
MAX_SOURCE_PATH = 256
MAX_TAGS = 16
MAX_TAG = 64
SECTION_WARNING_BYTES = 4096
FAMILIES = {
    "crashloop_startup",
    "oom_restarts",
    "traffic_latency_errors",
    "pending_unschedulable",
    "image_pull",
}
TEMPLATES = {
    "restore_deployment_template",
    "set_memory_profile",
    "scale_deployment",
}
EXPECTED_RELEASE = {
    "crashloop-startup": ("diagnosis", "crashloop_startup", ()),
    "oom-restarts": ("diagnosis", "oom_restarts", ()),
    "traffic-latency-errors": ("diagnosis", "traffic_latency_errors", ()),
    "pending-unschedulable": ("diagnosis", "pending_unschedulable", ()),
    "image-pull": ("diagnosis", "image_pull", ()),
    "restore-deployment-template": (
        "recovery_template",
        "crashloop_startup",
        ("restore_deployment_template",),
    ),
    "set-memory-profile": (
        "recovery_template",
        "oom_restarts",
        ("set_memory_profile",),
    ),
    "scale-deployment": (
        "recovery_template",
        "traffic_latency_errors",
        ("scale_deployment",),
    ),
}
ROOT_FIELDS = {"schema_version", "documents"}
DOCUMENT_FIELDS = {
    "document_key",
    "version",
    "title",
    "source_path",
    "guide_kind",
    "tags",
    "applicability",
    "provenance",
}
APPLICABILITY_FIELDS = {"kind", "symptom_families", "recovery_templates"}
PROVENANCE_FIELDS = {"source_revision", "reviewed_at"}
REFERENCE_LINK = re.compile(r"^[ \t]{0,3}\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CorpusError(ValueError):
    pass


@dataclass
class Report:
    release: bool
    documents: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    version_conflicts: list[dict[str, str]] = field(default_factory=list)
    corpus_bytes: int = 0

    @property
    def valid(self) -> bool:
        return not self.errors and not self.version_conflicts


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CorpusError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _load_json(path: Path) -> Any:
    try:
        info = path.stat()
    except OSError as exc:
        raise CorpusError(f"cannot read {path.name}: {exc.strerror}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise CorpusError("catalog must be a regular file")
    if info.st_size > MAX_CORPUS_BYTES:
        raise CorpusError("catalog exceeds the corpus byte cap")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CORPUS_BYTES + 1)
    except OSError as exc:
        raise CorpusError(f"cannot read {path.name}: {exc.strerror}") from exc
    if len(raw) > MAX_CORPUS_BYTES:
        raise CorpusError("catalog exceeds the corpus byte cap")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorpusError("catalog is not valid UTF-8") from exc
    try:
        return json.loads(text, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as exc:
        raise CorpusError(f"catalog is not valid JSON: {exc.msg}") from exc


def _is_reparse(path: Path) -> bool:
    try:
        attrs = os.lstat(path).st_file_attributes
    except AttributeError:
        return path.is_symlink()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return path.is_symlink() or bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _safe_source_path(root: Path, raw: str, issues: list[str]) -> Path | None:
    if not isinstance(raw, str):
        issues.append("source_path must be a string")
        return None
    if not raw or len(raw) > MAX_SOURCE_PATH:
        issues.append("source_path must be non-empty and at most 256 characters")
        return None
    if "\x00" in raw or "\\" in raw:
        issues.append("source_path contains an unsupported separator or control byte")
        return None
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        issues.append("source_path must be a relative path without traversal")
        return None
    if len(pure.parts) < 2 or pure.parts[0] != "runbooks" or pure.suffix != ".md":
        issues.append("source_path must be a .md file under runbooks/")
        return None
    current = root
    for part in pure.parts:
        current = current / part
        if _is_reparse(current):
            issues.append("source_path traverses a symlink or reparse point")
            return None
    resolved = current.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        issues.append("source_path escapes the repository root")
        return None
    return resolved


def _string(value: Any, field_name: str, issues: list[str], maximum: int) -> str | None:
    if not isinstance(value, str) or not value:
        issues.append(f"{field_name} must be a non-empty string")
        return None
    if len(value) > maximum:
        issues.append(f"{field_name} exceeds {maximum} characters")
        return None
    return value


def _array_strings(
    value: Any, field_name: str, issues: list[str], allowed: set[str] | None = None
) -> list[str] | None:
    if not isinstance(value, list):
        issues.append(f"{field_name} must be an array")
        return None
    if len(value) > MAX_TAGS:
        issues.append(f"{field_name} has too many entries")
        return None
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            issues.append(f"{field_name} entries must be non-empty strings")
            return None
        if len(item) > MAX_TAG:
            issues.append(f"{field_name} entry exceeds {MAX_TAG} characters")
            return None
        if allowed is not None and item not in allowed:
            issues.append(f"{field_name} contains unsupported value {item!r}")
            return None
        if item in result:
            issues.append(f"{field_name} contains duplicate value {item!r}")
            return None
        result.append(item)
    return result


def _check_unknown(value: Any, allowed: set[str], where: str, issues: list[str]) -> bool:
    if not isinstance(value, dict):
        issues.append(f"{where} must be an object")
        return False
    unknown = set(value) - allowed
    if unknown:
        issues.append(f"{where} contains unknown field(s): {', '.join(sorted(unknown))}")
        return False
    return True


def _section_sizes(text: str) -> tuple[int, list[int]]:
    sizes: list[int] = []
    current = 0
    for line in text.splitlines(keepends=True):
        encoded = len(line.encode("utf-8"))
        if HEADING.match(line.rstrip("\r\n")):
            if current:
                sizes.append(current)
            current = encoded
        else:
            current += encoded
    if current:
        sizes.append(current)
    return len(text.encode("utf-8")), sizes


def _check_link_target(root: Path, document_path: Path, raw_target: str, issues: list[str]) -> None:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    target = re.sub(r"\s+(?:\"[^\"]*\"|'[^']*')\s*$", "", target).strip()
    if target in {"", "#"} or target.startswith("#"):
        return
    if re.match(r"^(https?://|mailto:|tel:)", target):
        return
    path_part = target.split("#", 1)[0]
    if not path_part:
        return
    if path_part.startswith("/") or "\\" in path_part:
        issues.append(f"local link must be relative: {path_part}")
        return
    candidate = (document_path.parent / path_part).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        issues.append(f"local link escapes the repository: {path_part}")
        return
    if not candidate.is_file():
        issues.append(f"local link target does not exist: {path_part}")


def _inline_links(text: str):
    index = 0
    while True:
        marker = text.find("](", index)
        if marker < 0:
            return
        label_start = text.rfind("[", 0, marker)
        if label_start < 0:
            index = marker + 2
            continue
        depth = 1
        cursor = marker + 2
        while cursor < len(text) and depth:
            char = text[cursor]
            if char == "\\":
                cursor += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            cursor += 1
        if depth:
            return
        image = label_start > 0 and text[label_start - 1] == "!"
        yield (
            image,
            text[label_start + 1 : marker],
            text[marker + 2 : cursor - 1],
        )
        index = cursor


def _check_links(root: Path, document_path: Path, text: str, issues: list[str]) -> None:
    for image, _label, raw_target in _inline_links(text):
        if image:
            issues.append("Markdown images are not allowed in the release corpus")
            continue
        _check_link_target(root, document_path, raw_target, issues)
    for raw_target in REFERENCE_LINK.findall(text):
        _check_link_target(root, document_path, raw_target, issues)


def _read_document(
    root: Path,
    document: dict[str, Any],
    issues: list[str],
    warnings: list[str],
) -> tuple[dict[str, Any] | None, bytes | None, int]:
    if not _check_unknown(document, DOCUMENT_FIELDS, "document", issues):
        return None, None, 0
    key = _string(document.get("document_key"), "document_key", issues, MAX_KEY)
    if key is not None and not KEY.fullmatch(key):
        issues.append("document_key must be a lowercase slug")
    version = _string(document.get("version"), "version", issues, MAX_VERSION)
    title = _string(document.get("title"), "title", issues, MAX_TITLE)
    if not isinstance(document.get("guide_kind"), str) or document.get("guide_kind") not in {
        "diagnosis",
        "recovery_template",
    }:
        issues.append("guide_kind must be diagnosis or recovery_template")
    tags = _array_strings(document.get("tags"), "tags", issues)
    if tags is None:
        return None, None, 0
    applicability = document.get("applicability")
    families: list[str] | None = None
    templates: list[str] | None = None
    if _check_unknown(applicability, APPLICABILITY_FIELDS, "applicability", issues):
        if applicability.get("kind") != "Deployment":
            issues.append("applicability.kind must be Deployment")
        families = _array_strings(
            applicability.get("symptom_families"),
            "applicability.symptom_families",
            issues,
            FAMILIES,
        )
        templates = _array_strings(
            applicability.get("recovery_templates"),
            "applicability.recovery_templates",
            issues,
            TEMPLATES,
        )
    provenance = document.get("provenance")
    if _check_unknown(provenance, PROVENANCE_FIELDS, "provenance", issues):
        revision = provenance.get("source_revision")
        if not isinstance(revision, str) or not HEX40.fullmatch(revision):
            issues.append("provenance.source_revision must be a lowercase 40-hex commit SHA")
        reviewed = provenance.get("reviewed_at")
        if not isinstance(reviewed, str) or not ISO_DATE.fullmatch(reviewed):
            issues.append("provenance.reviewed_at must be YYYY-MM-DD")
        else:
            try:
                date.fromisoformat(reviewed)
            except ValueError:
                issues.append("provenance.reviewed_at is not a valid date")
    if document.get("guide_kind") == "diagnosis" and templates:
        issues.append("diagnosis guides must have an empty recovery_templates array")
    if document.get("guide_kind") == "recovery_template" and templates == []:
        issues.append("recovery_template guides must declare at least one template")
    if key is None or version is None or title is None:
        return None, None, 0
    source_path = _safe_source_path(root, document.get("source_path"), issues)  # type: ignore[arg-type]
    if source_path is None:
        return None, None, 0
    try:
        info = source_path.stat()
    except OSError:
        issues.append(f"source file does not exist: {document.get('source_path')}")
        return None, None, 0
    if not stat.S_ISREG(info.st_mode):
        issues.append(f"source must be a regular file: {document.get('source_path')}")
        return None, None, 0
    size = info.st_size
    if size > MAX_FILE_BYTES:
        issues.append(f"source file exceeds the 256 KiB cap: {document.get('source_path')}")
        return None, None, size
    try:
        with source_path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        issues.append(f"source file cannot be read: {document.get('source_path')}")
        return None, None, size
    if len(raw) > MAX_FILE_BYTES:
        issues.append(f"source file exceeds the 256 KiB cap: {document.get('source_path')}")
        return None, None, len(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        issues.append(f"source file is not valid UTF-8: {document.get('source_path')}")
        return None, raw, len(raw)
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first_line != f"# {title}":
        issues.append(f"H1 must match the manifest title: {document.get('source_path')}")
    _check_links(root, source_path, text, issues)
    _size, sections = _section_sizes(text)
    if any(size > SECTION_WARNING_BYTES for size in sections):
        # Editorial target only. The importer owns deterministic 4 KiB chunking.
        warnings.append(
            f"section exceeds the 4 KiB editorial target: {document.get('source_path')}"
        )
    content_hash = hashlib.sha256(raw).hexdigest()
    if issues:
        return None, raw, len(raw)
    metadata = {
        field: document[field]
        for field in (
            "document_key",
            "version",
            "title",
            "source_path",
            "guide_kind",
            "tags",
            "applicability",
            "provenance",
        )
    }
    metadata_hash = hashlib.sha256(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return (
        {
            "document_key": key,
            "version": version,
            "guide_kind": document.get("guide_kind"),
            "title": title,
            "source_path": document.get("source_path"),
            "_resolved_source_path": str(source_path.resolve()),
            "bytes": len(raw),
            "content_sha256": content_hash,
            "metadata_sha256": metadata_hash,
            "symptom_families": families or [],
            "recovery_templates": templates or [],
            "sections": sections,
            "largest_section_bytes": max(sections, default=0),
            "provenance": provenance,
        },
        raw,
        len(raw),
    )


def _load_manifest(root: Path, release: bool) -> tuple[list[dict[str, Any]], Report]:
    report = Report(release=release)
    catalog_path = root / "runbooks" / "catalog.json"
    if any(_is_reparse(path) for path in (catalog_path.parent, catalog_path)):
        report.errors.append("catalog path traverses a symlink or reparse point")
        return [], report
    try:
        data = _load_json(catalog_path)
    except CorpusError as exc:
        report.errors.append(str(exc))
        return [], report
    if not isinstance(data, dict):
        report.errors.append("catalog must be an object")
        return [], report
    if not _check_unknown(data, ROOT_FIELDS, "catalog", report.errors):
        return [], report
    if data.get("schema_version") != SCHEMA_VERSION:
        report.errors.append(f"schema_version must be {SCHEMA_VERSION}")
    documents = data.get("documents")
    if not isinstance(documents, list):
        report.errors.append("documents must be an array")
        return [], report
    if len(documents) > MAX_DOCUMENTS:
        report.errors.append("documents exceeds the 100-document cap")
        return [], report
    seen: set[tuple[str, str]] = set()
    seen_paths: set[str] = set()
    records: list[dict[str, Any]] = []
    for index, document in enumerate(documents):
        issues: list[str] = []
        if not isinstance(document, dict):
            report.errors.append(f"documents[{index}] must be an object")
            continue
        declared_key = document.get("document_key")
        declared_version = document.get("version")
        if isinstance(declared_key, str) and isinstance(declared_version, str):
            declared = (declared_key, declared_version)
            if declared in seen:
                report.errors.append(
                    f"duplicate document key/version: {declared[0]} v{declared[1]}"
                )
                continue
            seen.add(declared)
        record, _raw, size = _read_document(root, document, issues, report.warnings)
        report.errors.extend(issues)
        report.corpus_bytes += size
        if record is None:
            continue
        if record["_resolved_source_path"] in seen_paths:
            report.errors.append(f"duplicate source_path: {record['source_path']}")
            continue
        seen_paths.add(record["_resolved_source_path"])
        records.append(record)
    if report.corpus_bytes > MAX_CORPUS_BYTES:
        report.errors.append("corpus exceeds the 8 MiB cap")
    if release:
        _check_release(records, report)
    report.documents = records
    return records, report


def _check_release(records: list[dict[str, Any]], report: Report) -> None:
    by_key = {record["document_key"]: record for record in records}
    if len(records) != len(EXPECTED_RELEASE):
        report.errors.append("release corpus must contain exactly eight guides")
    for key, (kind, family, templates) in EXPECTED_RELEASE.items():
        record = by_key.get(key)
        if record is None:
            report.errors.append(f"missing release guide: {key}")
            continue
        if record["guide_kind"] != kind:
            report.errors.append(f"{key} has the wrong guide_kind")
        if record["symptom_families"] != [family]:
            report.errors.append(f"{key} has the wrong symptom family mapping")
        if record["recovery_templates"] != list(templates):
            report.errors.append(f"{key} has the wrong recovery template mapping")


def _compare_baseline(
    root: Path, candidate: list[dict[str, Any]], baseline_root: Path, report: Report
) -> None:
    baseline, baseline_report = _load_manifest(baseline_root, release=False)
    if baseline_report.errors:
        report.errors.extend(f"baseline: {error}" for error in baseline_report.errors)
        return
    by_tuple = {(row["document_key"], row["version"]): row for row in baseline}
    for row in candidate:
        previous = by_tuple.get((row["document_key"], row["version"]))
        if previous is None:
            continue
        if (
            row["content_sha256"] != previous["content_sha256"]
            or row["metadata_sha256"] != previous["metadata_sha256"]
        ):
            report.version_conflicts.append(
                {
                    "document_key": row["document_key"],
                    "version": row["version"],
                    "reason": "same key/version has different content or versioned metadata",
                }
            )


def check_corpus(
    root: str | Path,
    *,
    release: bool = True,
    baseline_root: str | Path | None = None,
) -> Report:
    root_path = Path(root).resolve()
    candidate, report = _load_manifest(root_path, release)
    if baseline_root is not None:
        _compare_baseline(root_path, candidate, Path(baseline_root).resolve(), report)
    return report


def _public(report: Report) -> dict[str, Any]:
    documents = []
    for row in report.documents:
        documents.append(
            {
                "document_key": row["document_key"],
                "version": row["version"],
                "guide_kind": row["guide_kind"],
                "title": row["title"],
                "source_path": row["source_path"],
                "bytes": row["bytes"],
                "content_sha256": row["content_sha256"],
                "metadata_sha256": row["metadata_sha256"],
                "symptom_families": row["symptom_families"],
                "recovery_templates": row["recovery_templates"],
                "sections": row["sections"],
                "largest_section_bytes": row["largest_section_bytes"],
                "provenance": row["provenance"],
            }
        )
    families = sorted({item for row in documents for item in row["symptom_families"]})
    templates = sorted({item for row in documents for item in row["recovery_templates"]})
    return {
        "schema": "RunbookCorpusCheck/v1",
        "checker_version": CHECKER_VERSION,
        "release_check": report.release,
        "documents": documents,
        "counts": {
            "documents": len(documents),
            "diagnosis": sum(row["guide_kind"] == "diagnosis" for row in documents),
            "recovery_templates": sum(
                row["guide_kind"] == "recovery_template" for row in documents
            ),
            "corpus_bytes": report.corpus_bytes,
        },
        "coverage": {"symptom_families": families, "recovery_templates": templates},
        "warnings": report.warnings,
        "errors": report.errors,
        "version_conflicts": report.version_conflicts,
        "valid": report.valid,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="trusted repository root")
    parser.add_argument(
        "--baseline-root",
        help="optional prior corpus root for same-key/version conflict detection",
    )
    parser.add_argument("--no-release", action="store_true", help="skip eight-guide coverage")
    parser.add_argument("--json", action="store_true", help="write only JSON to stdout")
    args = parser.parse_args(argv)
    report = check_corpus(
        args.root,
        release=not args.no_release,
        baseline_root=args.baseline_root,
    )
    value = _public(report)
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(
            f"Runbook corpus: {'valid' if value['valid'] else 'INVALID'} · "
            f"{value['counts']['documents']} documents · "
            f"{value['counts']['corpus_bytes']} bytes"
        )
        for issue in value["errors"]:
            print(f"error: {issue}", file=sys.stderr)
        for conflict in value["version_conflicts"]:
            print(f"conflict: {conflict}", file=sys.stderr)
    return 0 if value["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
