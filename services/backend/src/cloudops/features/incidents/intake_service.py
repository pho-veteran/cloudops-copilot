"""Authenticated Alertmanager intake over the transactional automatic-admission seam.

Each alert settles in its own short transaction so a permanent rejection never discards the
valid entries of a batch. Network identity reads happen before that transaction; the commit
rechecks the episode binding and the configured identity under the admission lock.

Lock order: admission lock -> episode -> Workload -> Incident/Run/Job (via admission) for new
work; admission lock -> episode -> Incident for repeats. Resolve takes Incident first but never
waits for the admission lock, so the orders cannot form a cycle.
"""

import base64
import hashlib
import hmac
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import delete, select, tuple_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from cloudops.features.incidents.coordination import lock_admission
from cloudops.features.incidents.intake_models import AlertEpisode, AlertReceipt
from cloudops.features.incidents.intake_schemas import (
    AlertEntry,
    AlertmanagerEnvelope,
    AlertReceiptPage,
    AlertReceiptView,
    IntakeResult,
)
from cloudops.features.incidents.intake_settings import (
    MAX_BODY_BYTES,
    RECEIPT_RETENTION,
    IntakeConfig,
    intake_config,
)
from cloudops.features.incidents.models import Incident
from cloudops.features.incidents.service import AutomaticRequest, admit_automatic
from cloudops.features.investigations.service import Origin
from cloudops.features.workloads.alert_identity import (
    IdentityResolver,
    Refused,
    Resolved,
    scope_snapshot,
)
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import database_now, sessions
from cloudops.shared.errors import DomainError

TOKEN = re.compile(r"[\x21-\x7e]{1,512}")
SEVERITY = re.compile(r"[A-Za-z0-9_.-]{1,32}")
# Tests inject an httpx transport; production reads the pinned cluster directly.
reader_transport = None


def authenticate(request: Request) -> IntakeConfig:
    """Machine authentication only: cookies and browser sessions never grant intake."""
    config = intake_config()
    if config is None:
        raise DomainError(
            503,
            "intake_unavailable",
            "Alert intake is not configured. Nothing was recorded; the sender may retry.",
        )
    values = request.headers.getlist("authorization")
    token = values[0][7:] if len(values) == 1 and values[0].startswith("Bearer ") else ""
    digest = hashlib.sha256(token.encode()).hexdigest() if TOKEN.fullmatch(token) else ""
    if not hmac.compare_digest(digest.encode(), config.token_sha256.encode()):
        raise DomainError(
            401, "intake_unauthorized", "Alert intake requires its configured sender credential."
        )
    return config


async def read_envelope(request: Request) -> AlertmanagerEnvelope:
    """Read at most MAX_BODY_BYTES, after authentication, then validate the v4 envelope."""
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/json":
        raise DomainError(
            415, "intake_media_type", "Send the Alertmanager webhook as application/json."
        )
    too_large = DomainError(
        413, "intake_too_large", "The notification exceeds the intake size bound."
    )
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise too_large
    try:
        return AlertmanagerEnvelope.model_validate_json(bytes(body))
    except ValidationError:
        raise DomainError(
            422,
            "intake_invalid",
            "The notification is not a supported Alertmanager webhook v4 payload.",
        ) from None


@dataclass(frozen=True)
class Facts:
    status: str
    fingerprint: str
    start_key: str
    starts_at: datetime
    alertname: str | None
    namespace: str | None
    target_kind: str | None
    target_name: str | None
    severity: str | None


