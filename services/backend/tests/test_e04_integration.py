"""Integrated E04 evidence journeys over real producer adapters and stored consumers."""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import e04_adapters as e04
import pytest
from conftest import sign_in
from sqlalchemy import func, select
from test_access_admission import create, incident_body, rerun
from test_collection_checkpoints import ProcessLost, context_for
from test_jobs import expire

from cloudops.entrypoints import worker
from cloudops.entrypoints.dispatch import GATE
from cloudops.entrypoints.worker import HANDLERS, run_once
from cloudops.features.access.models import User
from cloudops.features.incidents.models import Incident, IncidentActivity
from cloudops.features.investigations import collector, prometheus
from cloudops.features.investigations.catalog import SOURCES
from cloudops.features.investigations.collector import collect_checkpointed
from cloudops.features.investigations.models import Evidence, EvidencePin, InvestigationRun
from cloudops.features.recovery.models import ActionPlan, Execution
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.observation_models import WorkloadObservation
from cloudops.features.workloads.observation_schemas import Identity
from cloudops.shared.config import settings
from cloudops.shared.database import sessions
from cloudops.shared.jobs import claim_next, finish

WINDOW_START = e04.WINDOW_START
WINDOW_END = e04.WINDOW_END
METRIC_KEYS = tuple(
    f"prometheus.{query_id}"
    for query_id in (
        "request_rate",
        "error_ratio",
        "p95_latency",
        "memory_working_set",
        "memory_limit",
        "restarts",
    )
)


def create_fixed_run(client, headers, title):
    body = incident_body(client)
    body["title"] = title
    ids = create(client, headers, body).json()
    set_window(ids)
    return ids


def set_window(ids):
    with sessions()() as db, db.begin():
        run = db.get(InvestigationRun, UUID(ids["run_id"]))
        run.window_start = WINDOW_START
        run.window_end = WINDOW_END
        run.window_source = "explicit"


def run_view(client, ids):
    response = client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}")
    assert response.status_code == 200, response.text
    return response.json()


def stored(ids):
    with sessions()() as db:
        return {
            row.source_key: (row.sha256, row.coverage, row.payload)
            for row in db.scalars(select(Evidence).where(Evidence.run_id == UUID(ids["run_id"])))
        }


def collect_batch(client, headers, monkeypatch, scenario, title):
    ids = create_fixed_run(client, headers, title)
    binding = e04.binding(monkeypatch, scenario)
    claim = claim_next(HANDLERS, GATE)
    assert claim and claim.subject_id == UUID(ids["run_id"])
    result = collector.collect(context_for(claim), binding)
    assert finish(claim, HANDLERS[claim.kind], result=result, gate=GATE)
    return ids, binding


def evidence_for(view, key):
    return next(row for row in view["evidence"] if row["source_key"] == key)


def test_full_catalog_batch_checkpoint_and_takeover_parity(client, monkeypatch):
    monkeypatch.setenv("KUBE_CONFIGMAP_ENABLED", "1")
    headers = sign_in(client)
    assert len(SOURCES) == 12

    batch, binding = collect_batch(client, headers, monkeypatch, "crashloop", "Batch full catalog")
    reference = stored(batch)
    assert set(reference) == set(SOURCES)

    checkpointed = create_fixed_run(client, headers, "Checkpoint full catalog")
    checkpoint_claim = claim_next(HANDLERS, GATE)
    assert checkpoint_claim.subject_id == UUID(checkpointed["run_id"])
    assert finish(
        checkpoint_claim,
        HANDLERS[checkpoint_claim.kind],
        result=collect_checkpointed(checkpoint_claim, context_for(checkpoint_claim), binding, GATE),
        gate=GATE,
    )
    assert stored(checkpointed) == reference

    takeover = create_fixed_run(client, headers, "Takeover full catalog")
    first = claim_next(HANDLERS, GATE)
    first_context = context_for(first)
    lost_key = "prometheus.error_ratio"

    def lost(_context, _timeout):
        raise ProcessLost()

    with monkeypatch.context() as interrupted:
        interrupted.setitem(collector.REGISTERED_ADAPTERS, lost_key, lost)
        with pytest.raises(ProcessLost):
            collect_checkpointed(first, first_context, {**binding, lost_key: lost}, GATE)
    interrupted_view = run_view(client, takeover)
    interrupted_ids = {row["source_key"]: row["id"] for row in interrupted_view["evidence"]}
    assert interrupted_view["state"] == "collecting"
    assert interrupted_view["progress"]["sources_committed"] == 6
    assert interrupted_view["progress"]["sources_reused"] == 0
    assert interrupted_view["cancel"] == {
        "allowed": False,
        "reason": "collection_started",
    }

    expire(first)
    second = claim_next(HANDLERS, GATE)
    assert second.attempt == 2
    final = collect_checkpointed(second, context_for(second), binding, GATE)
    assert finish(second, HANDLERS[second.kind], result=final, gate=GATE)
    assert stored(takeover) == reference
    view = run_view(client, takeover)
    assert {row["catalog_version"] for row in view["evidence"]} == {"standard_v1"}
    final_ids = {row["source_key"]: row["id"] for row in view["evidence"]}
    assert {key: final_ids[key] for key in interrupted_ids} == interrupted_ids
    assert view["progress"]["sources_reused"] == 6
    assert view["progress"]["finalized"] is True
    assert [attempt["state"] for attempt in view["attempts"]] == ["expired", "succeeded"]
    for key in METRIC_KEYS:
        row = evidence_for(view, key)
        metadata = row["query_metadata"]
        assert metadata["query_id"] == key.removeprefix("prometheus.")
        assert metadata["unit"] == "count"
        assert metadata["step"] == 30
        assert metadata["window"]["start"].startswith("2026-01-01T00:00:00")
        assert metadata["source_identity"]["workload_uid"] == first_context.workload_uid
    request_rate = stored(takeover)["prometheus.request_rate"][2]
    assert request_rate["series"][0]["name"] == "Request rate"


