"""Exact scoped read services shared by citations and comparisons."""

import base64
import json

from sqlalchemy import select

from cloudops.features.incidents.models import Incident
from cloudops.features.incidents.service import get_incident
from cloudops.features.investigations.citation_schemas import (
    CitationRef,
    CitationResolution,
    CitationTargets,
    Locator,
)
from cloudops.features.investigations.comparison import differences
from cloudops.features.investigations.comparison_schemas import (
    ComparisonSide,
    RunComparison,
    SourceDifference,
)
from cloudops.features.investigations.locators import UnsupportedProjection, digest, project
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.investigations.service import (
    evidence_content,
    evidence_metadata,
    window_view,
)
from cloudops.features.investigations.storage import evidence_views
from cloudops.features.workloads.models import Workload
from cloudops.shared.errors import DomainError


def page(items, fingerprint, cursor, limit):
    """A cursor is an untrusted paging hint, never authorization or a data source."""
    after = None
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            if not isinstance(value, dict) or set(value) != {"snapshot", "after"}:
                raise ValueError()
            if value["snapshot"] != fingerprint:
                raise DomainError(
                    409, "comparison_changed", "Stored availability changed; reload the selection."
                )
            after = value["after"]
            if not isinstance(after, str) or len(after) > 128:
                raise ValueError()
        except (ValueError, UnicodeError, TypeError):
            raise DomainError(422, "invalid_cursor", "Invalid paging cursor.") from None
    selected = []
    serialized_bytes = 0
    found = after is None
    more = False
    for item in items:
        key = item.locator.key if hasattr(item, "locator") else item.key
        if not found:
            found = key == after
            continue
        item_bytes = len(item.model_dump_json().encode("utf-8"))
        if len(selected) == limit or (selected and serialized_bytes + item_bytes > 128 * 1024):
            more = True
            break
        selected.append(item)
        serialized_bytes += item_bytes
    if not found:
        raise DomainError(422, "invalid_cursor", "The cursor is outside this selection.")
    following = None
    if more:
        last = selected[-1]
        key = last.locator.key if hasattr(last, "locator") else last.key
        following = base64.urlsafe_b64encode(
            json.dumps({"snapshot": fingerprint, "after": key}, separators=(",", ":")).encode()
        ).decode()
    return selected, following


def _entries(meta, payload):
    try:
        return project(meta.payload_kind, meta.payload_version, payload)
    except UnsupportedProjection:
        raise DomainError(
            409,
            "locator_not_supported",
            "This stored payload has no supported semantic projection.",
        ) from None


def citation(db, incident_id, run_id, evidence_id, kind, key):
    meta = evidence_metadata(db, incident_id, run_id, evidence_id)
    reference = CitationRef(run_id=run_id, evidence_id=evidence_id)
    target = None
    if kind is not None:
        reference.locator = Locator(kind=kind, key=key)
        content = evidence_content(db, incident_id, run_id, evidence_id)
        target = next(
            (
                item.view()
                for item in _entries(meta, content.payload)
                if item.kind == kind and item.key == key
            ),
            None,
        )
        if target is None:
            raise DomainError(
                404, "citation_target_not_found", "The exact stored citation target was not found."
            )
        if target.ambiguous:
            raise DomainError(
                409, "citation_target_ambiguous", "The stored target identity is ambiguous."
            )
    return CitationResolution(citation=reference, evidence=meta, target=target)


def targets(db, incident_id, run_id, evidence_id, kind, cursor, limit):
    meta = evidence_metadata(db, incident_id, run_id, evidence_id)
    content = evidence_content(db, incident_id, run_id, evidence_id)
    entries = _entries(meta, content.payload)
    fingerprint = digest(
        [str(run_id), str(evidence_id), meta.sha256, meta.content_status, kind, "locator_v1"]
    )
    items, following = page(
        (entry.view() for entry in entries if kind is None or entry.kind == kind),
        fingerprint,
        cursor,
        limit,
    )
    return CitationTargets(evidence=meta, items=items, next_cursor=following)


