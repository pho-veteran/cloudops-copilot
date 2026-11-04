"""Stored-data handoff composition. This module never calls a provider or executes work."""

import html
import json
import re
from typing import Literal
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from cloudops.features.access.models import User
from cloudops.features.incidents.models import Incident, IncidentActivity
from cloudops.features.incidents.schemas import IncidentView
from cloudops.features.investigations.models import Evidence, InvestigationRun
from cloudops.features.recovery.classification import (
    BLOCKING,
    classification_expression,
    classify,
    terminal_unknown_eligible,
)
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.workloads.models import Workload
from cloudops.shared.errors import DomainError
from cloudops.shared.jobs import Job

SCHEMA_VERSION = "HandoffSnapshot/v1"
LEGACY_VERSION = "legacy"


class FixtureFinding(BaseModel):
    """Pure-composer contract fixture, not a production findings producer."""

    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=4000)
    status: Literal["ruled_out", "unverified"]
    run_id: UUID
    evidence_ids: list[UUID] = Field(min_length=1)


class FixtureFindings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["Contract fixture; not production findings"]
    items: list[FixtureFinding]


def _fields(row, names):
    return jsonable_encoder({name: getattr(row, name) for name in names})


def compose(db, incident_id, *, findings_fixture: FixtureFindings | None = None):
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise DomainError(404, "incident_not_found", "Incident not found.")
    workload = db.get(Workload, incident.workload_id)
    activity = db.execute(
        select(IncidentActivity, User.username)
        .outerjoin(User, User.id == IncidentActivity.actor_id)
        .where(IncidentActivity.incident_id == incident_id)
        .order_by(IncidentActivity.created_at, IncidentActivity.id)
    ).all()
    runs = db.scalars(
        select(InvestigationRun)
        .where(InvestigationRun.incident_id == incident_id)
        .order_by(InvestigationRun.created_at, InvestigationRun.id)
    ).all()
    evidence = db.scalars(
        select(Evidence)
        .join(InvestigationRun, Evidence.run_id == InvestigationRun.id)
        .where(InvestigationRun.incident_id == incident_id)
        .order_by(Evidence.collected_at, Evidence.id)
    ).all()
    # Plans are collected after executions so that cross-incident lock owners are
    # represented by the very same plan records as their execution references.
    executions = db.execute(
        select(Execution, ActionPlan.incident_id, Job.state)
        .join(ActionPlan, ActionPlan.id == Execution.plan_id)
        .outerjoin(Job, (Job.kind == "recovery_execution_v1") & (Job.subject_id == Execution.id))
        .where(
            or_(
                ActionPlan.incident_id == incident_id,
                (Execution.workload_id == incident.workload_id)
                & or_(
                    Execution.holds_lock.is_(True),
                    classification_expression(
                        Execution.state, Execution.holds_lock, Execution.maintenance_released
                    ).in_(BLOCKING),
                ),
            )
        )
        .order_by(Execution.created_at, Execution.id)
    ).all()
    referenced_plans = {row.plan_id for row, _, _ in executions}
    plans = db.scalars(
        select(ActionPlan)
        .where(or_(ActionPlan.incident_id == incident_id, ActionPlan.id.in_(referenced_plans)))
        .order_by(ActionPlan.created_at, ActionPlan.id)
    ).all()
    events = [
        {**_fields(row, ("id", "kind", "text", "actor_id", "created_at")), "actor_name": name}
        for row, name in activity
    ]
    run_rows = [
        _fields(
            row,
            (
                "id",
                "state",
                "reason",
                "created_at",
                "finished_at",
                "requested_by",
                "origin_kind",
                "origin_source_id",
                "question",
                "question_source",
                "window_start",
                "window_end",
                "window_source",
                "queue_reason",
                "outcome_code",
            ),
        )
        for row in runs
    ]
    evidence_rows = [
        _fields(
            row,
            (
                "id",
                "run_id",
                "workload_uid",
                "source",
                "schema_version",
                "observed_at",
                "collected_at",
                "sha256",
            ),
        )
        for row in evidence
    ]
    plan_rows = [
        _fields(
            row,
            (
                "id",
                "incident_id",
                "workload_id",
                "version",
                "template",
                "content",
                "digest",
                "state",
                "rationale",
                "created_by",
                "created_at",
                "expires_at",
                "decided_by",
                "decided_at",
            ),
        )
        for row in plans
    ]
    execution_rows = [
        {
            **_fields(
                row,
                (
                    "id",
                    "plan_id",
                    "workload_id",
                    "state",
                    "holds_lock",
                    "maintenance_released",
                    "result",
                    "created_at",
                    "finished_at",
                ),
            ),
            "incident_id": str(owner),
            "classification": classify(row.state, row.holds_lock, row.maintenance_released),
            "job_state": job_state,
            "terminal_unknown_handoff_eligible": terminal_unknown_eligible(
                row.state, row.holds_lock, row.maintenance_released, job_state, row.result
            ),
        }
        for row, owner, job_state in executions
    ]
    locks = [
        row for row in execution_rows if row["holds_lock"] or row["classification"] in BLOCKING
    ]
    snapshot = {
        "incident": IncidentView.model_validate(incident).model_dump(mode="json"),
        "workload": _fields(
            workload,
            (
                "id",
                "cluster",
                "namespace",
                "name",
                "uid",
                "active",
                "source",
                "retirement_reason",
            ),
        ),
        "impact": {
            "status": "not_recorded",
            "detail": "Impact is not recorded by the current producer.",
        },
        "runs": {"status": "complete", "count": len(run_rows), "items": run_rows},
        "evidence": {
            "status": "complete" if evidence_rows else "not_recorded",
            "count": len(evidence_rows),
            "items": evidence_rows,
        },
        "actions": {
            "status": "complete" if plan_rows or execution_rows else "not_recorded",
            "plan_count": len(plan_rows),
            "incident_plan_count": sum(row.incident_id == incident_id for row in plans),
            "plans": plan_rows,
            "execution_count": len(execution_rows),
            "executions": execution_rows,
        },
        "target_locks": {"status": "complete", "count": len(locks), "items": locks},
        "activity": {"status": "complete", "count": len(events), "items": events},
        "findings": {
            "status": "unavailable",
            "detail": "Production RCA and findings are unavailable.",
        },
        "ruled_out_checks": {
            "status": "unavailable",
            "detail": "No structured ruled-out checks are recorded.",
        },
        "unverified_checks": {
            "status": "unavailable",
            "detail": "No structured unverified checks are recorded.",
        },
        "open_questions": {
            "status": "unavailable",
            "detail": "No structured open questions are recorded; Run requests are listed above.",
        },
        "limitations": "Stored records and human notes only; no new diagnosis was generated. Unknown outcomes and target locks remain independent of incident resolution.",
    }
    if findings_fixture is not None:
        fixture = FixtureFindings.model_validate(findings_fixture)
        evidence_by_run = {(str(row.run_id), str(row.id)) for row in evidence}
        for finding in fixture.items:
            if any(
                (str(finding.run_id), str(eid)) not in evidence_by_run
                for eid in finding.evidence_ids
            ):
                raise ValueError("Fixture finding references evidence outside its stored Run.")
        items = [item.model_dump(mode="json") for item in fixture.items]
        snapshot["findings"] = {
            "status": "contract_fixture",
            "label": fixture.label,
            "count": len(items),
            "items": items,
        }
        for status, section in (
            ("ruled_out", "ruled_out_checks"),
            ("unverified", "unverified_checks"),
        ):
            matching = [item for item in items if item["status"] == status]
            snapshot[section] = {
                "status": "contract_fixture",
                "label": fixture.label,
                "count": len(matching),
                "items": matching,
            }
    return snapshot


