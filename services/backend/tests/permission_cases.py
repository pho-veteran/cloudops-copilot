"""Valid requests and outcome assertions owned by the available feature operations."""

from dataclasses import dataclass
from typing import Callable
from uuid import UUID, uuid4

from cloudops.features.access.schemas import SessionView
from cloudops.features.health.schemas import HealthView
from cloudops.features.incidents.schemas import AdmissionView, IncidentDetail, IncidentPage
from cloudops.features.investigations.schemas import RunPage, RunView
from cloudops.features.workloads.schemas import (
    ObservationRefreshView,
    WorkloadObservationPage,
    WorkloadObservationView,
    WorkloadPage,
)


@dataclass(frozen=True)
class RequestCase:
    request: Callable
    check: Callable


def validated(model, response):
    assert response.status_code == 200, response.text
    value = model.model_validate(response.json())
    assert value.model_dump(mode="json") == response.json(), "Unexpected transport fields"
    return value


def session_request(context):
    return {}


def session_check(response, context, actor):
    assert response.status_code == 200, response.text
    result = validated(SessionView, response)
    expected = context.actors[actor].session
    assert result.model_dump(mode="json") == expected
    assert result.principal.role == actor


def workload_check(response, context, actor):
    assert response.status_code == 200, response.text
    page = validated(WorkloadPage, response)
    assert str(context.workload_id) in {str(item.id) for item in page.items}
    assert page.next_offset is None


def incident_request(context):
    return {"path_params": {"incident_id": context.incident_id}}


def incident_list_check(response, context, actor):
    assert response.status_code == 200, response.text
    page = validated(IncidentPage, response)
    record = next(item for item in page.items if str(item.id) == context.incident_id)
    assert record.description == context.body["description"]
    assert str(record.created_by) == context.actors["operator"].session["principal"]["id"]
    assert page.next_offset is None


def incident_check(response, context, actor):
    assert response.status_code == 200, response.text
    record = validated(IncidentDetail, response)
    assert str(record.id) == context.incident_id
    assert record.description == context.body["description"]
    assert str(record.workload.id) == context.workload_id


def run_request(context):
    return {"path_params": {"incident_id": context.incident_id, "run_id": context.run_id}}


def assert_evidence(run, context):
    assert str(run.id) == context.run_id
    assert str(run.incident_id) == context.incident_id
    assert run.state == "insufficient_evidence"
    assert len(run.evidence) == len(run.attempts) == 1
    assert run.evidence[0].source == "development_fixture"
    assert run.evidence[0].payload.label.startswith("Synthetic development evidence;")
    assert run.attempts[0].state == "succeeded"
    assert run.diagnosis == run.recovery == "unavailable"


def run_list_check(response, context, actor):
    assert response.status_code == 200, response.text
    page = validated(RunPage, response)
    assert page.next_offset is None
    assert_evidence(next(item for item in page.items if str(item.id) == context.run_id), context)


def run_check(response, context, actor):
    assert response.status_code == 200, response.text
    assert_evidence(validated(RunView, response), context)


def health_check(response, context, actor):
    assert response.status_code == 200, response.text
    view = validated(HealthView, response)
    observations = {item.component: item for item in view.observations}
    assert set(observations) == {"database", "worker", "collector", "diagnosis", "executor"}
    assert observations["database"].status == "available"
    assert observations["collector"].source == "development_fixture"
    assert observations["diagnosis"].status == observations["executor"].status == "unavailable"


def create_incident_request(context):
    return {"json": context.body, "headers": {"Idempotency-Key": str(uuid4())}}


def create_run_request(context):
    return {**incident_request(context), "headers": {"Idempotency-Key": str(uuid4())}}


def admission_check(response, context, actor):
    assert response.status_code == 201, response.text
    result = AdmissionView.model_validate(response.json())
    assert result.model_dump(mode="json") == response.json()
    assert isinstance(result.incident_id, UUID) and isinstance(result.run_id, UUID)
    assert str(result.run_id) != context.run_id


def logout_check(response, context, actor):
    assert response.status_code == 204, response.text
    assert response.content == b""


def login_request(context):
    return {"json": {"username": "viewer", "password": "local-test-password"}}


def login_check(response, context, actor):
    assert response.status_code == 200, response.text
    result = validated(SessionView, response)
    assert result.principal.username == result.principal.role == "viewer"
    assert result.csrf_token


def probe_check(response, context, actor):
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "available"}


def observation_request(context):
    return {"path_params": {"workload_id": context.consumer.workload_id}}


def observation_check(response, context, actor):
    assert response.status_code == 200, response.text
    view = validated(WorkloadObservationView, response)
    assert str(view.workload_id) == context.consumer.workload_id
    assert view.availability in {"available", "unavailable"}
    assert view.source == "live_kubernetes"


def observation_history_check(response, context, actor):
    assert response.status_code == 200, response.text
    page = validated(WorkloadObservationPage, response)
    assert page.next_offset is None


def refresh_check(response, context, actor):
    assert response.status_code == 202, response.text
    view = ObservationRefreshView.model_validate(response.json())
    assert view.model_dump(mode="json") == response.json()
    assert view.status == "pending"
    assert str(view.workload_id) == context.consumer.workload_id


CASES = {
    "get_session": RequestCase(session_request, session_check),
    "list_workloads": RequestCase(session_request, workload_check),
    "list_incidents": RequestCase(session_request, incident_list_check),
    "get_incident": RequestCase(incident_request, incident_check),
    "list_runs": RequestCase(incident_request, run_list_check),
    "get_run": RequestCase(run_request, run_check),
    "get_health": RequestCase(session_request, health_check),
    "create_incident": RequestCase(create_incident_request, admission_check),
    "create_run": RequestCase(create_run_request, admission_check),
    "logout": RequestCase(session_request, logout_check),
    "login": RequestCase(login_request, login_check),
    "liveness": RequestCase(session_request, probe_check),
    "readiness": RequestCase(session_request, probe_check),
    "get_workload_observation": RequestCase(observation_request, observation_check),
    "list_workload_observations": RequestCase(observation_request, observation_history_check),
    "request_workload_observation_refresh": RequestCase(observation_request, refresh_check),
}

from consumer_cases import CASES as CONSUMER_CASES  # noqa: E402

CASES.update(CONSUMER_CASES)
