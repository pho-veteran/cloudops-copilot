"""Safe, bounded capture and validation of a RunbookCatalog/v1 corpus."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = "RunbookCatalog/v1"
CANONICALIZATION_VERSION = "2"
MAX_DOCUMENTS = 100
MAX_FILE_BYTES = 256 * 1024
MAX_CORPUS_BYTES = 8 * 1024 * 1024
MAX_KEY = 64
MAX_VERSION = 64
MAX_TITLE = 200
MAX_SOURCE_PATH = 256
MAX_TAGS = 16
MAX_TAG = 64
FAMILIES = frozenset(
    {
        "crashloop_startup",
        "oom_restarts",
        "traffic_latency_errors",
        "pending_unschedulable",
        "image_pull",
    }
)
TEMPLATES = frozenset({"restore_deployment_template", "set_memory_profile", "scale_deployment"})
GUIDE_KINDS = frozenset({"diagnosis", "recovery_template"})
KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
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


class CorpusError(ValueError):
    """A safe corpus rejection; never includes absolute host paths or file bodies."""


@dataclass(frozen=True)
class CapturedDocument:
    document_key: str
    version: str
    title: str
    source_path: str
    guide_kind: str
    tags: tuple[str, ...]
    applicability: dict[str, Any]
    provenance: dict[str, Any]
    content: bytes
    content_sha256: str
    metadata_sha256: str

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "document_key": self.document_key,
            "version": self.version,
            "title": self.title,
            "source_path": self.source_path,
            "guide_kind": self.guide_kind,
            "tags": list(self.tags),
            "applicability": self.applicability,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class CapturedCorpus:
    documents: tuple[CapturedDocument, ...]
    source_digest: str
    captured_at: datetime
    manifest_bytes: int


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _metadata_projection(document: CapturedDocument) -> dict[str, Any]:
    return {
        "canonicalization_version": CANONICALIZATION_VERSION,
        "metadata": document.metadata,
    }


def metadata_digest(document: CapturedDocument) -> str:
    return _sha(_canonical(_metadata_projection(document)))


def _source_projection(documents: tuple[CapturedDocument, ...]) -> dict[str, Any]:
    rows = []
    for document in sorted(documents, key=lambda item: (item.document_key, item.version)):
        rows.append(
            {
                "metadata": _metadata_projection(document)["metadata"],
                "content_sha256": document.content_sha256,
                "bytes": len(document.content),
            }
        )
    return {"schema_version": SCHEMA_VERSION, "documents": rows}


def source_digest(documents: tuple[CapturedDocument, ...]) -> str:
    return _sha(_canonical(_source_projection(documents)))


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CorpusError("catalog contains a duplicate JSON key")
        result[key] = value
    return result


def _load_json(path: Path) -> tuple[Any, bytes]:
    try:
        info = path.stat()
    except OSError:
        raise CorpusError("catalog.json is missing or unreadable") from None
    if not stat.S_ISREG(info.st_mode):
        raise CorpusError("catalog.json must be a regular file")
    if info.st_size > MAX_CORPUS_BYTES:
        raise CorpusError("catalog.json exceeds the corpus byte cap")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CORPUS_BYTES + 1)
    except OSError:
        raise CorpusError("catalog.json could not be read") from None
    if len(raw) != info.st_size or len(raw) > MAX_CORPUS_BYTES:
        raise CorpusError("catalog.json changed during capture")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CorpusError("catalog.json is not valid UTF-8 JSON") from None
    return value, raw


def _is_reparse(path: Path) -> bool:
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) & attributes)


def _safe_path(root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_SOURCE_PATH:
        raise CorpusError(f"source_path must be a string of 1..{MAX_SOURCE_PATH} characters")
    if "\x00" in raw or "\\" in raw or any(ord(char) < 32 for char in raw):
        raise CorpusError("source_path contains an unsupported byte")
    value = PurePosixPath(raw)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        raise CorpusError("source_path must be relative without traversal")
    if len(value.parts) < 2 or value.parts[0] != "runbooks" or value.suffix != ".md":
        raise CorpusError("source_path must be a Markdown file under runbooks/")
    current = root
    for part in value.parts:
        current = current / part
        if current.is_symlink() or _is_reparse(current):
            raise CorpusError("source_path traverses a symlink or reparse point")
    try:
        current.relative_to(root.resolve())
    except ValueError:
        raise CorpusError("source_path escapes the capture root") from None
    return current


def _file_bytes(path: Path, source_path: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    try:
        before = path.stat()
    except OSError:
        raise CorpusError(f"source file is missing: {source_path}") from None
    if not stat.S_ISREG(before.st_mode):
        raise CorpusError(f"source file is not regular: {source_path}")
    if before.st_size > max_bytes:
        if max_bytes == MAX_FILE_BYTES:
            raise CorpusError(f"source file exceeds the 256 KiB cap: {source_path}")
        raise CorpusError(f"source file exceeds the remaining corpus budget: {source_path}")
    try:
        with path.open("rb") as stream:
            content = stream.read(max_bytes + 1)
    except OSError:
        raise CorpusError(f"source file could not be read: {source_path}") from None
    if len(content) != before.st_size or len(content) > max_bytes:
        raise CorpusError(f"source file changed during capture: {source_path}")
    try:
        after = path.stat()
    except OSError:
        raise CorpusError(f"source file disappeared during capture: {source_path}") from None
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise CorpusError(f"source file changed during capture: {source_path}")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        raise CorpusError(f"source file is not valid UTF-8: {source_path}") from None
    return content


def _require_object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CorpusError(f"{where} must be an object")
    return value


def _require_fields(value: dict[str, Any], fields: set[str], where: str) -> None:
    missing = fields - value.keys()
    unknown = value.keys() - fields
    if missing:
        raise CorpusError(f"{where} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise CorpusError(f"{where} has unknown fields")


def _require_string(value: Any, where: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CorpusError(f"{where} must be a non-empty string of at most {maximum} characters")
    return value


def _string_list(
    value: Any, where: str, max_items: int, max_entry: int, *, allow_empty: bool
) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise CorpusError(f"{where} must be {'a list' if allow_empty else 'a non-empty list'}")
    if len(value) > max_items:
        raise CorpusError(f"{where} has too many entries")
    result = []
    for entry in value:
        if not isinstance(entry, str) or not entry or len(entry) > max_entry:
            raise CorpusError(f"{where} contains an entry over {max_entry} characters")
        result.append(entry)
    if len(result) != len(set(result)):
        raise CorpusError(f"{where} contains duplicate entries")
    return tuple(result)


def _document(
    value: Any, index: int, root: Path, max_bytes: int = MAX_FILE_BYTES
) -> CapturedDocument:
    where = f"documents[{index}]"
    item = _require_object(value, where)
    _require_fields(item, DOCUMENT_FIELDS, where)
    key = _require_string(item["document_key"], f"{where}.document_key", MAX_KEY)
    if not KEY_RE.fullmatch(key):
        raise CorpusError(f"{where}.document_key must be a lowercase slug")
    version = _require_string(item["version"], f"{where}.version", MAX_VERSION)
    title = _require_string(item["title"], f"{where}.title", MAX_TITLE)
    guide_kind = _require_string(item["guide_kind"], f"{where}.guide_kind", 32)
    if guide_kind not in GUIDE_KINDS:
        raise CorpusError(f"{where}.guide_kind is invalid")
    tags = _string_list(item["tags"], f"{where}.tags", MAX_TAGS, MAX_TAG, allow_empty=True)
    applicability = _require_object(item["applicability"], f"{where}.applicability")
    _require_fields(applicability, APPLICABILITY_FIELDS, f"{where}.applicability")
    if applicability["kind"] != "Deployment":
        raise CorpusError(f"{where}.applicability.kind must be Deployment")
    families = _string_list(
        applicability["symptom_families"],
        f"{where}.applicability.symptom_families",
        32,
        MAX_TAG,
        allow_empty=True,
    )
    if any(family not in FAMILIES for family in families):
        raise CorpusError(f"{where}.applicability.symptom_families contains an unknown family")
    templates = _string_list(
        applicability["recovery_templates"],
        f"{where}.applicability.recovery_templates",
        3,
        MAX_TAG,
        allow_empty=True,
    )
    if any(template not in TEMPLATES for template in templates):
        raise CorpusError(f"{where}.applicability.recovery_templates contains an unknown template")
    if guide_kind == "diagnosis" and templates:
        raise CorpusError(f"{where} diagnosis must not declare recovery templates")
    if guide_kind == "recovery_template" and not templates:
        raise CorpusError(f"{where} recovery guide must declare a recovery template")
    provenance = _require_object(item["provenance"], f"{where}.provenance")
    _require_fields(provenance, PROVENANCE_FIELDS, f"{where}.provenance")
    revision = _require_string(
        provenance["source_revision"], f"{where}.provenance.source_revision", 40
    )
    if not HEX40_RE.fullmatch(revision):
        raise CorpusError(f"{where}.provenance.source_revision must be a lowercase 40-hex SHA")
    reviewed_at = _require_string(provenance["reviewed_at"], f"{where}.provenance.reviewed_at", 10)
    if not DATE_RE.fullmatch(reviewed_at):
        raise CorpusError(f"{where}.provenance.reviewed_at must be an ISO date")
    try:
        datetime.fromisoformat(reviewed_at)
    except ValueError:
        raise CorpusError(f"{where}.provenance.reviewed_at must be a valid date") from None
    source_path = _require_string(item["source_path"], f"{where}.source_path", MAX_SOURCE_PATH)
    path = _safe_path(root, source_path)
    content = _file_bytes(path, source_path, min(MAX_FILE_BYTES, max_bytes))
    partial = CapturedDocument(
        document_key=key,
        version=version,
        title=title,
        source_path=source_path,
        guide_kind=guide_kind,
        tags=tags,
        applicability={
            "kind": "Deployment",
            "symptom_families": list(families),
            "recovery_templates": list(templates),
        },
        provenance={"source_revision": revision, "reviewed_at": reviewed_at},
        content=content,
        content_sha256=_sha(content),
        metadata_sha256="",
    )
    return CapturedDocument(
        **{
            **partial.__dict__,
            "metadata_sha256": metadata_digest(partial),
        }
    )


def capture_corpus(root: str | Path) -> CapturedCorpus:
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise CorpusError("capture root must be an existing directory")
    catalog = root_path / "runbooks" / "catalog.json"
    runbooks = catalog.parent
    if runbooks.is_symlink() or _is_reparse(runbooks):
        raise CorpusError("runbooks/ must not be a symlink or reparse point")
    if catalog.is_symlink() or _is_reparse(catalog):
        raise CorpusError("catalog.json must not be a symlink or reparse point")
    value, manifest = _load_json(catalog)
    item = _require_object(value, "catalog")
    _require_fields(item, ROOT_FIELDS, "catalog")
    if item["schema_version"] != SCHEMA_VERSION:
        raise CorpusError(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(item["documents"], list):
        raise CorpusError("documents must be a list")
    if len(item["documents"]) > MAX_DOCUMENTS:
        raise CorpusError(f"corpus exceeds the {MAX_DOCUMENTS}-document cap")
    documents: list[CapturedDocument] = []
    seen_tuples: set[tuple[str, str]] = set()
    seen_paths: set[str] = set()
    total = len(manifest)
    for index, row in enumerate(item["documents"]):
        if isinstance(row, dict):
            key, version, source = (
                row.get("document_key"),
                row.get("version"),
                row.get("source_path"),
            )
            if isinstance(key, str) and isinstance(version, str):
                pair = (key, version)
                if pair in seen_tuples:
                    raise CorpusError("duplicate document key/version")
                seen_tuples.add(pair)
            if isinstance(source, str):
                resolved = str(_safe_path(root_path, source).resolve())
                if resolved in seen_paths:
                    raise CorpusError(f"duplicate source_path: {source}")
                seen_paths.add(resolved)
        document = _document(row, index, root_path, MAX_CORPUS_BYTES - total)
        total += len(document.content)
        if total > MAX_CORPUS_BYTES:
            raise CorpusError("corpus exceeds the 8 MiB cap")
        documents.append(document)
    return CapturedCorpus(
        documents=tuple(documents),
        source_digest=source_digest(tuple(documents)),
        captured_at=datetime.now(timezone.utc),
        manifest_bytes=len(manifest),
    )