def _side(db, incident_id, run_id):
    # The current application read policy allows either signed-in role to read
    # every incident. Keep both exact resource checks here, before serialization.
    get_incident(db, incident_id)
    row = db.get(InvestigationRun, run_id)
    if row is None or row.incident_id != incident_id:
        raise DomainError(404, "comparison_not_found", "Investigation selection not found.")
    incident = db.get(Incident, incident_id)
    workload = db.get(Workload, incident.workload_id)
    if workload is None:
        raise DomainError(404, "comparison_not_found", "Investigation selection not found.")
    return ComparisonSide(
        incident_id=incident_id,
        run_id=run_id,
        workload_id=workload.id,
        workload_uid=workload.uid,
        cluster=workload.cluster,
        namespace=workload.namespace,
        name=workload.name,
        state=row.state,
        window=window_view(row),
        question=row.question,
        question_source=row.question_source,
        outcome_code=row.outcome_code,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )


def _metadata(db, side):
    rows = db.scalars(
        select(Evidence)
        .where(Evidence.run_id == side.run_id, Evidence.workload_uid == side.workload_uid)
        .order_by(Evidence.source_key, Evidence.catalog_version)
    ).all()
    return {(row.source_key, row.catalog_version): row for row in evidence_views(db, rows)}


def compare(
    db,
    incident_id,
    run_id,
    right_incident_id,
    right_run_id,
    source_key,
    catalog_version,
    changed_only,
    cursor,
    limit,
    difference_status=None,
):
    left, right = _side(db, incident_id, run_id), _side(db, right_incident_id, right_run_id)
    same = left.workload_id == right.workload_id and left.workload_uid == right.workload_uid
    configured = (left.cluster, left.namespace, left.name) == (
        right.cluster,
        right.namespace,
        right.name,
    )
    if not same and (not configured or left.workload_id == right.workload_id):
        raise DomainError(
            422, "unrelated_workloads", "These stored workload identities cannot be compared."
        )
    response = RunComparison(
        left=left,
        right=right,
        identity_relation="same_identity" if same else "replacement",
        sources=[],
    )
    if not same:
        response.status, response.reason = "not_comparable", "workload_identity_replaced"
        return response
    lmeta, rmeta = _metadata(db, left), _metadata(db, right)
    for slot in sorted(lmeta.keys() | rmeta.keys()):
        before, after = lmeta.get(slot), rmeta.get(slot)
        reason = None
        if not before or not after:
            status, reason = "unavailable", "source_not_recorded"
        elif before.content_status != "available" or after.content_status != "available":
            status, reason = "unavailable", "content_unavailable_or_expired"
        elif (before.schema_version, before.payload_kind, before.payload_version) != (
            after.schema_version,
            after.payload_kind,
            after.payload_version,
        ):
            status, reason = "not_comparable", "incompatible_payload_version"
        else:
            metadata_equal = (
                before.coverage == after.coverage
                and before.query_metadata == after.query_metadata
                and before.time_relation == after.time_relation
                and before.requested_window == after.requested_window
            )
            status = "unchanged" if before.sha256 == after.sha256 and metadata_equal else "changed"
            if before.sha256 == after.sha256 and not metadata_equal:
                reason = "source_context_changed"
        response.sources.append(
            SourceDifference(
                source_key=slot[0],
                catalog_version=slot[1],
                left=before,
                right=after,
                status=status,
                reason=reason,
            )
        )
    if source_key is None:
        if cursor:
            raise DomainError(422, "invalid_cursor", "Select a source before paging details.")
        return response
    slot = (source_key, catalog_version)
    source = next(
        (item for item in response.sources if (item.source_key, item.catalog_version) == slot), None
    )
    if source is None:
        raise DomainError(404, "source_not_found", "Stored source selection not found.")
    if source.status in {"unavailable", "not_comparable"}:
        response.reason = source.reason
        return response
    before, after = source.left, source.right
    lpayload = evidence_content(db, left.incident_id, left.run_id, before.id).payload
    rpayload = evidence_content(db, right.incident_id, right.run_id, after.id).payload
    fingerprint = digest(
        [
            left.model_dump(mode="json"),
            right.model_dump(mode="json"),
            [item.model_dump(mode="json") for item in response.sources],
            source_key,
            catalog_version,
            changed_only,
            difference_status,
            "semantic_v1",
        ]
    )
    try:
        rows = differences(before, after, lpayload, rpayload)
        items, following = page(
            (
                item
                for item in rows
                if (not changed_only or item.status != "unchanged")
                and (difference_status is None or item.status == difference_status)
            ),
            fingerprint,
            cursor,
            limit,
        )
    except UnsupportedProjection:
        response.status, response.reason = "not_comparable", "unsupported_semantic_schema"
        return response
    response.items, response.next_cursor = items, following
    return response
