"""Schema-backed diagnosis cases for the complete permission inventory."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

from diagnosis_fixtures import profile
from sqlalchemy import select
from test_diagnosis_validation import policy

from cloudops.features.diagnosis.api_schemas import AnalysisView, CheckPage, CheckView, ModelStatus
from cloudops.features.diagnosis.lifecycle import ANALYSIS_JOB, admit
from cloudops.features.diagnosis.settings import Deployment
from cloudops.features.incidents.models import Incident
from cloudops.features.investigations.models import InvestigationRun
from cloudops.shared.database import database_now, sessions
from cloudops.shared.jobs import Job
from permission_cases import RequestCase, validated


def seed(context, tmp_path, monkeypatch):
    model, egress = profile(mode="operational", credential=None), policy()
    for name, env, payload in (
        ("model.json", "DIAGNOSIS_PROFILE_FILE", model.model_dump(mode="json", by_alias=True)),
        ("egress.json", "DIAGNOSIS_EGRESS_FILE", egress.model_dump(mode="json", by_alias=True)),
        (
            "qualification.json",
            "DIAGNOSIS_QUALIFICATION_FILE",
            {
                "schema_version": "DiagnosisQualification/v1",
                "cohort_id": "permission-fixture",
                "evidence_reference": "fixture-no-live-quality",
                "identity": Deployment(model, egress, None).identity(),
            },
        ),
    ):
        path = tmp_path / name
        path.write_text(json.dumps(payload))
        monkeypatch.setenv(env, str(path))
    with sessions()() as db, db.begin():
        original = db.get(InvestigationRun, UUID(context.run_id))
        incident = Incident(
            workload_id=UUID(context.workload_id),
            title="Analysis permission fixture",
            description="Isolated held Run",
            created_by=original.requested_by,
        )
        db.add(incident)
        db.flush()
        run = InvestigationRun(
            incident_id=incident.id,
            requested_by=original.requested_by,
            question=original.question,
            question_source=original.question_source,
            window_start=original.window_start,
            window_end=original.window_end,
            window_source=original.window_source,
            pack_coverage=original.pack_coverage,
        )
        db.add(run)
        db.flush()
        analysis = admit(db, run)
        analysis.state, analysis.wait_mode = "waiting_for_quota", "resume_required"
        analysis.earliest_retry = database_now(db) - timedelta(seconds=1)
        run.state, run.provider_call = "waiting_for_quota", "none"
        db.flush()
        db.scalar(
            select(Job).where(Job.kind == ANALYSIS_JOB, Job.subject_id == analysis.id)
        ).state = "held"
        context.diagnosis_incident_id, context.diagnosis_run_id = str(incident.id), str(run.id)


def run_path(context):
    return {
        "path_params": {
            "incident_id": context.diagnosis_incident_id,
            "run_id": context.diagnosis_run_id,
        }
    }


def empty_mutation(context):
    return {"json": {}, "headers": {"Idempotency-Key": str(uuid4())}}


def resume_check(response, context, actor):
    result = validated(AnalysisView, response)
    assert result.status == "pending" and result.calls == 0


def probe_check(response, context, actor):
    assert response.status_code == 201, response.text
    assert CheckView.model_validate(response.json()).state == "queued"


CASES = {
    "get_analysis": RequestCase(run_path, lambda r, c, a: validated(AnalysisView, r)),
    "resume_analysis": RequestCase(lambda c: {**run_path(c), **empty_mutation(c)}, resume_check),
    "get_model_status": RequestCase(lambda c: {}, lambda r, c, a: validated(ModelStatus, r)),
    "list_model_checks": RequestCase(lambda c: {}, lambda r, c, a: validated(CheckPage, r)),
    "create_model_check": RequestCase(empty_mutation, probe_check),
}
