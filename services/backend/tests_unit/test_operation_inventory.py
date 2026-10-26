"""Guard self-tests do not require a database or authentication overrides."""

from dataclasses import replace

import pytest
from fastapi import APIRouter, FastAPI
from permission_cases import CASES, RequestCase
from permission_inventory import OPERATIONS, Operation, verify_inventory
from starlette.routing import Mount, Route, WebSocketRoute

from cloudops.entrypoints.api import create_app


def test_current_inventory_without_runtime_configuration(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    verify_inventory(create_app(), OPERATIONS, CASES)


@pytest.mark.parametrize("include_in_schema", [True, False])
def test_new_route_requires_explicit_classification(include_in_schema):
    app = create_app()
    app.add_api_route(
        "/api/new-operation",
        lambda: {},
        operation_id="new_operation",
        include_in_schema=include_in_schema,
    )
    with pytest.raises(AssertionError, match="Unclassified operations"):
        verify_inventory(app, OPERATIONS, CASES)


def test_classification_alone_does_not_supply_request_evidence():
    app = create_app()
    app.add_api_route("/api/new-operation", lambda: {}, operation_id="new_operation")
    operations = {
        **OPERATIONS,
        "new_operation": Operation("GET", "/api/new-operation", "protected_read"),
    }
    with pytest.raises(AssertionError, match="Request fixture inventory"):
        verify_inventory(app, operations, CASES)


@pytest.mark.parametrize("field", ["request", "check"])
def test_case_requires_both_executable_request_and_assertions(field):
    cases = {**CASES, "get_session": replace(CASES["get_session"], **{field: None})}
    with pytest.raises(AssertionError, match="Missing request fixture"):
        verify_inventory(create_app(), OPERATIONS, cases)


def test_a_deliberately_classified_route_requires_an_executable_case():
    app = create_app()

    def fixture_response():
        return {"status": "available"}

    def request(context):
        return {}

    def check(response, context, actor):
        assert response.status_code == 200
        assert response.json() == {"status": "available"}

    app.add_api_route("/api/new-probe", fixture_response, operation_id="new_probe")
    operations = {**OPERATIONS, "new_probe": Operation("GET", "/api/new-probe", "public_probe")}
    cases = {**CASES, "new_probe": RequestCase(request, check)}
    verify_inventory(app, operations, cases)
    from fastapi.testclient import TestClient

    # No context manager: this unit app's route needs no runtime/startup dependencies.
    response = TestClient(app).get("/api/new-probe", **cases["new_probe"].request(None))
    cases["new_probe"].check(response, None, "anonymous")


@pytest.mark.parametrize("identity", [None, "get_session"])
def test_missing_or_duplicate_explicit_id_is_rejected(identity):
    app = create_app()
    app.add_api_route("/api/new-operation", lambda: {}, operation_id=identity)
    with pytest.raises(AssertionError, match="Explicit operation ID|Duplicate operation ID"):
        verify_inventory(app, OPERATIONS, CASES)


def test_stale_inventory_after_route_removal_is_rejected():
    app = create_app()
    app.add_api_route("/api/removed", lambda: {}, operation_id="removed")
    app.router.routes[:] = [
        route for route in app.routes if getattr(route, "operation_id", None) != "removed"
    ]
    operations = {**OPERATIONS, "removed": Operation("GET", "/api/removed", "protected_read")}
    with pytest.raises(AssertionError, match="stale inventory"):
        verify_inventory(app, operations, CASES)


@pytest.mark.parametrize("change", ["category", "method", "path"])
def test_changed_classification_or_route_requires_review(change):
    value = {"category": "unknown", "method": "POST", "path": "/api/other-session"}[change]
    operations = {
        **OPERATIONS,
        "get_session": replace(OPERATIONS["get_session"], **{change: value}),
    }
    with pytest.raises(AssertionError, match="Unknown classification|Changed route"):
        verify_inventory(create_app(), operations, CASES)


@pytest.mark.parametrize(
    "route",
    [
        Mount("/extra", app=FastAPI()),
        WebSocketRoute("/ws", lambda websocket: None),
        Route("/docs-unclassified", lambda request: None),
        Route("/docs", lambda request: None, name="swagger_ui_html"),
    ],
)
def test_unknown_non_api_routes_cannot_inherit_a_public_exemption(route):
    app = create_app()
    app.router.routes.append(route)
    with pytest.raises(
        AssertionError,
        match="Unsupported route type|Unclassified framework route|Duplicate method/path",
    ):
        verify_inventory(app, OPERATIONS, CASES)


def test_multi_method_operation_cannot_hide_a_second_permission_requirement():
    app = create_app()
    app.add_api_route("/api/mixed", lambda: {}, methods=["GET", "POST"], operation_id="mixed")
    with pytest.raises(AssertionError, match="Classify each method separately"):
        verify_inventory(app, OPERATIONS, CASES)


def test_hidden_existing_operation_still_needs_permission_evidence():
    app = create_app()
    child = APIRouter()
    child.add_api_route("/hidden", lambda: {}, operation_id="hidden")
    parent = APIRouter()
    parent.include_router(child, prefix="/child")
    app.include_router(parent, prefix="/parent", include_in_schema=False)
    with pytest.raises(AssertionError, match="Unclassified operations"):
        verify_inventory(app, OPERATIONS, CASES)
    operations = {
        **OPERATIONS,
        "hidden": Operation("GET", "/parent/child/hidden", "protected_read"),
    }
    cases = {**CASES, "hidden": CASES["get_session"]}
    verify_inventory(app, operations, cases)


def test_duplicate_method_path_cannot_shadow_another_operation():
    app = create_app()
    app.add_api_route("/api/session", lambda: {}, operation_id="shadow_session")
    with pytest.raises(AssertionError, match="Duplicate method/path"):
        verify_inventory(app, OPERATIONS, CASES)


def test_stale_request_fixture_is_rejected():
    with pytest.raises(AssertionError, match="Request fixture inventory"):
        verify_inventory(create_app(), OPERATIONS, {**CASES, "removed": CASES["get_session"]})


def test_schema_disagreement_is_rejected(monkeypatch):
    app = create_app()
    document = app.openapi()
    document["paths"]["/api/session"]["get"]["operationId"] = "schema_only_session"
    monkeypatch.setattr(app, "openapi", lambda: document)
    with pytest.raises(AssertionError, match="OpenAPI differs"):
        verify_inventory(app, OPERATIONS, CASES)


def test_nested_router_prefix_is_verified_against_live_openapi():
    app = create_app()
    child = APIRouter()
    child.add_api_route("/probe", lambda: {"status": "available"}, operation_id="nested_probe")
    parent = APIRouter()
    parent.include_router(child, prefix="/child")
    app.include_router(parent, prefix="/parent")
    operations = {
        **OPERATIONS,
        "nested_probe": Operation("GET", "/parent/child/probe", "public_probe"),
    }
    cases = {**CASES, "nested_probe": CASES["liveness"]}
    verify_inventory(app, operations, cases)