@pytest.mark.parametrize(
    "scenario,pack",
    [
        ("crashloop", "crashloop_startup"),
        ("oom", "oom_restarts"),
        ("healthy", "traffic_latency_errors"),
        ("pending", "pending_unschedulable"),
        ("imagepull", "image_pull"),
    ],
)
def test_five_fixed_pack_coverage_with_k8s_and_metrics(client, monkeypatch, scenario, pack):
    monkeypatch.setenv("KUBE_CONFIGMAP_ENABLED", "1")
    headers = sign_in(client)
    ids, _binding = collect_batch(client, headers, monkeypatch, scenario, f"Pack {pack}")
    view = run_view(client, ids)
    coverage = {row["pack"]: row for row in view["pack_coverage"]}
    assert coverage[pack]["status"] == "available"
    assert len(view["evidence"]) == 12
    state = stored(ids)["kubernetes.state"][2]
    pods = state["pods"]
    if scenario in {"pending", "imagepull"}:
        assert (
            evidence_for(view, "kubernetes.logs.current")["coverage"]["status"] == "not_applicable"
        )
        assert (
            evidence_for(view, "kubernetes.logs.previous")["coverage"]["status"] == "not_applicable"
        )
    elif scenario == "healthy":
        assert evidence_for(view, "kubernetes.logs.current")["coverage"]["status"] == "available"
        assert (
            evidence_for(view, "kubernetes.logs.previous")["coverage"]["status"] == "not_applicable"
        )
    else:
        assert evidence_for(view, "kubernetes.logs.current")["coverage"]["status"] == "available"
        assert evidence_for(view, "kubernetes.logs.previous")["coverage"]["status"] == "available"

    if scenario == "crashloop":
        assert any(
            pod.get("containers", [{}])[0].get("last_state", {}).get("terminated", {}).get("reason")
            == "Error"
            for pod in pods
        )
    elif scenario == "oom":
        assert any(
            pod.get("containers", [{}])[0].get("last_state", {}).get("terminated", {}).get("reason")
            == "OOMKilled"
            for pod in pods
        )
    elif scenario == "pending":
        assert all(pod.get("status") == "Pending" for pod in pods)
    elif scenario == "imagepull":
        assert any(
            pod.get("containers", [{}])[0].get("state", {}).get("waiting", {}).get("reason")
            == "ImagePullBackOff"
            for pod in pods
        )
    else:
        assert all(pod.get("status") == "Running" for pod in pods)


def test_injector_truth_and_secret_values_never_enter_evidence(client, monkeypatch):
    monkeypatch.setenv("KUBE_CONFIGMAP_ENABLED", "1")
    headers = sign_in(client)
    ids, _binding = collect_batch(client, headers, monkeypatch, "injector", "Injector truth canary")
    view = run_view(client, ids)
    assert len(view["evidence"]) == 12
    payloads = stored(ids)
    encoded = json.dumps({key: value[2] for key, value in payloads.items()}, sort_keys=True)
    assert e04.INJECTOR_CANARY not in encoded
    assert "injector-secret" not in encoded
    assert e04.INJECTOR_SECRET not in encoded


