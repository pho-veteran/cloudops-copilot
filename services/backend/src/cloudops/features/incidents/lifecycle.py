import base64
import binascii
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session, operator
from cloudops.features.access.models import User
from cloudops.features.access.schemas import Principal
from cloudops.features.incidents import handoff as composer
from cloudops.features.incidents.models import Incident, IncidentActivity
from cloudops.features.incidents.schemas import IncidentDetail
from cloudops.features.incidents.service import cancel_pending, get_incident
from cloudops.features.recovery.classification import blocking_execution
from cloudops.features.recovery.service import invalidate
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES, DomainError
from cloudops.shared.schemas import View

router = APIRouter(prefix="/api/incidents", tags=["incident lifecycle"], responses=ERROR_RESPONSES)


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["acknowledge", "resolve", "reopen"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class ActivityView(View):
    id: UUID
    incident_id: UUID
    kind: str
    text: str
    actor_id: UUID
    actor_name: str | None
    created_at: datetime
    snapshot: dict | None


class ActivityPage(BaseModel):
    items: list[ActivityView]
    total: int
    next_cursor: str | None


class HandoffPreview(BaseModel):
    incident_id: UUID
    resolution_id: UUID | None
    schema_version: str
    snapshot: dict
    markdown: str


def _activity_view(row, actor_name):
    return ActivityView(
        id=row.id,
        incident_id=row.incident_id,
        kind=row.kind,
        text=row.text,
        actor_id=row.actor_id,
        actor_name=actor_name,
        created_at=row.created_at,
        snapshot=row.snapshot,
    )


def _cursor(incident_id, row):
    value = json.dumps(
        [str(incident_id), row.created_at.isoformat(), str(row.id)], separators=(",", ":")
    )
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _parse_cursor(value, incident_id):
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        data = json.loads(decoded)
        if (
            not isinstance(data, list)
            or len(data) != 3
            or not all(isinstance(item, str) for item in data)
            or data[0] != str(incident_id)
        ):
            raise ValueError
        at = datetime.fromisoformat(data[1])
        if at.utcoffset() is None:
            raise ValueError
        return at, UUID(data[2])
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error, OverflowError):
        raise DomainError(422, "invalid_cursor", "Invalid activity cursor.") from None


def _preview(db, incident_id, resolution_id):
    if resolution_id is None:
        snapshot = composer.compose(db, incident_id)
        return HandoffPreview(
            incident_id=incident_id,
            resolution_id=None,
            schema_version=composer.SCHEMA_VERSION,
            snapshot=snapshot,
            markdown=composer.render(snapshot),
        )
    row = db.get(IncidentActivity, resolution_id)
    if not row or row.incident_id != incident_id or row.kind != "resolve" or not row.snapshot:
        raise DomainError(
            404, "resolution_not_found", "Saved resolution not found for this incident."
        )
    stored = row.snapshot
    if "schema_version" not in stored:
        return HandoffPreview(
            incident_id=incident_id,
            resolution_id=resolution_id,
            schema_version=composer.LEGACY_VERSION,
            snapshot=stored,
            markdown=composer.legacy_markdown(stored),
        )
    if (
        stored.get("schema_version") != composer.SCHEMA_VERSION
        or not isinstance(stored.get("snapshot"), dict)
        or not isinstance(stored.get("markdown"), str)
    ):
        raise DomainError(
            422, "unsupported_handoff_snapshot", "Saved handoff format is unsupported."
        )
    return HandoffPreview(
        incident_id=incident_id,
        resolution_id=resolution_id,
        schema_version=composer.SCHEMA_VERSION,
        snapshot=stored["snapshot"],
        markdown=stored["markdown"],
    )


