"""Test-only operation inventory; feature fixtures supply behavioral evidence."""

from dataclasses import dataclass

from fastapi.routing import APIRoute, iter_route_contexts
from starlette.routing import Route


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    category: str


OPERATIONS = {
    "login": Operation("POST", "/api/login", "account_entry"),
    "get_session": Operation("GET", "/api/session", "protected_read"),
    "logout": Operation("POST", "/api/logout", "session_self_service"),
    "list_workloads": Operation("GET", "/api/workloads", "protected_read"),
    "list_workload_scope": Operation("GET", "/api/workloads/scope", "protected_read"),
    "get_workload": Operation("GET", "/api/workloads/{workload_id}", "protected_read"),
    "get_workload_observation": Operation(
        "GET", "/api/workloads/{workload_id}/observation", "protected_read"
    ),
    "list_workload_observations": Operation(
        "GET", "/api/workloads/{workload_id}/observations", "protected_read"
    ),
    "list_incidents": Operation("GET", "/api/incidents", "protected_read"),
    "get_incident": Operation("GET", "/api/incidents/{incident_id}", "protected_read"),
    "list_runs": Operation("GET", "/api/incidents/{incident_id}/runs", "protected_read"),
    "get_run": Operation("GET", "/api/incidents/{incident_id}/runs/{run_id}", "protected_read"),
    "get_health": Operation("GET", "/api/health", "protected_read"),
    "create_incident": Operation("POST", "/api/incidents", "operator_mutation"),
    "create_run": Operation("POST", "/api/incidents/{incident_id}/runs", "operator_mutation"),
    "cancel_run": Operation(
        "POST", "/api/incidents/{incident_id}/runs/{run_id}/cancel", "operator_mutation"
    ),
    "liveness": Operation("GET", "/api/health/live", "public_probe"),
    "readiness": Operation("GET", "/api/health/ready", "public_probe"),
    "get_baseline_candidate": Operation(
        "GET", "/api/workloads/{workload_id}/baseline-candidate", "protected_observation"
    ),
    "confirm_baseline": Operation(
        "POST", "/api/workloads/{workload_id}/baseline", "operator_mutation"
    ),
    "list_baselines": Operation("GET", "/api/workloads/{workload_id}/baselines", "protected_read"),
    "get_recovery_context": Operation(
        "GET", "/api/workloads/{workload_id}/recovery-context", "protected_observation"
    ),
    "add_incident_note": Operation(
        "POST", "/api/incidents/{incident_id}/notes", "operator_mutation"
    ),
    "transition_incident": Operation(
        "POST", "/api/incidents/{incident_id}/lifecycle", "operator_mutation"
    ),
    "list_incident_activity": Operation(
        "GET", "/api/incidents/{incident_id}/activity", "protected_read"
    ),
    "export_incident_handoff": Operation(
        "GET", "/api/incidents/{incident_id}/handoff", "protected_read"
    ),
    "create_connection_check": Operation("POST", "/api/connection-checks", "operator_mutation"),
    "list_connection_checks": Operation("GET", "/api/connection-checks", "protected_read"),
    "receive_alertmanager_notification": Operation(
        "POST", "/api/intake/alertmanager", "machine_intake"
    ),
    "list_alert_receipts": Operation("GET", "/api/alert-receipts", "protected_read"),
    "create_recommendation_report": Operation(
        "POST", "/api/recommendation-reports", "operator_mutation"
    ),
    "list_recommendation_reports": Operation(
        "GET", "/api/recommendation-reports", "protected_read"
    ),
    "get_recommendation_report": Operation(
        "GET", "/api/recommendation-reports/{report_id}", "protected_read"
    ),
    "export_recommendation_report": Operation(
        "GET", "/api/recommendation-reports/{report_id}/export", "protected_read"
    ),
    "propose_plan": Operation("POST", "/api/incidents/{incident_id}/plans", "operator_mutation"),
    "list_plans": Operation("GET", "/api/incidents/{incident_id}/plans", "protected_read"),
    "read_plan": Operation("GET", "/api/plans/{plan_id}", "protected_review"),
    "decide_plan": Operation("POST", "/api/plans/{plan_id}/decision", "operator_mutation"),
    "list_executions": Operation(
        "GET", "/api/incidents/{incident_id}/executions", "protected_read"
    ),
}