def test_collected_revision_diff_and_saved_export_survive_reinvestigation(client, monkeypatch):
    """Real producers on controlled transports; no direct evidence payload seeding."""
    headers = sign_in(client)
    left, _ = collect_batch(client, headers, monkeypatch, "healthy", "Revision one")
    original = stored(left)
    incident = left["incident_id"]
    assert (
        client.post(
            f"/api/incidents/{incident}/lifecycle",
            headers=headers,
            json={"action": "resolve", "reason": "Freeze collected revision one"},
        ).status_code
        == 200
    )
    activity = client.get(f"/api/incidents/{incident}/activity").json()["items"]
    resolution = next(row["id"] for row in activity if row["kind"] == "resolve")
    export = f"/api/incidents/{incident}/handoff?resolution_id={resolution}"
    baseline = client.get(export)
    assert baseline.status_code == 200 and baseline.content
    assert (
        client.post(
            f"/api/incidents/{incident}/lifecycle",
            headers=headers,
            json={"action": "reopen", "reason": "Observe a new revision"},
        ).status_code
        == 200
    )
    right = rerun(client, headers, incident).json()
    set_window(right)
    binding = e04.binding(monkeypatch, "changed")
    monkeypatch.setattr(worker, "configured_adapters", lambda _mode: binding)
    assert run_once()
    assert run_view(client, right)["state"] == "insufficient_evidence"

    def detail(a, b):
        response = client.get(
            f"/api/incidents/{a['incident_id']}/runs/{a['run_id']}/compare",
            params={
                "right_incident_id": b["incident_id"],
                "right_run_id": b["run_id"],
                "source_key": "kubernetes.state",
                "catalog_version": "standard_v1",
                "limit": 100,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    forward = detail(left, right)
    assert forward == detail(left, right)
    changed = [row for row in forward["items"] if row["status"] == "changed"]
    assert any(row["left"] == "1" and row["right"] == "2" for row in changed)
    # Anonymous template objects retain content identities; their changed image
    # is represented as removed/added rather than inventing a historical match.
    assert any(
        row["left"] == "fixture:1" and row["status"] == "removed" for row in forward["items"]
    )
    assert any(row["right"] == "fixture:2" and row["status"] == "added" for row in forward["items"])
    assert any(row["status"] == "unchanged" for row in forward["items"])
    reverse = detail(right, left)
    reversed_rows = {row["key"]: row for row in reverse["items"]}
    for row in changed:
        other = reversed_rows[row["key"]]
        assert other["left"] == row["right"] and other["right"] == row["left"]
        for side, ids in (("left", left), ("right", right)):
            ref = row[f"{side}_citation"]
            response = client.get(
                f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}/evidence/{ref['evidence_id']}/citation",
                params={
                    "locator_kind": ref["locator"]["kind"],
                    "locator_key": ref["locator"]["key"],
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["target"]["value"] == row[side]

    monkeypatch.setenv("COLLECTOR_FAIL", "true")
    settings.cache_clear()
    failed = rerun(client, headers, incident).json()
    for _ in range(3):
        assert run_once()
    assert run_view(client, failed)["state"] == "failed"
    monkeypatch.setenv("COLLECTOR_FAIL", "false")
    settings.cache_clear()
    # New engine/session connections model a reader process restart, not worker takeover.
    from cloudops.shared.database import engine

    engine().dispose()

    from cloudops.features.incidents import handoff

    def forbidden(*args, **kwargs):
        raise AssertionError("Saved export must not compose current incident state.")

    monkeypatch.setattr(handoff, "compose", forbidden)
    assert client.get(export).content == baseline.content
    assert stored(left) == original
    with sessions()() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(EvidencePin)
                .where(EvidencePin.kind == "resolution", EvidencePin.owner_id == UUID(resolution))
            )
            > 0
        )


def test_integrated_compare_citation_timeline_and_read_only(client, monkeypatch):
    monkeypatch.setenv("KUBE_CONFIGMAP_ENABLED", "1")
    headers = sign_in(client)
    left, _left_binding = collect_batch(client, headers, monkeypatch, "healthy", "Left healthy")
    right, _right_binding = collect_batch(client, headers, monkeypatch, "crashloop", "Right crash")
    left_view = run_view(client, left)
    left_evidence = {row["source_key"]: row for row in left_view["evidence"]}
    original = stored(left)
    with sessions()() as db:
        pins_before = list(
            db.execute(select(EvidencePin.evidence_id, EvidencePin.kind, EvidencePin.owner_id))
        )
        latest_before = set(
            db.scalars(
                select(EvidencePin.evidence_id).where(
                    EvidencePin.kind == "latest",
                    EvidencePin.owner_id == UUID(left["incident_id"]),
                )
            )
        )

    compare = client.get(
        f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}/compare",
        params={
            "right_incident_id": right["incident_id"],
            "right_run_id": right["run_id"],
        },
    )
    assert compare.status_code == 200, compare.text
    body = compare.json()
    assert body["identity_relation"] == "same_identity"
    assert body["status"] == "comparable"
    assert {source["source_key"] for source in body["sources"]} == set(SOURCES)

    def targets(source_key):
        evidence = left_evidence[source_key]
        return client.get(
            f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}"
            f"/evidence/{evidence['id']}/citation-targets",
            params={"limit": 100},
        )

    state_targets = targets("kubernetes.state")
    assert state_targets.status_code == 200, state_targets.text
    state_target = next(
        item for item in state_targets.json()["items"] if item["label"] == "deployment/replicas"
    )
    resolved = client.get(
        f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}"
        f"/evidence/{left_evidence['kubernetes.state']['id']}/citation",
        params={
            "locator_kind": state_target["locator"]["kind"],
            "locator_key": state_target["locator"]["key"],
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["target"]["value"] == 1

    for source_key, kind in (
        ("kubernetes.events", "event"),
        ("kubernetes.logs.current", "log_segment"),
    ):
        response = targets(source_key)
        assert response.status_code == 200, response.text
        target = next(item for item in response.json()["items"] if item["locator"]["kind"] == kind)
        resolved = client.get(
            f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}"
            f"/evidence/{left_evidence[source_key]['id']}/citation",
            params={
                "locator_kind": target["locator"]["kind"],
                "locator_key": target["locator"]["key"],
            },
        )
        assert resolved.status_code == 200, resolved.text

    series_targets = targets("prometheus.request_rate")
    assert series_targets.status_code == 200, series_targets.text
    sample_target = next(
        item for item in series_targets.json()["items"] if item["locator"]["kind"] == "sample"
    )
    resolved = client.get(
        f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}"
        f"/evidence/{left_evidence['prometheus.request_rate']['id']}/citation",
        params={
            "locator_kind": sample_target["locator"]["kind"],
            "locator_key": sample_target["locator"]["key"],
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert (
        resolved.json()["evidence"]["sha256"] == left_evidence["prometheus.request_rate"]["sha256"]
    )

    # A useful Run, then a failed successor, must not replace its latest-useful pin.
    monkeypatch.setenv("COLLECTOR_FAIL", "true")
    settings.cache_clear()
    failed_binding = e04.binding(monkeypatch, "healthy")
    monkeypatch.setattr(worker, "configured_adapters", lambda _mode: failed_binding)
    failed = rerun(client, headers, left["incident_id"]).json()
    set_window(failed)
    for _ in range(3):
        assert run_once()
    failed_view = run_view(client, failed)
    assert failed_view["state"] == "failed"
    with sessions()() as db:
        pinned = set(
            db.scalars(
                select(EvidencePin.evidence_id).where(
                    EvidencePin.kind == "latest",
                    EvidencePin.owner_id == UUID(left["incident_id"]),
                )
            )
        )
    assert pinned == latest_before
    monkeypatch.setenv("COLLECTOR_FAIL", "false")
    settings.cache_clear()

    # Provider outage: the terminal failed successor must not hide the stored useful Run.
    outage_view = run_view(client, left)
    assert outage_view["state"] == "insufficient_evidence"
    for row in outage_view["evidence"]:
        if row["content_status"] != "available":
            continue
        content = client.get(
            f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}"
            f"/evidence/{row['id']}/content"
        )
        assert content.status_code == 200, content.text
        assert content.json()["sha256"] == row["sha256"]
    assert (
        client.get(
            f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}/timeline"
        ).status_code
        == 200
    )
    outage_compare = client.get(
        f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}/compare",
        params={
            "right_incident_id": right["incident_id"],
            "right_run_id": right["run_id"],
        },
    )
    assert outage_compare.status_code == 200
    assert len(outage_compare.json()["sources"]) == 12

    with sessions()() as db, db.begin():
        incident = db.get(Incident, UUID(left["incident_id"]))
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
        inside = WINDOW_START + timedelta(minutes=5)
        db.add(
            WorkloadObservation(
                workload_id=workload.id,
                epoch=workload.observation_epoch,
                observed_at=inside,
                payload=e04.observation(identity, inside).model_dump(mode="json"),
                comparison={},
            )
        )
        db.add(
            IncidentActivity(
                incident_id=incident.id,
                kind="note",
                text="Integrated timeline fixture",
                actor_id=operator.id,
                created_at=inside,
            )
        )
        plan = ActionPlan(
            incident_id=incident.id,
            workload_id=workload.id,
            template="scale_deployment",
            content={"replicas": 1},
            digest="a" * 64,
            target_epoch="epoch",
            state="approved",
            rationale="Integrated timeline fixture",
            created_by=operator.id,
            expires_at=WINDOW_END + timedelta(hours=1),
        )
        db.add(plan)
        db.flush()
        db.add(
            Execution(
                plan_id=plan.id,
                workload_id=workload.id,
                state="succeeded",
                holds_lock=False,
                result={},
                created_at=inside + timedelta(minutes=1),
                finished_at=inside + timedelta(minutes=2),
            )
        )

    timeline = client.get(f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}/timeline")
    assert timeline.status_code == 200, timeline.text
    body = timeline.json()
    kinds = [marker["kind"] for marker in body["markers"]]
    assert kinds[0] == "window_start" and kinds[-1] == "window_end"
    assert "rollout_observation" in kinds
    assert "incident_activity" in kinds
    assert "execution_created" in kinds and "execution_finished" in kinds
    assert body["truncated"] is False

    # Current preview/export parity; frozen saved exports have a separate regression above.
    preview = client.get(f"/api/incidents/{left['incident_id']}/handoff/preview")
    assert preview.status_code == 200, preview.text
    exported = client.get(f"/api/incidents/{left['incident_id']}/handoff")
    assert exported.status_code == 200, exported.text
    assert exported.content == preview.json()["markdown"].encode()

    # Read-only consumers must not call providers or change pins/evidence.
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Read consumers must not call a provider.")

    monkeypatch.setattr(collector, "configured_adapters", forbidden)
    monkeypatch.setattr(prometheus, "adapters", forbidden)
    assert (
        client.get(
            f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}/compare",
            params={
                "right_incident_id": right["incident_id"],
                "right_run_id": right["run_id"],
            },
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}/timeline"
        ).status_code
        == 200
    )
    assert stored(left) == original
    with sessions()() as db:
        pins_after = list(
            db.execute(select(EvidencePin.evidence_id, EvidencePin.kind, EvidencePin.owner_id))
        )
    assert pins_after == pins_before

    client.cookies.clear()
    assert (
        client.get(
            f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}/timeline"
        ).status_code
        == 401
    )
    viewer = sign_in(client, "viewer")
    assert (
        client.get(
            f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}/timeline",
            headers=viewer,
        ).status_code
        == 200
    )

    with sessions()() as db, db.begin():
        row = db.get(Evidence, UUID(left_evidence["kubernetes.state"]["id"]))
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        for pin in db.scalars(select(EvidencePin).where(EvidencePin.evidence_id == row.id)):
            db.delete(pin)
    assert (
        client.get(
            f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}"
            f"/evidence/{left_evidence['kubernetes.state']['id']}/content"
        ).status_code
        == 410
    )
    assert (
        client.get(
            f"/api/incidents/{left['incident_id']}/runs/{left['run_id']}"
            f"/evidence/{left_evidence['kubernetes.state']['id']}/citation-targets"
        ).status_code
        == 410
    )


def test_reads_do_not_create_jobs_or_evidence(client, monkeypatch):
    monkeypatch.setenv("KUBE_CONFIGMAP_ENABLED", "1")
    headers = sign_in(client)
    ids, _binding = collect_batch(client, headers, monkeypatch, "healthy", "Read-only counts")
    with sessions()() as db:
        before = (
            db.scalar(select(func.count()).select_from(Evidence)),
            db.scalar(select(func.count()).select_from(EvidencePin)),
            db.scalar(select(func.count()).select_from(InvestigationRun)),
        )
    assert (
        client.get(f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}/timeline").status_code
        == 200
    )
    assert (
        client.get(
            f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}/compare",
            params={"right_incident_id": ids["incident_id"], "right_run_id": ids["run_id"]},
        ).status_code
        == 200
    )
    with sessions()() as db:
        after = (
            db.scalar(select(func.count()).select_from(Evidence)),
            db.scalar(select(func.count()).select_from(EvidencePin)),
            db.scalar(select(func.count()).select_from(InvestigationRun)),
        )
    assert after == before
