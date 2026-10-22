"""Versioned, deterministic development collection. No external credentials or clients."""

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.investigations.schemas import FixturePayload
from cloudops.features.workloads.service import identity
from cloudops.shared.config import settings
from cloudops.shared.database import database_now, sessions


@dataclass(frozen=True)
class CollectionInput:
    workload_uid: str
    workload: str


@dataclass(frozen=True)
class CollectionResult:
    schema_version: int
    workload_uid: str
    observed_at: datetime
    collected_at: datetime
    payload: FixturePayload


def prepare(subject_id: UUID) -> CollectionInput:
    with sessions()() as db:
        run = db.get(InvestigationRun, subject_id)
        # Workload identity remains readable even if removed from the current allowlist.
        from cloudops.features.incidents.service import get_incident

        incident = get_incident(db, run.incident_id)
        workload = identity(db, incident.workload_id)
        return CollectionInput(
            workload.uid, f"{workload.cluster}/{workload.namespace}/{workload.name}"
        )


def collect(context: CollectionInput) -> CollectionResult:
    # Deliberately outside a database transaction. Delay/failure controls are development-only.
    time.sleep(settings().collector_delay_seconds)
    if settings().collector_fail:
        raise RuntimeError("Development collector failure requested.")
    now = datetime.now(timezone.utc)
    return CollectionResult(
        1,
        context.workload_uid,
        now,
        now,
        FixturePayload(
            label="Synthetic development evidence; no live cluster was contacted.",
            workload=context.workload,
            observation="Live workload health and telemetry are unavailable.",
        ),
    )


class CollectionHandler:
    def started(self, db: Session, subject_id: UUID):
        run = db.get(InvestigationRun, subject_id)
        run.state, run.reason = (
            "collecting",
            "Collecting a labeled development fixture; no live telemetry.",
        )

    def failed(self, db: Session, subject_id: UUID, reason: str, terminal: bool):
        run = db.get(InvestigationRun, subject_id)
        run.state = "failed" if terminal else "queued"
        run.reason = (
            reason if terminal else "Collection attempt failed. Waiting for a bounded retry."
        )
        run.finished_at = database_now(db) if terminal else None

    def publish(self, db: Session, subject_id: UUID, result: CollectionResult):
        if result.schema_version != 1:
            raise ValueError("Unsupported collection result version.")
        payload = result.payload.model_dump()
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        db.add(
            Evidence(
                run_id=subject_id,
                workload_uid=result.workload_uid,
                source="development_fixture",
                schema_version=result.schema_version,
                observed_at=result.observed_at,
                collected_at=result.collected_at,
                payload=payload,
                sha256=digest,
            )
        )
        run = db.get(InvestigationRun, subject_id)
        run.state = "insufficient_evidence"
        run.reason = "Fixture collection saved. Live evidence and AI diagnosis are unavailable; no recovery plan exists."
        run.finished_at = database_now(db)
