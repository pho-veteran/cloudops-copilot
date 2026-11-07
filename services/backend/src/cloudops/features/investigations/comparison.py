"""Bounded semantic comparison of stored projections, without provider access."""

from cloudops.features.investigations.citation_schemas import CitationRef, Locator
from cloudops.features.investigations.comparison_schemas import SemanticDifference
from cloudops.features.investigations.locators import equivalent, project, sample_number


def differences(left_meta, right_meta, left_payload, right_payload):
    left = {
        entry.key: entry
        for entry in project(left_meta.payload_kind, left_meta.payload_version, left_payload)
    }
    right = {
        entry.key: entry
        for entry in project(right_meta.payload_kind, right_meta.payload_version, right_payload)
    }
    complete = left_meta.coverage.status == right_meta.coverage.status == "available"
    deployment_left, deployment_right = (
        left_payload.get("deployment"),
        right_payload.get("deployment"),
    )
    identity_changed = (
        isinstance(deployment_left, dict)
        and isinstance(deployment_right, dict)
        and deployment_left.get("uid")
        and deployment_right.get("uid")
        and deployment_left["uid"] != deployment_right["uid"]
    )
    query = left_meta.query_metadata
    left_units = {
        str(series.get("unit", ""))
        for series in left_payload.get("series", [])
        if isinstance(series, dict)
    }
    right_units = {
        str(series.get("unit", ""))
        for series in right_payload.get("series", [])
        if isinstance(series, dict)
    }
    series_compatible = (
        isinstance(query, dict)
        and bool(query.get("query_id"))
        and bool(query.get("query_version"))
        and bool(query.get("unit") or query.get("units"))
        and bool(query.get("source_identity"))
        and isinstance(query.get("step"), (int, float))
        and not isinstance(query.get("step"), bool)
        and query["step"] > 0
        and left_meta.requested_window is not None
        and bool(left_units)
        and "" not in left_units
        and left_units == right_units
        and left_meta.query_metadata == right_meta.query_metadata
        and left_meta.requested_window == right_meta.requested_window
        and complete
    )
    # Semantic identity, not the digest's lexical order, defines stable ordering.
    ordered = sorted(
        set(left) | set(right),
        key=lambda key: (
            (left.get(key) or right[key]).kind,
            (left.get(key) or right[key]).identity,
        ),
    )
    for key in ordered:
        before, after = left.get(key), right.get(key)
        entry = before or after
        reason = None
        if identity_changed:
            status, reason = "not_comparable", "payload_identity_changed"
        elif (before and before.ambiguous) or (after and after.ambiguous):
            status, reason = "not_comparable", "ambiguous_identity"
        elif entry.kind == "sample" and (
            not series_compatible
            or (before is not None and sample_number(before.value) is None)
            or (after is not None and sample_number(after.value) is None)
        ):
            status, reason = "not_comparable", "incompatible_numeric_context"
        elif not before or not after:
            status = ("added" if after else "removed") if complete else "unavailable"
            reason = "retained_observations_only" if not complete else None
        elif (
            equivalent(entry.kind, before.value, after.value)
            and before.multiplicity == after.multiplicity
        ):
            status = "unchanged"
        else:
            status = "changed"
        lview, rview = before.view() if before else None, after.view() if after else None
        yield SemanticDifference(
            key=key,
            kind=entry.kind,
            label=entry.label[:240],
            status=status,
            reason=reason,
            left=lview.value if lview and not lview.ambiguous else None,
            right=rview.value if rview and not rview.ambiguous else None,
            left_occurrences=before.multiplicity if before else None,
            right_occurrences=after.multiplicity if after else None,
            left_citation=CitationRef(
                run_id=left_meta.run_id,
                evidence_id=left_meta.id,
                locator=Locator(kind=entry.kind, key=key) if not before.ambiguous else None,
            )
            if before
            else None,
            right_citation=CitationRef(
                run_id=right_meta.run_id,
                evidence_id=right_meta.id,
                locator=Locator(kind=entry.kind, key=key) if not after.ambiguous else None,
            )
            if after
            else None,
            preview_truncated=bool(
                (lview and lview.preview_truncated) or (rview and rview.preview_truncated)
            ),
        )