def _fenced(value):
    content = json.dumps(value, ensure_ascii=False, indent=2)
    fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", content)), default=0))
    return f"{fence}json\n{content}\n{fence}\n"


def legacy_markdown(snapshot):
    # This is the original renderer; legacy JSON must never pass through a new schema.
    return "# Incident handoff\n\n" + _fenced(snapshot)


def _inline(value):
    """Keep untrusted single-line metadata from becoming Markdown or raw HTML."""
    if value is None or value == "":
        return "Not recorded"
    value = html.escape(str(value).replace("\r", " ").replace("\n", " "), quote=True)
    return re.sub(r"([\\`*{}\[\]()#+!|>~])", r"\\\1", value)


def _text_block(value):
    """Fence human text verbatim, including text that itself contains code fences."""
    if value is None or value == "":
        return "Not recorded.\n\n"
    fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", value)), default=0))
    return f"{fence}text\n{value}\n{fence}\n\n"


def render(snapshot):
    """Render the stored projection as readable Markdown, without interpreting human text."""
    incident = snapshot["incident"]
    workload = snapshot["workload"]
    severity = incident["severity"]
    sections = [
        "# Incident handoff\n\n",
        "## Incident and workload\n\n",
        f"- Incident: {_inline(incident['id'])}\n",
        f"- State: {_inline(incident['state'])}\n",
        f"- Created: {_inline(incident['created_at'])}; actor: {_inline(incident['created_by'])}\n",
        f"- Origin: {_inline(incident['origin']['kind'])}; source: {_inline(incident['origin']['source_id'])}\n",
        f"- Workload: {_inline(workload['name'])}; namespace: {_inline(workload['namespace'])}; cluster: {_inline(workload['cluster'])}\n",
        f"- Workload ID: {_inline(workload['id'])}; UID: {_inline(workload['uid'])}; source: {_inline(workload['source'])}\n",
        f"- Severity: {_inline(severity['value'])}; provenance: {_inline(severity['provenance'])}\n",
        f"- Alert signal: {_inline(incident['alert_signal'])}; first seen: {_inline(incident['first_seen_at'])}; last seen: {_inline(incident['last_seen_at'])}\n\n",
        "**Title**\n\n",
        _text_block(incident["title"]),
        "**Description**\n\n",
        _text_block(incident["description"]),
        "## Impact and provenance\n\n",
        f"{_inline(snapshot['impact']['detail'])}\n\n",
        f"## Runs and requests ({snapshot['runs']['count']})\n\n",
    ]
    if not snapshot["runs"]["items"]:
        sections.append("No stored Runs.\n\n")
    for run in snapshot["runs"]["items"]:
        sections.extend(
            [
                f"### Run {_inline(run['id'])}\n\n",
                f"- State: {_inline(run['state'])}; created: {_inline(run['created_at'])}; finished: {_inline(run['finished_at'])}\n",
                f"- Requested by: {_inline(run['requested_by'])}; origin: {_inline(run['origin_kind'])}; source: {_inline(run['origin_source_id'])}\n",
                f"- Window: {_inline(run['window_start'])} to {_inline(run['window_end'])}; provenance: {_inline(run['window_source'])}\n",
                f"- Question provenance: {_inline(run['question_source'])}; queue reason: {_inline(run['queue_reason'])}; outcome: {_inline(run['outcome_code'])}\n\n",
                "**Question**\n\n",
                _text_block(run["question"]),
                "**Recorded Run reason**\n\n",
                _text_block(run["reason"]),
            ]
        )
    sections.append(f"## Evidence references ({snapshot['evidence']['count']})\n\n")
    if not snapshot["evidence"]["items"]:
        sections.append("No Evidence references recorded.\n\n")
    for evidence in snapshot["evidence"]["items"]:
        sections.append(
            f"- Evidence {_inline(evidence['id'])}; Run {_inline(evidence['run_id'])}; "
            f"source {_inline(evidence['source'])} v{_inline(evidence['schema_version'])}; "
            f"workload UID {_inline(evidence['workload_uid'])}; observed {_inline(evidence['observed_at'])}; "
            f"collected {_inline(evidence['collected_at'])}; SHA-256 {_inline(evidence['sha256'])}\n"
        )
    actions = snapshot["actions"]
    sections.append(
        f"\n## Actions and outcomes ({actions['plan_count']} plans; "
        f"{actions['execution_count']} executions)\n\n"
    )
    if not actions["plans"]:
        sections.append("No action plans recorded.\n\n")
    for plan in actions["plans"]:
        sections.extend(
            [
                f"### Plan {_inline(plan['id'])}\n\n",
                f"- Incident: {_inline(plan['incident_id'])}; workload: {_inline(plan['workload_id'])}\n",
                f"- Template: {_inline(plan['template'])}; state: {_inline(plan['state'])}; version: {_inline(plan['version'])}\n",
                f"- Created: {_inline(plan['created_at'])} by {_inline(plan['created_by'])}; decision: {_inline(plan['decided_at'])} by {_inline(plan['decided_by'])}\n",
                f"- Expires: {_inline(plan['expires_at'])}; digest: {_inline(plan['digest'])}\n\n",
                "**Recorded rationale**\n\n",
                _text_block(plan["rationale"]),
            ]
        )
    if not actions["executions"]:
        sections.append("No executions recorded.\n\n")
    for execution in actions["executions"]:
        result = execution["result"] if isinstance(execution["result"], dict) else {}
        sections.extend(
            [
                f"### Execution {_inline(execution['id'])}\n\n",
                f"- Plan: {_inline(execution['plan_id'])}; incident: {_inline(execution['incident_id'])}; workload: {_inline(execution['workload_id'])}\n",
                f"- State: {_inline(execution['state'])}; classification: {_inline(execution['classification'])}; Job state: {_inline(execution['job_state'])}\n",
                f"- Holds lock: {_inline(execution['holds_lock'])}; maintenance released: {_inline(execution['maintenance_released'])}; terminal unknown handoff eligible: {_inline(execution['terminal_unknown_handoff_eligible'])}\n",
                f"- Created: {_inline(execution['created_at'])}; finished: {_inline(execution['finished_at'])}\n",
                f"- Outcome: {_inline(result.get('outcome'))}; verification: {_inline(result.get('verification'))}\n\n",
            ]
        )
        if isinstance(result.get("reason"), str):
            sections.extend(["**Recorded outcome reason**\n\n", _text_block(result["reason"])])
    locks = snapshot["target_locks"]
    sections.append(f"## Retained target locks and blockers ({locks['count']})\n\n")
    if not locks["items"]:
        sections.append("No retained target locks or execution blockers recorded.\n\n")
    for lock in locks["items"]:
        sections.append(
            f"- Execution {_inline(lock['id'])}; plan {_inline(lock['plan_id'])}; "
            f"incident {_inline(lock['incident_id'])}; workload {_inline(lock['workload_id'])}; "
            f"state {_inline(lock['state'])}; classification {_inline(lock['classification'])}; "
            f"holds lock {_inline(lock['holds_lock'])}\n"
        )
    sections.append("\n")
    for title, key in (
        ("Findings", "findings"),
        ("Ruled-out checks", "ruled_out_checks"),
        ("Unverified checks", "unverified_checks"),
        ("Open questions", "open_questions"),
    ):
        section = snapshot[key]
        sections.append(f"## {title}\n\n")
        if section["status"] == "contract_fixture":
            sections.append(f"{_inline(section['label'])}; {section['count']} recorded.\n\n")
            for item in section["items"]:
                sections.extend(
                    [
                        f"- Status: {_inline(item['status'])}; Run: {_inline(item['run_id'])}; "
                        f"Evidence: {', '.join(_inline(eid) for eid in item['evidence_ids'])}\n\n",
                        _text_block(item["summary"]),
                    ]
                )
        else:
            sections.append(f"{_inline(section['detail'])}\n\n")
    sections.append(f"## Activity and notes ({snapshot['activity']['count']})\n\n")
    if not snapshot["activity"]["items"]:
        sections.append("No activity recorded.\n\n")
    for event in snapshot["activity"]["items"]:
        sections.extend(
            [
                f"### {_inline(event['kind'])} · {_inline(event['created_at'])}\n\n",
                f"Actor: {_inline(event['actor_name'])} ({_inline(event['actor_id'])}); "
                f"activity: {_inline(event['id'])}\n\n",
                _text_block(event["text"]),
            ]
        )
    sections.extend(["## Limitations\n\n", f"{_inline(snapshot['limitations'])}\n"])
    return "".join(sections)
