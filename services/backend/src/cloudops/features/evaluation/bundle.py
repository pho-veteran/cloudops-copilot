"""Exact, immutable E04/E05 context snapshots for evaluation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cloudops.features.diagnosis.context import PreparedContext
from cloudops.features.evaluation.hashing import canonical, content_sha256
from cloudops.features.evaluation.versions import VERSIONS
from cloudops.features.investigations.locators import digest

# A snapshot retains both wire messages and full citation/value records. Keep its
# independent storage bound above the 128 KiB wire ceiling without truncating either.
MAX_BUNDLE_BYTES = 512 * 1024


class EvalInputError(ValueError):
    """Closed evaluation input failure without payload disclosure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class EvalBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["EvalBundle/v1"] = VERSIONS["bundle"]
    bundle_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    captured_at: datetime
    context: PreparedContext
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def from_context(
    context: PreparedContext,
    *,
    case_id: str,
    bundle_id: str,
    captured_at,
) -> EvalBundle:
    bundle = EvalBundle(
        bundle_id=bundle_id,
        case_id=case_id,
        captured_at=captured_at,
        context=context,
        manifest_sha256=content_sha256(context),
    )
    verify(bundle, expected_case_id=case_id)
    return bundle


def verify(bundle: EvalBundle, *, expected_case_id: str | None = None) -> None:
    if bundle.schema_version != VERSIONS["bundle"]:
        raise EvalInputError("bundle_schema")
    if expected_case_id is not None and bundle.case_id != expected_case_id:
        raise EvalInputError("case_id_mismatch")
    if bundle.manifest_sha256 != content_sha256(bundle.context):
        raise EvalInputError("manifest_mismatch")

    seen: set[str] = set()
    for target in (*bundle.context.required_targets, *bundle.context.optional_targets):
        if target.alias in seen:
            raise EvalInputError("duplicate_alias")
        seen.add(target.alias)
        if target.entry_digest != digest(target.value):
            raise EvalInputError("entry_digest_mismatch")
        if target.value_bytes != len(canonical(target.value).encode("utf-8")):
            raise EvalInputError("value_bytes_mismatch")

    for runbook in bundle.context.runbooks:
        if runbook.alias in seen:
            raise EvalInputError("duplicate_alias")
        seen.add(runbook.alias)
        # A retrieved chunk is usually one section of its document, so only the chunk
        # hash describes these bytes; document_sha256 stays document metadata.
        content_sha = hashlib.sha256(runbook.content.encode("utf-8")).hexdigest()
        if (
            runbook.content_bytes != len(runbook.content.encode("utf-8"))
            or runbook.reference.chunk_sha256 != content_sha
        ):
            raise EvalInputError("runbook_digest_mismatch")


def load(path: Path, *, expected_case_id: str | None = None) -> tuple[EvalBundle, str]:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError:
        raise EvalInputError("bundle_unreadable") from None
    if len(raw) > MAX_BUNDLE_BYTES:
        raise EvalInputError("bundle_too_large")
    raw_sha = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(raw.decode("utf-8"))
        bundle = EvalBundle.model_validate(value)
        verify(bundle, expected_case_id=expected_case_id)
    except EvalInputError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValidationError, ValueError):
        raise EvalInputError("bundle_schema") from None
    return bundle, raw_sha


def context_of(bundle: EvalBundle) -> PreparedContext:
    return bundle.context
