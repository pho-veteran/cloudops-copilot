"""Immutable evidence publication and metadata projections.

Content is deferred by default; only the explicitly scoped content reader loads it.
"""

import hashlib
import json
import math
import re
from datetime import timedelta
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.catalog import CATALOG_VERSION, SOURCES, derive_pack_coverage
from cloudops.features.investigations.schemas import (
    Coverage,
    EvidenceView,
    FixturePayload,
    SourceResult,
)
from cloudops.features.investigations.models import Evidence, EvidencePin, InvestigationRun
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import database_now

REASONS = {
    "forbidden",
    "timeout",
    "source_not_configured",
    "source_unavailable",
    "no_samples",
    "truncated",
    "stale",
    "identity_changed",
    "not_applicable",
    "not_captured",
    "expired",
}
# Only recognized data fields survive, at every level. Raw Kubernetes object maps and secret
# material are never accepted as an opaque blob.
FIELDS = {
    "deployment",
    "pods",
    "containers",
    "events",
    "logs",
    "series",
    "samples",
    "metadata",
    "configmaps",
    "uid",
    "name",
    "namespace",
    "kind",
    "revision",
    "generation",
    "replicas",
    "ready_replicas",
    "available_replicas",
    "conditions",
    "type",
    "status",
    "reason",
    "message",
    "image",
    "image_id",
    "state",
    "waiting",
    "running",
    "terminated",
    "started_at",
    "finished_at",
    "restart_count",
    "exit_code",
    "last_state",
    "timestamp",
    "observed_at",
    "first_seen",
    "last_seen",
    "count",
    "involved_uid",
    "container",
    "pod_uid",
    "lines",
    "text",
    "previous",
    "labels",
    "values",
    "value",
    "unit",
    "warnings",
    "query_id",
    "query_version",
    "query",
    "step",
    "start",
    "end",
    "source",
    "workload",
    "label",
    "observation",
}
FIELDS.update({"pod", "workload_uid", "resolved_query", "units", "window", "source_identity"})
AUTHORIZATION = re.compile(r"(?i)(\bauthorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s,;]+")
CREDENTIAL = re.compile(
    r"""(?ix)(["']?(?:password|passwd|token|secret|api[_-]?key|authorization)["']?
        \s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)"""
)
AUTH_SCHEME = re.compile(r"(?i)(\b(?:bearer|basic)\s+)[^\s,;]+")
TOKEN_FORMAT = re.compile(
    r"\b(?:eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+"
    r"|AKIA[A-Z0-9]{16})\b"
)
PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----.*?"
    r"(?:-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----|$)",
    re.DOTALL,
)
URL_CREDENTIAL = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/@\s]+@")
LIMITS = {
    "pods": 10,
    "containers": 20,
    "events": 100,
    "lines": 200,
    "series": 25,
    "samples": 20000,
    "values": 20000,
}
ROOT_FIELDS = {
    "fixture": {"label", "workload", "observation"},
    "state": {"deployment", "pods", "containers", "metadata", "configmaps"},
    "events": {"events"},
    "logs": {"logs", "containers", "pods"},
    "series": {"series"},
}
CONFIG_FIELDS = {
    "metadata",
    "configmaps",
    "uid",
    "name",
    "namespace",
    "kind",
    "resource_version",
    "references",
    "configmap",
}
QUERY_FIELDS = {
    "query_id",
    "query_version",
    "binding_version",
    "producer_version",
    "query",
    "resolved_query",
    "window",
    "start",
    "end",
    "step",
    "unit",
    "units",
    "warnings",
    "source_identity",
}
QUERY_IDENTITY_FIELDS = {
    "cluster_id",
    "namespace",
    "workload_uid",
    "pod_uids",
    "endpoint_id",
}
QUERY_WINDOW_FIELDS = {"start", "end"}
MAP_FIELDS = {"deployment", "metadata", "labels", "source_identity", "window"}
OBJECT_LIST_FIELDS = {
    "pods",
    "containers",
    "events",
    "logs",
    "series",
    "configmaps",
    "conditions",
}
LOG_FIELDS = {
    "pods",
    "containers",
    "logs",
    "uid",
    "pod_uid",
    "pod",
    "name",
    "container",
    "lines",
    "text",
    "timestamp",
    "previous",
}
SERIES_FIELDS = {
    "series",
    "labels",
    "namespace",
    "pod",
    "pod_uid",
    "container",
    "workload_uid",
    "name",
    "uid",
    "samples",
    "values",
    "timestamp",
    "value",
    "unit",
}
EVENT_FIELDS = {
    "events",
    "uid",
    "name",
    "namespace",
    "kind",
    "reason",
    "message",
    "timestamp",
    "first_seen",
    "last_seen",
    "count",
    "involved_uid",
    "type",
}
STATE_FIELDS = FIELDS - {"logs", "lines", "text", "series", "samples", "values", "query"}


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _order(value):
    # Ordering must not fail before invalid metric samples can be classified as gaps.
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    if isinstance(value, dict):
        return (
            str(value.get("uid", value.get("pod_uid", ""))),
            str(value.get("container", value.get("name", ""))),
            encoded,
        )
    return ("", "", encoded)