@router.post(
    "/{incident_id}/notes",
    operation_id="add_incident_note",
    response_model=ActivityView,
    status_code=201,
)
def note(
    incident_id: UUID,
    body: NoteBody,
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    with db.begin():
        if not db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update()):
            raise DomainError(404, "incident_not_found", "Incident not found.")
        row = IncidentActivity(
            incident_id=incident_id, kind="note", text=body.text, actor_id=actor.id
        )
        db.add(row)
        db.flush()
        return _activity_view(row, actor.username)


@router.post(
    "/{incident_id}/lifecycle", operation_id="transition_incident", response_model=IncidentDetail
)
def transition(
    incident_id: UUID,
    body: TransitionBody,
    actor: Principal = Depends(operator),
    db: Session = Depends(get_db),
):
    with db.begin():
        incident = db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
        if not incident:
            raise DomainError(404, "incident_not_found", "Incident not found.")
        expected = {"acknowledge": ("open", "acknowledged"), "reopen": ("resolved", "open")}
        if body.action == "resolve":
            if incident.state == "resolved":
                raise DomainError(
                    409, "incident_state_changed", "This incident is already resolved."
                )
            if blocking_execution(db, incident_id):
                raise DomainError(
                    409,
                    "execution_active",
                    "An admitted execution cannot be hidden by resolution. Wait for its recorded outcome.",
                )
            # Keep claim's Run -> plan order; queued work cannot outlive resolution.
            cancel_pending(db, incident_id, "incident_resolved")
            invalidate(db, incident_id)
            incident.state = "resolved"
        else:
            before, after = expected[body.action]
            if incident.state != before:
                raise DomainError(
                    409,
                    "incident_state_changed",
                    "Refresh the incident before making this transition.",
                )
            incident.state = after
        row = IncidentActivity(
            incident_id=incident_id, kind=body.action, text=body.reason, actor_id=actor.id
        )
        db.add(row)
        db.flush()
        if body.action == "resolve":
            snapshot = composer.compose(db, incident_id)
            row.snapshot = {
                "schema_version": composer.SCHEMA_VERSION,
                "snapshot": snapshot,
                "markdown": composer.render(snapshot),
            }
        return get_incident(db, incident_id)


@router.get(
    "/{incident_id}/activity",
    operation_id="list_incident_activity",
    response_model=ActivityPage,
    dependencies=[Depends(current_session)],
)
def activity(
    incident_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    db: Session = Depends(get_db),
):
    get_incident(db, incident_id)
    total = db.scalar(
        select(func.count())
        .select_from(IncidentActivity)
        .where(IncidentActivity.incident_id == incident_id)
    )
    query = (
        select(IncidentActivity, User.username)
        .outerjoin(User, User.id == IncidentActivity.actor_id)
        .where(IncidentActivity.incident_id == incident_id)
    )
    if cursor is not None:
        at, identity = _parse_cursor(cursor, incident_id)
        query = query.where(
            tuple_(IncidentActivity.created_at, IncidentActivity.id) < tuple_(at, identity)
        )
    rows = db.execute(
        query.order_by(IncidentActivity.created_at.desc(), IncidentActivity.id.desc()).limit(
            limit + 1
        )
    ).all()
    items = [_activity_view(row, name) for row, name in rows[:limit]]
    return ActivityPage(
        items=items,
        total=total,
        next_cursor=_cursor(incident_id, rows[limit - 1][0]) if len(rows) > limit else None,
    )


@router.get(
    "/{incident_id}/handoff/preview",
    operation_id="preview_incident_handoff",
    response_model=HandoffPreview,
    dependencies=[Depends(current_session)],
)
def preview(
    incident_id: UUID,
    resolution_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return _preview(db, incident_id, resolution_id)


@router.get(
    "/{incident_id}/handoff",
    operation_id="export_incident_handoff",
    response_class=PlainTextResponse,
    dependencies=[Depends(current_session)],
)
def handoff(
    incident_id: UUID,
    resolution_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
):
    result = _preview(db, incident_id, resolution_id)
    return PlainTextResponse(
        result.markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="incident-{incident_id}.md"'},
    )