def clean(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = "".join(c for c in value if c.isprintable()).strip()
    return text[:limit] or None


def facts(alert: AlertEntry) -> Facts:
    key, moment = alert.start_key
    labels = alert.labels
    kind = "pod" if labels.get("pod") else "deployment" if labels.get("deployment") else None
    severity = labels.get("severity")
    return Facts(
        status=alert.status,
        fingerprint=alert.fingerprint,
        start_key=key,
        starts_at=moment,
        alertname=clean(labels.get("alertname"), 100),
        namespace=clean(labels.get("namespace"), 100),
        target_kind=kind,
        target_name=clean(labels.get(kind), 253) if kind else None,
        severity=severity if severity and SEVERITY.fullmatch(severity) else None,
    )


def find(db: Session, config: IntakeConfig, f: Facts, *, lock=False) -> AlertEpisode | None:
    query = select(AlertEpisode).where(
        AlertEpisode.source_id == config.source_id,
        AlertEpisode.fingerprint == f.fingerprint,
        AlertEpisode.starts_at_key == f.start_key,
    )
    return db.scalar(query.with_for_update() if lock else query)


def record(db, config, delivery, f, now, outcome, reason=None, episode=None):
    db.add(
        AlertReceipt(
            delivery_id=delivery,
            received_at=now,
            source_id=config.source_id,
            alert_status=f.status,
            fingerprint=f.fingerprint,
            starts_at_key=f.start_key,
            alertname=f.alertname,
            namespace=f.namespace,
            target_kind=f.target_kind,
            target_name=f.target_name,
            outcome=outcome,
            reason=reason,
            episode_id=episode.id if episode else None,
            incident_id=episode.incident_id if episode else None,
            run_id=episode.run_id if episode else None,
            workload_id=episode.workload_id if episode else None,
        )
    )
    return outcome


def marker(db, config, f, now, outcome, reason, *, signal="firing") -> AlertEpisode:
    """A terminal episode with no work; later repeats of this exact episode stay inert."""
    episode = AlertEpisode(
        source_id=config.source_id,
        fingerprint=f.fingerprint,
        starts_at_key=f.start_key,
        starts_at=f.starts_at,
        outcome=outcome,
        reason=reason,
        signal=signal,
        first_received_at=now,
        last_received_at=now,
    )
    db.add(episode)
    db.flush()
    return episode


def repeat(db, config, delivery, f, episode: AlertEpisode, now) -> str:
    episode.last_received_at = now
    outcome = "duplicate"
    if episode.outcome == "accepted":
        incident = db.get(Incident, episode.incident_id)
        if f.status == "resolved" and episode.signal != "resolved":
            # A signal only: lifecycle, Runs and recovery state are untouched.
            episode.signal = incident.alert_signal = "resolved"
            outcome = "resolved_signal"
        elif f.status == "firing" and episode.signal == "firing":
            incident.last_seen_at = now
    return record(db, config, delivery, f, now, outcome, episode.reason, episode)


class _Remap(Exception):
    """The episode disappeared between the unlocked check and the commit; resolve again."""


def settle(db: Session, config, delivery, f: Facts, resolution) -> str:
    lock_admission(db)
    now = database_now(db)
    episode = find(db, config, f, lock=True)
    if episode:
        return repeat(db, config, delivery, f, episode, now)
    if f.status == "resolved":
        reason = "resolved_before_admission"
        episode = marker(db, config, f, now, "ignored", reason, signal="resolved")
        return record(db, config, delivery, f, now, "ignored", reason, episode)
    if resolution is None:
        raise _Remap()
    if isinstance(resolution, Refused):
        if resolution.retry:
            return record(db, config, delivery, f, now, "retryable", resolution.reason)
        episode = marker(db, config, f, now, "rejected", resolution.reason)
        return record(db, config, delivery, f, now, "rejected", resolution.reason, episode)
    workload = db.scalar(
        select(Workload).where(Workload.id == resolution.workload_id).with_for_update()
    )
    if (
        not workload
        or not workload.active
        or workload.cluster != config.cluster
        or workload.source != "live_kubernetes"
        or workload.uid != resolution.uid
    ):
        # Scope changed after the read; never attach this episode to a replacement.
        replaced = workload is not None and db.scalar(
            select(Workload.id).where(
                Workload.active,
                Workload.cluster == workload.cluster,
                Workload.namespace == workload.namespace,
                Workload.name == workload.name,
                Workload.id != workload.id,
            )
        )
        reason = (
            "identity_changed" if replaced or (workload and workload.active) else "out_of_scope"
        )
        episode = marker(db, config, f, now, "rejected", reason)
        return record(db, config, delivery, f, now, "rejected", reason, episode)
    return admit(db, config, delivery, f, resolution, workload, now)


def admit(db, config, delivery, f: Facts, resolution: Resolved, workload, now) -> str:
    bound = {}

    def lookup(session):
        row = find(session, config, f)
        return (row.incident_id, row.run_id) if row and row.outcome == "accepted" else None

    def bind(session, view):
        episode = AlertEpisode(
            source_id=config.source_id,
            fingerprint=f.fingerprint,
            starts_at_key=f.start_key,
            starts_at=f.starts_at,
            outcome="accepted",
            workload_id=workload.id,
            workload_uid=workload.uid,
            incident_id=view.incident_id,
            run_id=view.run_id,
            signal="firing",
            first_received_at=now,
            last_received_at=now,
        )
        session.add(episode)
        session.flush()
        incident = session.get(Incident, view.incident_id)
        incident.first_seen_at, incident.last_seen_at = f.starts_at, now
        incident.alert_signal = "firing"
        if f.severity:
            incident.severity, incident.severity_provenance = f.severity, "alert_label"
        bound["episode"] = episode

    alertname = f.alertname or "Alertmanager alert"
    target = f"{resolution.namespace}/{resolution.name}"
    view = admit_automatic(
        db,
        Origin.alertmanager(config.source_id),
        AutomaticRequest(
            workload_id=workload.id,
            title=f"{alertname} · {target}"[:160],
            description=(
                f"Opened by authenticated Alertmanager intake from source {config.source_id}. "
                "Alert annotations are not stored."
            ),
            question=f"Why is {alertname} firing for {target}?",
        ),
        lookup=lookup,
        bind=bind,
    )
    episode = bound.get("episode") or find(db, config, f)
    outcome = "deferred" if view.queue_reason == "capacity" else "accepted"
    return record(db, config, delivery, f, now, outcome, None, episode)


def retryable(config, delivery, f: Facts, reason: str) -> str:
    try:
        with sessions()() as db, db.begin():
            record(db, config, delivery, f, database_now(db), "retryable", reason)
    except SQLAlchemyError:
        pass
    return "retryable"


def handle(config, delivery, alert: AlertEntry, resolver: IdentityResolver) -> str:
    f = facts(alert)
    with sessions()() as db:
        known = find(db, config, f) is not None
    # Existing bindings and markers are consulted before any remapping read.
    resolution = None if known or f.status == "resolved" else resolver.resolve(alert.labels)
    for _ in range(2):
        try:
            with sessions()() as db, db.begin():
                return settle(db, config, delivery, f, resolution)
        except _Remap:
            resolution = resolver.resolve(alert.labels)
        except IntegrityError:
            continue  # A concurrent writer bound this episode; settle again as a repeat.
        except DomainError as error:
            reason = "capacity_exhausted" if error.code == "capacity_exhausted" else None
            return retryable(config, delivery, f, reason or "admission_conflict")
        except SQLAlchemyError:
            return retryable(config, delivery, f, "database_unavailable")
    return retryable(config, delivery, f, "database_unavailable")


def prune() -> None:
    try:
        with sessions()() as db, db.begin():
            cutoff = db.execute(
                select(AlertReceipt.received_at, AlertReceipt.id)
                .order_by(AlertReceipt.received_at.desc(), AlertReceipt.id.desc())
                .offset(RECEIPT_RETENTION)
                .limit(1)
            ).first()
            if cutoff:
                db.execute(
                    delete(AlertReceipt).where(
                        tuple_(AlertReceipt.received_at, AlertReceipt.id) <= tuple(cutoff)
                    )
                )
    except SQLAlchemyError:
        pass  # Retention is best effort; the next delivery prunes again.


def receive(config: IntakeConfig, envelope: AlertmanagerEnvelope) -> IntakeResult:
    delivery = uuid4()
    with sessions()() as db:
        snapshot = scope_snapshot(db, config.cluster)
    resolver = IdentityResolver(config.cluster, snapshot, transport=reader_transport)
    try:
        outcomes = Counter(handle(config, delivery, alert, resolver) for alert in envelope.alerts)
    finally:
        resolver.close()
    prune()
    if outcomes["retryable"]:
        raise DomainError(
            503,
            "intake_retry",
            "Some alerts were not admitted. Retry the delivery; committed alerts deduplicate.",
        )
    return IntakeResult(delivery_id=delivery, counts=dict(outcomes))


def encode_cursor(row: AlertReceipt) -> str:
    raw = json.dumps([row.received_at.isoformat(), str(row.id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def receipts_page(db: Session, limit: int, cursor: str | None) -> AlertReceiptPage:
    query = select(AlertReceipt)
    if cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if not isinstance(data, list) or len(data) != 2:
                raise ValueError
            moment, identity = datetime.fromisoformat(data[0]), UUID(data[1])
            if moment.tzinfo is None:
                raise ValueError
        except (ValueError, TypeError, IndexError):
            raise DomainError(
                422, "invalid_cursor", "Use the cursor returned by alert receipt history."
            ) from None
        query = query.where(tuple_(AlertReceipt.received_at, AlertReceipt.id) < (moment, identity))
    rows = db.scalars(
        query.order_by(AlertReceipt.received_at.desc(), AlertReceipt.id.desc()).limit(limit + 1)
    ).all()
    config = intake_config()
    return AlertReceiptPage(
        configured=config is not None,
        source_id=config.source_id if config else None,
        items=[AlertReceiptView.model_validate(r) for r in rows[:limit]],
        next_cursor=encode_cursor(rows[limit - 1]) if len(rows) > limit else None,
    )