def _sample(value):
    if isinstance(value, list) and len(value) == 2:
        timestamp, number = value
    elif (
        isinstance(value, dict)
        and set(value) <= {"timestamp", "value"}
        and set(value) == {"timestamp", "value"}
    ):
        timestamp, number = value["timestamp"], value["value"]
    else:
        return False
    if not isinstance(timestamp, (str, int, float)) or isinstance(timestamp, bool):
        return False
    try:
        if isinstance(timestamp, str):
            try:
                valid_time = math.isfinite(float(timestamp))
            except ValueError:
                valid_time = (
                    datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is not None
                )
        else:
            valid_time = math.isfinite(timestamp)
        return valid_time and not isinstance(number, bool) and math.isfinite(float(number))
    except (ValueError, TypeError, OverflowError):
        return False


def _redact(value: str) -> str:
    value = PRIVATE_KEY.sub("[REDACTED]", value)
    value = URL_CREDENTIAL.sub(r"\1[REDACTED]@", value)
    value = AUTHORIZATION.sub(r"\1[REDACTED]", value)
    value = CREDENTIAL.sub(r"\1[REDACTED]", value)
    value = AUTH_SCHEME.sub(r"\1[REDACTED]", value)
    return TOKEN_FORMAT.sub("[REDACTED]", value)