CATEGORIES = {
    "protected_observation",
    "protected_review",
    "protected_read",
    "operator_mutation",
    "session_self_service",
    "account_entry",
    "public_probe",
    # Sender-credential only: no browser session or role can use it.
    "machine_intake",
}

# FastAPI's generated UI/schema routes have no application operation ID.
# An unknown Starlette route, mount or WebSocket must never inherit public access.
FRAMEWORK_ROUTES = {
    "/openapi.json": ("openapi", frozenset({"GET", "HEAD"})),
    "/docs": ("swagger_ui_html", frozenset({"GET", "HEAD"})),
    "/docs/oauth2-redirect": ("swagger_ui_redirect", frozenset({"GET", "HEAD"})),
    "/redoc": ("redoc_html", frozenset({"GET", "HEAD"})),
}


def verify_inventory(app, operations, cases):
    """Fail closed on unknown routes, classifications and missing request assertions."""
    actual = {}
    schema_operations = {}
    framework = {}
    paths = set()
    # Preserve include prefixes and hidden operations using the pinned FastAPI
    # route-context API also used by its OpenAPI exporter.
    for route in iter_route_contexts(app.routes):
        for method in route.methods or ():
            key = (method, route.path)
            assert key not in paths, f"Duplicate method/path: {key}"
            paths.add(key)
        if isinstance(route.original_route, APIRoute):
            identity = route.operation_id
            assert identity, f"Explicit operation ID required: {route.path}"
            assert identity not in actual, f"Duplicate operation ID: {identity}"
            assert len(route.methods) == 1, f"Classify each method separately: {identity}"
            method = next(iter(route.methods))
            actual[identity] = (method, route.path)
            if route.include_in_schema:
                schema_operations[identity] = (method, route.path)
        else:
            assert type(route.original_route) is Route, (
                f"Unsupported route type: {type(route.original_route).__name__}"
            )
            expected = FRAMEWORK_ROUTES.get(route.path)
            assert expected is not None, f"Unclassified framework route: {route.path}"
            assert route.path not in framework, f"Duplicate framework route: {route.path}"
            definition = (route.name, frozenset(route.methods))
            assert definition == expected, f"Changed framework route: {route.path}"
            framework[route.path] = definition

    assert framework == FRAMEWORK_ROUTES, "Framework route inventory changed"
    assert actual.keys() == operations.keys(), (
        f"Unclassified operations: {actual.keys() - operations.keys()}; "
        f"stale inventory: {operations.keys() - actual.keys()}"
    )
    assert cases.keys() == operations.keys(), "Request fixture inventory differs from operations"
    for identity, operation in operations.items():
        assert operation.category in CATEGORIES, f"Unknown classification: {identity}"
        assert actual[identity] == (operation.method, operation.path), f"Changed route: {identity}"
        case = cases[identity]
        assert callable(case.request) and callable(case.check), (
            f"Missing request fixture: {identity}"
        )

    # Inspect live OpenAPI, not a potentially stale committed schema or cached document.
    app.openapi_schema = None
    exposed = {}
    for path, item in app.openapi()["paths"].items():
        for method, operation in item.items():
            if method.upper() not in {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
                "OPTIONS",
                "TRACE",
            }:
                continue
            identity = operation["operationId"]
            assert identity not in exposed, f"Duplicate schema operation ID: {identity}"
            exposed[identity] = (method.upper(), path)
    assert exposed == schema_operations, "OpenAPI differs from live route inventory"
