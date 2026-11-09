"""Read-only run timeline from persisted rollout, activity and execution records."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from conftest import sign_in
from sqlalchemy import select
from test_access_admission import create, incident_body

from cloudops.features.access.models import User
from cloudops.features.incidents.models import Incident, IncidentActivity
from cloudops.features.investigations import timeline as timeline_module
from cloudops.features.investigations.models import InvestigationRun
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.observation_models import WorkloadObservation
from cloudops.features.workloads.observation_schemas import (
    DeploymentObservation,
    Identity,
    Observation,
)
from cloudops.shared.database import sessions

WINDOW_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
WINDOW_END = WINDOW_START + timedelta(minutes=30)
INSIDE = WINDOW_START + timedelta(minutes=5)
OUTSIDE = WINDOW_END + timedelta(minutes=5)


def metric_observation(identity, moment):
    return Observation(
        observation_id=uuid4(),
        identity=identity,
        started_at=moment,
        observed_at=moment,
        deployment=DeploymentObservation(
            generation=1,
            resource_version="rv-1",
            end_resource_version="rv-1",
            observed_generation=1,
            desired=1,
            ready=1,
            available=1,
            updated=1,
            replicas=1,
        ),
        replicaset_resource_version="rv-1",
        pod_resource_version="rv-1",
        current_replicaset_uid="rs-1",
        coverage="complete",
        health="healthy",
        reasons=[],
        pods=[],
        unrelated_pods=0,
        template_fields=[],
        config_references=[],
    )


def seed(ids, monkeypatch=None):
    with sessions()() as db, db.begin():
        run = db.get(InvestigationRun, UUID(ids["run_id"]))
        run.window_start = WINDOW_START
        run.window_end = WINDOW_END
        run.window_source = "explicit"
        incident = db.get(Incident, UUID(ids["incident_id"]))
        workload = db.get(Workload, incident.workload_id)
        operator = db.scalar(select(User).where(User.username == "operator"))
        identity = Identity(
            workload_id=workload.id,
            cluster=workload.cluster,
            namespace=workload.namespace,
            name=workload.name,
            uid=workload.uid,
            epoch=workload.observation_epoch,
        )
        inside_observation = WorkloadObservation(
            workload_id=workload.id,
            epoch=workload.observation_epoch,
            observed_at=INSIDE,
            payload=metric_observation(identity, INSIDE).model_dump(mode="json"),
            comparison={},
        )
        outside_observation = WorkloadObservation(
            workload_id=workload.id,
            epoch=workload.observation_epoch,
            observed_at=OUTSIDE,
            payload=metric_observation(identity, OUTSIDE).model_dump(mode="json"),
            comparison={},
        )
        db.add_all([inside_observation, outside_observation])
        inside_activity = IncidentActivity(
            incident_id=incident.id,
            kind="note",
            text="Operator note inside the window",
            actor_id=operator.id,
            created_at=INSIDE,
        )
        outside_activity = IncidentActivity(
            incident_id=incident.id,
            kind="note",
            text="Operator note outside the window",
            actor_id=operator.id,
            created_at=OUTSIDE,
        )
        db.add_all([inside_activity, outside_activity])
        plan = ActionPlan(
            incident_id=incident.id,
            workload_id=workload.id,
            template="scale_deployment",
            content={"replicas": 1},
            digest="a" * 64,
            target_epoch="epoch",
            state="approved",
            rationale="Timeline fixture",
            created_by=operator.id,
            expires_at=WINDOW_END + timedelta(hours=1),
        )
        db.add(plan)
        db.flush()
        inside_execution = Execution(
            plan_id=plan.id,
            workload_id=workload.id,
            state="succeeded",
            holds_lock=False,
            result={},
            created_at=INSIDE + timedelta(minutes=1),
            finished_at=INSIDE + timedelta(minutes=2),
        )
        outside_execution = Execution(
            plan_id=plan.id,
            workload_id=workload.id,
            state="succeeded",
            holds_lock=False,
            result={},
            created_at=OUTSIDE,
            finished_at=OUTSIDE,
        )
        # One row per logical execution; outside timestamps are covered by a separate plan.
        db.add(inside_execution)
        db.flush()
        outside_plan = ActionPlan(
            incident_id=incident.id,
            workload_id=workload.id,
            template="scale_deployment",
            content={"replicas": 2},
            digest="b" * 64,
            target_epoch="epoch",
            state="approved",
            rationale="Timeline fixture outside",
            created_by=operator.id,
            expires_at=WINDOW_END + timedelta(hours=2),
        )
        db.add(outside_plan)
        db.flush()
        outside_execution.plan_id = outside_plan.id
        db.add(outside_execution)
        return {
            "inside_observation": inside_observation.id,
            "outside_observation": outside_observation.id,
            "inside_activity": inside_activity.id,
            "outside_activity": outside_activity.id,
            "inside_execution": inside_execution.id,
            "outside_execution": outside_execution.id,
        }


def call(client, ids, headers):
    return client.get(
        f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}/timeline",
        headers=headers,
    )


def test_timeline_returns_only_persisted_records_inside_the_window(client):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    seeded = seed(ids)
    response = call(client, ids, headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"] == "RunTimeline/v1"
    assert body["window"]["start"].startswith("2026-01-01T00:00:00")
    assert body["window"]["end"].startswith("2026-01-01T00:30:00")
    assert body["truncated"] is False
    assert "correlation" in body["limitations"]
    kinds = [marker["kind"] for marker in body["markers"]]
    assert kinds[0] == "window_start"
    assert kinds[-1] == "window_end"
    assert "rollout_observation" in kinds
    assert "incident_activity" in kinds
    assert "execution_created" in kinds
    assert "execution_finished" in kinds
    ids_seen = {marker["id"] for marker in body["markers"]}
    assert str(seeded["inside_observation"]) in ids_seen
    assert str(seeded["inside_activity"]) in ids_seen
    assert str(seeded["inside_execution"]) in ids_seen
    assert str(seeded["outside_observation"]) not in ids_seen
    assert str(seeded["outside_activity"]) not in ids_seen
    assert str(seeded["outside_execution"]) not in ids_seen
    timestamps = [marker["at"] for marker in body["markers"]]
    assert timestamps == sorted(timestamps)
    observation = next(
        marker for marker in body["markers"] if marker["kind"] == "rollout_observation"
    )
    assert observation["detail"]["health"] == "healthy"
    assert observation["detail"]["coverage"] == "complete"


def test_timeline_is_scoped_to_the_exact_incident_and_run(client):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    seed(ids)
    wrong = client.get(f"/api/incidents/{uuid4()}/runs/{ids['run_id']}/timeline", headers=headers)
    assert wrong.status_code == 404
    wrong = client.get(
        f"/api/incidents/{ids['incident_id']}/runs/{uuid4()}/timeline", headers=headers
    )
    assert wrong.status_code == 404


def test_viewer_can_read_the_timeline_and_missing_window_is_honest(client):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    seed(ids)
    viewer = sign_in(client, "viewer")
    assert call(client, ids, viewer).status_code == 200
    with sessions()() as db, db.begin():
        run = db.get(InvestigationRun, UUID(ids["run_id"]))
        run.window_start = None
        run.window_end = None
        run.window_source = "not_captured"
    body = call(client, ids, viewer).json()
    assert body["window"] is None
    assert body["markers"] == []
    assert body["truncated"] is False
    assert "not captured" in body["limitations"]


def test_timeline_truncates_at_its_marker_bound(client, monkeypatch):
    headers = sign_in(client)
    ids = create(client, headers, incident_body(client)).json()
    seed(ids)
    monkeypatch.setattr(timeline_module, "MARKER_LIMIT", 2)
    body = call(client, ids, headers).json()
    assert body["truncated"] is True
    assert len(body["markers"]) == 2
    assert body["markers"][0]["kind"] == "window_start"