def _clean(value, key="", depth=0, budget=None, fields=None, pod=""):
    if budget is None:
        budget = {"samples": 20000, "series": 25, "pods_seen": set(), "containers_seen": set()}
    if fields is None:
        fields = FIELDS
    if depth > 12:
        raise ValueError("Evidence nesting exceeds the limit.")
    if isinstance(value, dict):
        if fields is QUERY_FIELDS and key == "source_identity":
            fields = QUERY_IDENTITY_FIELDS
        elif fields is QUERY_FIELDS and key == "window":
            fields = QUERY_WINDOW_FIELDS
        for field, item in value.items():
            if field not in fields:
                continue
            if field in MAP_FIELDS and not isinstance(item, dict):
                raise ValueError("Evidence map fields must contain objects.")
            if field in OBJECT_LIST_FIELDS and (
                not isinstance(item, list) or any(not isinstance(row, dict) for row in item)
            ):
                raise ValueError("Evidence collections must contain objects.")
            if field == "text" and not isinstance(item, str):
                raise ValueError("Log text must be a string.")
            if field in {"lines", "warnings", "pod_uids"} and (
                not isinstance(item, list) or any(not isinstance(row, str) for row in item)
            ):
                raise ValueError("Evidence text collections must contain strings.")
            if fields is CONFIG_FIELDS and not isinstance(item, (dict, list, str)):
                raise ValueError("ConfigMap metadata must contain identifiers.")
            if (
                fields is CONFIG_FIELDS
                and field == "references"
                and (
                    not isinstance(item, list)
                    or any(not isinstance(row, (dict, str)) for row in item)
                )
            ):
                raise ValueError("ConfigMap references must contain identifiers.")
        if str(value.get("kind", "")).lower() == "secret":
            raise ValueError("Secret objects are not evidence.")
        for identity_field in ("pod", "pod_uid", "container", "uid", "name"):
            if identity_field in value and not isinstance(value[identity_field], str):
                raise ValueError("Evidence identity fields must be strings.")
        if fields is CONFIG_FIELDS:
            for identity_field in ("uid", "name", "namespace", "kind", "resource_version"):
                if identity_field in value and (
                    not isinstance(value[identity_field], str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,253}", value[identity_field])
                ):
                    raise ValueError("ConfigMap metadata contains an invalid identifier.")
        if fields is SERIES_FIELDS:
            if "value" in value and isinstance(value["value"], (dict, list)):
                raise ValueError("A metric value must be a scalar.")
            for list_field in ("series", "samples", "values"):
                if list_field in value and not isinstance(value[list_field], list):
                    raise ValueError("Metric collections must be lists.")
        if (
            fields is SERIES_FIELDS
            and key == "series"
            and not any(k in value for k in ("samples", "values", "series"))
        ):
            budget["sample_gaps"] = True
        pod = str(value.get("pod_uid", value.get("pod", pod)))
        if key == "pods":
            pod = str(value.get("uid") or value.get("name") or _canonical(value).hex())
        if pod:
            seen = budget["pods_seen"]
            if pod not in seen and len(seen) >= 10:
                return {}
            seen.add(pod)
        container = value.get("container", value.get("name") if key == "containers" else None)
        if key == "containers" and not container:
            container = _canonical(value).hex()
        if isinstance(container, str) and container:
            identity = (pod, container)
            seen = budget["containers_seen"]
            if identity not in seen and len(seen) >= 20:
                return {}
            seen.add(identity)
        return {
            k: _clean(v, k, depth + 1, budget, fields, pod)
            for k, v in sorted(value.items())
            if k in fields
        }
    if isinstance(value, list):
        values = value
        if key in {"pods", "containers", "events", "series", "logs"}:
            values = sorted(value, key=_order)
        if key in {"samples", "values"}:
            valid = [v for v in values if _sample(v)]
            if not values or len(valid) != len(values):
                budget["sample_gaps"] = True
            values = valid
            count = min(len(values), budget["samples"])
            budget["samples"] -= count
            # A sample is a single timestamp/value pair, never an arbitrary nested object.
            return [_clean(v, "", depth + 1, budget, fields, pod) for v in values[:count]]
        if key == "series" and not values:
            budget["sample_gaps"] = True
        if key == "series":
            count = min(len(values), budget["series"])
            budget["series"] -= count
            values = values[:count]
        elif key in {"pods", "containers"}:
            # Each record registers its identity while traversing the dictionary.
            values = [v for v in values if isinstance(v, dict)]
        else:
            values = values[: LIMITS.get(key, 1000)]
        items = [_clean(v, key, depth + 1, budget, fields, pod) for v in values]
        return (
            [item for item in items if item != {}]
            if key in {"pods", "containers", "logs"}
            else items
        )
    if isinstance(value, str):
        if fields is CONFIG_FIELDS and not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,253}", value):
            raise ValueError("ConfigMap metadata contains an invalid identifier.")
        return _redact(value)[:32768]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Evidence contains unsupported values.")


def _bound_log_segments(value, budgets, context=("", ""), key=""):
    if isinstance(value, dict):
        pod, container = context
        pod = str(value.get("pod_uid", value.get("pod", pod)))
        if key == "pods":
            pod = str(value.get("uid", value.get("name", pod)))
        container = str(value.get("container", container))
        if key == "containers":
            container = str(value.get("name", container))
        return {k: _bound_log_segments(v, budgets, (pod, container), k) for k, v in value.items()}
    if isinstance(value, list):
        return [_bound_log_segments(v, budgets, context, key) for v in value]
    if isinstance(value, str) and key in {"text", "lines"}:
        remaining = budgets.setdefault(context, [32768, 200])
        chunks = value.splitlines(keepends=True)[: remaining[1]]
        text = "".join(chunks).encode("utf-8")[: remaining[0]].decode("utf-8", "ignore")
        remaining[0] -= len(text.encode("utf-8"))
        remaining[1] -= len(text.splitlines())
        return text
    return value


def _count_samples(value):
    if isinstance(value, dict):
        return sum(
            len(v) if k in {"samples", "values"} and isinstance(v, list) else _count_samples(v)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return sum(_count_samples(v) for v in value)
    return 0


def _bounded(payload, kind, byte_limit, budget, source_key):
    original = payload
    if kind == "fixture":
        payload = FixturePayload.model_validate(payload).model_dump()
        fields = ROOT_FIELDS["fixture"]
    else:
        payload = {key: value for key, value in payload.items() if key in ROOT_FIELDS[kind]}
        fields = (
            CONFIG_FIELDS
            if source_key == "kubernetes.configmap.metadata"
            else {
                "state": STATE_FIELDS,
                "events": EVENT_FIELDS,
                "logs": LOG_FIELDS,
                "series": SERIES_FIELDS,
            }[kind]
        )
    cleaned = _clean(payload, budget=budget, fields=fields)
    if kind == "logs":
        cleaned = _bound_log_segments(cleaned, {})
    truncated = cleaned != original
    # Remove list entries deterministically until the fixed allocation is satisfied.
    while len(_canonical(cleaned)) > byte_limit:
        lists = []

        def visit(v):
            if isinstance(v, dict):
                for item in v.values():
                    visit(item)
            elif isinstance(v, list) and v:
                lists.append(v)
                for item in v:
                    visit(item)

        visit(cleaned)
        if not lists:
            raise ValueError("Evidence scalar exceeds source allocation.")
        largest = max(lists, key=lambda item: len(_canonical(item)))
        del largest[(len(largest) * 3) // 4 :]
        truncated = True
    return cleaned, truncated


def store_batch(
    db: Session, run_id: UUID, workload_uid: str, results: list[SourceResult]
) -> list[Evidence]:
    # Read the foreign key without a Run lock, then establish Incident -> Run order.
    incident_id = db.scalar(
        select(InvestigationRun.incident_id).where(InvestigationRun.id == run_id)
    )
    if incident_id is None:
        raise ValueError("Unknown investigation.")
    incident = db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
    run = db.scalar(select(InvestigationRun).where(InvestigationRun.id == run_id).with_for_update())
    workload = db.get(Workload, incident.workload_id)
    if not workload or workload.uid != workload_uid or len(workload_uid) > 100:
        raise ValueError("Investigation workload identity mismatch.")
    keys = [r.source_key for r in results]
    if len(keys) != len(set(keys)) or set(keys) != set(SOURCES):
        raise ValueError("Collection must contain each catalog source exactly once.")
    window = (
        None
        if run.window_start is None
        else {
            "start": run.window_start.isoformat(),
            "end": run.window_end.isoformat(),
            "source": run.window_source,
        }
    )
    existing = {
        e.source_key: e
        for e in db.scalars(
            select(Evidence).where(
                Evidence.run_id == run_id, Evidence.catalog_version == CATALOG_VERSION
            )
        ).all()
    }
    rows = []
    envelopes = []
    total = 0
    samples_left = 20000
    pods_seen, containers_seen = set(), set()
    for result in sorted(results, key=lambda r: r.source_key):
        spec = SOURCES[result.source_key]
        if result.payload_kind != spec.payload_kind or result.payload_version != 1:
            raise ValueError("Invalid source payload version or kind.")
        if len(result.coverage.reason_codes) > 16 or set(result.coverage.reason_codes) - REASONS:
            raise ValueError("Unknown evidence coverage reason.")
        if result.coverage.status == "not_applicable" and result.source_key not in {
            "kubernetes.logs.current",
            "kubernetes.logs.previous",
        }:
            raise ValueError("Only conditional log sources may be not applicable.")
        if (
            result.source_times.time_relation != "not_captured"
            and window is None
            and result.source_key != "development_fixture"
        ):
            raise ValueError("A historical request has no captured window.")
        if result.query_metadata is not None:
            if spec.query_id is None or result.query_metadata.get("query_id") != spec.query_id:
                raise ValueError("Invalid query identifier.")
            query = _clean(result.query_metadata, fields=QUERY_FIELDS)
            identity = query.get("source_identity", {})
            if any(
                key in identity and identity[key] != expected
                for key, expected in (
                    ("cluster_id", workload.cluster),
                    ("namespace", workload.namespace),
                    ("workload_uid", workload_uid),
                )
            ):
                raise ValueError("Query metadata workload identity mismatch.")
            if len(_canonical(query)) > 16384:
                raise ValueError("Query metadata exceeds allocation.")
        else:
            query = None
        payload = result.redacted_bounded_payload
        if payload is not None and window is None and result.payload_kind in {"logs", "series"}:
            raise ValueError("Live log and metric payloads require a captured request window.")
        if (payload is None) != (result.coverage.status in {"unavailable", "not_applicable"}):
            raise ValueError("Coverage and payload disagree.")
        coverage = result.coverage.model_dump()
        digest = None
        if payload is not None:
            budget = {
                "pods_seen": pods_seen,
                "containers_seen": containers_seen,
                "samples": samples_left,
                "series": 25,
                "sample_gaps": False,
            }
            payload, truncated = _bounded(
                payload, result.payload_kind, spec.byte_limit, budget, result.source_key
            )
            samples_left = budget["samples"]
            if result.payload_kind == "series":
                count = _count_samples(payload)
                if budget["sample_gaps"] or count == 0:
                    coverage["reason_codes"] = sorted(
                        set(coverage["reason_codes"] + ["no_samples"])
                    )
                    coverage["status"] = "partial" if count else "unavailable"
                if count == 0:
                    payload = None
            if payload is not None:
                data = _canonical(payload)
                total += len(data)
                digest = hashlib.sha256(data).hexdigest()
            if truncated:
                if coverage["status"] == "available":
                    coverage["status"] = "partial"
                coverage["reason_codes"] = sorted(set(coverage["reason_codes"] + ["truncated"]))
        values = dict(
            workload_uid=workload_uid,
            source_kind=spec.source_kind,
            schema_version=2,
            payload_kind=result.payload_kind,
            payload_version=result.payload_version,
            observed_at=result.source_times.observed_at,
            collected_at=result.source_times.collected_at,
            requested_window=window,
            time_relation=result.source_times.time_relation,
            coverage=coverage,
            query_metadata=query,
            sha256=digest,
            redaction_policy_version="v1",
            expires_at=result.source_times.collected_at + timedelta(days=30)
            if payload is not None
            else None,
        )
        # Measure the actual bounded metadata shape, including IDs and nullable references.
        envelope = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in values.items()
        }
        envelope.update(
            id="0" * 36,
            run_id=str(run_id),
            source_key=result.source_key,
            catalog_version=CATALOG_VERSION,
            pinned=False,
            payload_ref="0" * 36 if payload is not None else None,
            content_status="available" if payload is not None else "unavailable",
        )
        envelopes.append(envelope)
        old = existing.get(result.source_key)
        if old:
            if any(getattr(old, k) != v for k, v in values.items()):
                raise ValueError("Conflicting immutable evidence publication.")
            rows.append(old)
        else:
            row = Evidence(
                run_id=run_id,
                source_key=result.source_key,
                catalog_version=CATALOG_VERSION,
                payload=payload,
                **values,
            )
            db.add(row)
            rows.append(row)
    if total > 7_864_320:
        raise ValueError("Evidence batch exceeds content budget.")
    packs = derive_pack_coverage({r.source_key: Coverage.model_validate(r.coverage) for r in rows})
    envelope_bytes = len(_canonical({"evidence": envelopes, "pack_coverage": packs}))
    if envelope_bytes > 512 * 1024 or total + envelope_bytes > 8 * 1024 * 1024:
        raise ValueError("Evidence metadata exceeds the Run envelope budget.")
    run.pack_coverage = packs
    db.flush()
    # Serialize with purge, including idempotent re-publication of older rows.
    db.scalars(
        select(Evidence.id)
        .where(Evidence.id.in_([r.id for r in rows]))
        .order_by(Evidence.id)
        .with_for_update()
    ).all()
    useful = [
        v
        for v in evidence_views(db, rows)
        if v.content_status == "available" and v.coverage.status in {"available", "partial"}
    ]
    if useful and not existing and incident.state != "resolved":
        db.execute(
            delete(EvidencePin).where(
                EvidencePin.kind == "latest", EvidencePin.owner_id == incident.id
            )
        )
        db.add_all(
            [EvidencePin(evidence_id=v.id, kind="latest", owner_id=incident.id) for v in useful]
        )
    return rows


def evidence_views(db: Session, rows: list[Evidence]) -> list[EvidenceView]:
    if not rows:
        return []
    ids = [r.id for r in rows]
    pinned = set(
        db.scalars(select(EvidencePin.evidence_id).where(EvidencePin.evidence_id.in_(ids))).all()
    )
    present = set(
        db.scalars(
            select(Evidence.id).where(Evidence.id.in_(ids), Evidence.payload.is_not(None))
        ).all()
    )
    now = database_now(db)
    result = []
    for row in rows:
        status = (
            "unavailable"
            if row.sha256 is None
            else (
                "expired"
                if row.id not in present
                or (row.id not in pinned and row.expires_at is not None and row.expires_at <= now)
                else "available"
            )
        )
        result.append(
            EvidenceView(
                id=row.id,
                run_id=row.run_id,
                workload_uid=row.workload_uid,
                source_key=row.source_key,
                source_kind=row.source_kind,
                catalog_version=row.catalog_version,
                schema_version=row.schema_version,
                payload_kind=row.payload_kind,
                payload_version=row.payload_version,
                observed_at=row.observed_at,
                collected_at=row.collected_at,
                requested_window=row.requested_window,
                time_relation=row.time_relation,
                coverage=row.coverage,
                query_metadata=row.query_metadata,
                sha256=row.sha256,
                redaction_policy_version=row.redaction_policy_version,
                payload_ref=row.id if status == "available" else None,
                expires_at=row.expires_at,
                pinned=row.id in pinned,
                content_status=status,
            )
        )
    return result


def pin_resolution(db: Session, incident_id: UUID, resolution_id: UUID) -> None:
    rows = db.scalars(
        select(Evidence)
        .join(InvestigationRun, Evidence.run_id == InvestigationRun.id)
        .join(Incident, InvestigationRun.incident_id == Incident.id)
        .join(Workload, Incident.workload_id == Workload.id)
        .where(InvestigationRun.incident_id == incident_id, Evidence.workload_uid == Workload.uid)
        .order_by(Evidence.id)
        .with_for_update(of=Evidence)
    ).all()
    for view in evidence_views(db, rows):
        if view.content_status == "available":
            db.add(EvidencePin(evidence_id=view.id, kind="resolution", owner_id=resolution_id))
    db.execute(
        delete(EvidencePin).where(EvidencePin.kind == "latest", EvidencePin.owner_id == incident_id)
    )
    db.flush()


def pin_latest_on_reopen(db: Session, incident_id: UUID) -> None:
    """The lifecycle caller holds Incident; pin the newest usable original-UID Run."""
    while True:
        now = database_now(db)
        candidate = db.execute(
            select(InvestigationRun.id, Workload.uid)
            .join(Evidence, Evidence.run_id == InvestigationRun.id)
            .join(Incident, InvestigationRun.incident_id == Incident.id)
            .join(Workload, Incident.workload_id == Workload.id)
            .where(
                Incident.id == incident_id,
                Evidence.workload_uid == Workload.uid,
                Evidence.payload.is_not(None),
                Evidence.sha256.is_not(None),
                or_(
                    Evidence.schema_version == 1,
                    Evidence.coverage["status"].astext.in_(("available", "partial")),
                ),
                or_(
                    Evidence.expires_at.is_(None),
                    Evidence.expires_at > now,
                    exists().where(EvidencePin.evidence_id == Evidence.id),
                ),
            )
            .order_by(InvestigationRun.created_at.desc(), InvestigationRun.id.desc())
            .limit(1)
        ).first()
        if candidate is None:
            return
        run_id, workload_uid = candidate
        rows = db.scalars(
            select(Evidence)
            .where(Evidence.run_id == run_id, Evidence.workload_uid == workload_uid)
            .order_by(Evidence.id)
            .with_for_update()
        ).all()
        useful = [
            view
            for view in evidence_views(db, rows)
            if view.content_status == "available"
            and (view.schema_version == 1 or view.coverage.status in {"available", "partial"})
        ]
        if not useful:
            # Purge or expiry won after selection. A fresh snapshot can select an older Run.
            continue
        db.execute(
            delete(EvidencePin).where(
                EvidencePin.kind == "latest", EvidencePin.owner_id == incident_id
            )
        )
        db.add_all(
            EvidencePin(evidence_id=view.id, kind="latest", owner_id=incident_id) for view in useful
        )
        db.flush()
        return
