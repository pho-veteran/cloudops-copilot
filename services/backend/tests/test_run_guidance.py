from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4

import pytest
from conftest import sign_in
from pydantic import ValidationError
from alembic import command
from alembic.config import Config
from pathlib import Path
from sqlalchemy import func, select

from cloudops.features.access.models import User
from cloudops.features.incidents.models import Incident, IncidentActivity
from cloudops.features.investigations.models import InvestigationRun, Evidence
from cloudops.features.investigations.retention import purge_expired
from cloudops.features.knowledge import guidance
from cloudops.features.knowledge.guidance import (
    pin_guidance,
    read_guidance,
    resolve_reference,
    resolve_selection_chunk,
    retrieve,
)
from cloudops.features.knowledge.guidance_schemas import KnowledgeRef
from cloudops.features.knowledge.indexing import index_corpus
from cloudops.features.knowledge.models import (
    KnowledgeSelection,
    KnowledgeSelectionChunk,
    KnowledgeDocumentVersion,
)
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import sessions
from cloudops.shared.errors import DomainError
from knowledge_fixtures import document, write_corpus


@pytest.fixture
def context(db, seeded):
    actor = db.scalar(select(User).where(User.username == "operator"))
    workload = db.scalar(select(Workload))
    incident = Incident(
        workload_id=workload.id,
        title="Memory pressure",
        description="Observed restarts",
        created_by=actor.id,
    )
    db.add(incident)
    db.flush()
    now = datetime.now(timezone.utc)
    run = InvestigationRun(
        incident_id=incident.id,
        state="insufficient_evidence",
        reason="Analysis unavailable",
        requested_by=actor.id,
        question="memory",
        question_source="operator",
        window_start=now - timedelta(minutes=30),
        window_end=now,
        window_source="explicit",
        pack_coverage=[
            {
                "pack": "oom_restarts",
                "status": "available",
                "missing_required": [],
                "optional_gaps": [],
            }
        ],
    )
    db.add(run)
    db.commit()
    return incident.id, run.id, actor.id


def pin(context, **kwargs):
    incident, run, actor = context
    with sessions()() as db, db.begin():
        return pin_guidance(
            db,
            incident,
            run,
            consumer="manual",
            request_identity="manual",
            actor_id=actor,
            **kwargs,
        )


def corpus(root, **kwargs):
    write_corpus(
        root,
        [document(title="Memory guide", **kwargs)],
        {"fixture-guide": "# Memory guide\n\nMemory pressure and restarts.\n"},
    )
    result = index_corpus(root)
    assert result.outcome in {"indexed", "unchanged", "reactivated"}, result.reason
    return result


def test_index_unavailable_can_retry_and_pinning_preserves_domain_state(db, context, tmp_path):
    incident_id, run_id, _ = context
    saved = IncidentActivity(
        incident_id=incident_id,
        actor_id=context[2],
        kind="resolve",
        text="Historical resolution",
        snapshot={"markdown": "Original evidence: Giữ nguyên.\r\n"},
    )
    db.add(saved)
    db.commit()
    original_markdown = saved.snapshot["markdown"].encode("utf-8")
    before = (db.get(Incident, incident_id).state, db.get(InvestigationRun, run_id).state)
    assert pin(context).reason == "not_indexed"
    assert db.scalar(select(func.count()).select_from(KnowledgeSelection)) == 0
    corpus(tmp_path)
    selected = pin(context).selection
    assert selected.retrieval.status == "matched"
    assert selected.retrieval.scope.symptom_family is None
    assert selected.retrieval.scope.workload_uid == db.scalar(
        select(Workload.uid)
        .join(Incident, Incident.workload_id == Workload.id)
        .where(Incident.id == incident_id)
    )
    assert any("unverified" in text for text in selected.retrieval.limitations)
    assert pin(context).selection.id == selected.id
    db.expire_all()
    assert (db.get(Incident, incident_id).state, db.get(InvestigationRun, run_id).state) == before
    assert (
        db.get(IncidentActivity, saved.id).snapshot["markdown"].encode("utf-8") == original_markdown
    )


def test_manual_no_match_does_not_block_a_separate_trusted_request(db, context, tmp_path):
    corpus(tmp_path)
    run = db.get(InvestigationRun, context[1])
    run.question = "unmatchedqueryxyz"
    db.commit()
    manual = pin(context).selection
    assert manual.retrieval.status == "no_matches"
    assert pin(context).selection.id == manual.id
    with sessions()() as session, session.begin():
        trusted = pin_guidance(
            session,
            context[0],
            context[1],
            consumer="analysis",
            request_identity="future-analysis-1",
            query="memory",
        ).selection
    assert trusted.id != manual.id
    assert trusted.retrieval.status == "matched"
    assert trusted.origin == "trusted"
    assert pin(context).selection.retrieval.status == "no_matches"


def test_applicability_is_applied_before_ranking_and_limits(db, context, tmp_path):
    rows = [
        document(
            document_key="wrong-family",
            source_path="runbooks/wrong.md",
            title="Memory memory memory",
            applicability={
                "kind": "Deployment",
                "symptom_families": ["traffic_latency_errors"],
                "recovery_templates": [],
            },
        ),
        document(
            document_key="memory",
            source_path="runbooks/memory.md",
            title="Memory guide",
            applicability={
                "kind": "Deployment",
                "symptom_families": ["oom_restarts"],
                "recovery_templates": [],
            },
        ),
        document(
            document_key="restore",
            source_path="runbooks/restore.md",
            title="Memory restore",
            guide_kind="recovery_template",
            applicability={
                "kind": "Deployment",
                "symptom_families": [],
                "recovery_templates": ["restore_deployment_template"],
            },
        ),
    ]
    write_corpus(tmp_path, rows)
    index_corpus(tmp_path)
    result = retrieve(db, *context[:2], symptom_family="oom_restarts", limit=1)
    assert [c.reference.document_key for c in result.chunks] == ["memory"]
    unknown = retrieve(db, *context[:2])
    assert {c.reference.document_key for c in unknown.chunks} == {"memory", "wrong-family"}
    explicit = retrieve(db, *context[:2], recovery_template="restore_deployment_template")
    assert "restore" in {c.reference.document_key for c in explicit.chunks}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 11},
        {"limit": True},
        {"query": "x" * 2001},
        {"query": " "},
        {"recovery_template": "arbitrary"},
        {"symptom_family": "invented"},
    ],
)
def test_invalid_requests_are_rejected(db, context, kwargs):
    with pytest.raises(DomainError) as error:
        retrieve(db, *context[:2], **kwargs)
    assert error.value.status == 422


def test_utf8_budget_and_stable_ties(db, context, tmp_path):
    rows = [
        document(document_key=f"memory-{i:02d}", source_path=f"runbooks/{i}.md", title="Memory")
        for i in reversed(range(12))
    ]
    body = "# Memory\n\n" + "é" * 2020 + " memory\n"
    write_corpus(tmp_path, rows, {row["document_key"]: body for row in rows})
    assert index_corpus(tmp_path).outcome == "indexed"
    result = retrieve(db, *context[:2])
    assert result.status == "partial" and result.reason == "content_budget"
    assert (
        result.content_bytes
        == sum(len(item.content.encode("utf-8")) for item in result.chunks)
        <= 32768
    )
    keys = [chunk.reference.document_key for chunk in result.chunks]
    assert keys == sorted(keys)
    assert retrieve(db, *context[:2]).model_dump() == result.model_dump()


def test_exact_history_survives_new_version_removal_and_forged_refs_fail(db, context, tmp_path):
    corpus(tmp_path)
    selected = pin(context).selection
    reference = selected.retrieval.chunks[0].reference
    target = resolve_selection_chunk(
        db, *context[:2], selected.id, reference.chunk_id, reference.document_version_id
    )
    expired = Evidence(
        run_id=context[1],
        workload_uid=selected.retrieval.scope.workload_uid,
        source_key="development_fixture",
        source_kind="fixture",
        catalog_version="standard_v1",
        schema_version=2,
        payload_kind="fixture",
        payload_version=1,
        collected_at=datetime.now(timezone.utc) - timedelta(days=40),
        time_relation="not_captured",
        coverage={"status": "available", "reason_codes": []},
        redaction_policy_version="fixture/v1",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        payload={"fixture": "expired"},
    )
    db.add(expired)
    db.commit()
    assert purge_expired(db, apply=True) == 1
    db.expire_all()
    assert db.get(Evidence, expired.id).payload is None
    corpus(tmp_path, version="2")
    write_corpus(tmp_path, [])
    assert index_corpus(tmp_path).outcome == "indexed"
    assert pin(context).selection.model_dump() == selected.model_dump()
    with sessions()() as fresh:
        historical = resolve_selection_chunk(
            fresh, *context[:2], selected.id, reference.chunk_id, reference.document_version_id
        )
        assert historical.model_dump() == target.model_dump()
        for field, value in [
            ("version", "forged"),
            ("document_key", "wrong"),
            ("chunker_version", "unknown"),
            ("chunk_sha256", "0" * 64),
            ("document_sha256", "0" * 64),
            ("metadata_sha256", "0" * 64),
        ]:
            with pytest.raises(DomainError):
                resolve_reference(fresh, reference.model_copy(update={field: value}))
        with pytest.raises(DomainError):
            resolve_selection_chunk(
                fresh, *context[:2], uuid4(), reference.chunk_id, reference.document_version_id
            )
        with pytest.raises(DomainError):
            resolve_selection_chunk(fresh, *context[:2], selected.id, reference.chunk_id, uuid4())
    with pytest.raises(ValidationError):
        KnowledgeRef.model_validate({**reference.model_dump(), "chunk_sha256": "bad"})


def test_last_good_guidance_is_explicitly_degraded(db, context, tmp_path):
    original = corpus(tmp_path)
    (tmp_path / "runbooks/fixture-guide.md").unlink()
    assert index_corpus(tmp_path).outcome == "failed"
    result = retrieve(db, *context[:2])
    assert result.degraded
    assert result.generation_id == original.generation_id
    assert result.chunks and any("retained successful" in text for text in result.limitations)


def test_reference_from_another_run_is_rejected(db, context, tmp_path):
    corpus(tmp_path)
    selected = pin(context).selection
    ref = selected.retrieval.chunks[0].reference
    other = InvestigationRun(
        incident_id=context[0], requested_by=context[2], state="cancelled", reason="Other Run"
    )
    db.add(other)
    db.commit()
    with pytest.raises(DomainError) as error:
        resolve_selection_chunk(
            db, context[0], other.id, selected.id, ref.chunk_id, ref.document_version_id
        )
    assert error.value.status == 404


@pytest.mark.parametrize("family", ["pending_unschedulable", "image_pull"])
def test_manual_only_families_reject_template_context(db, context, family):
    with pytest.raises(DomainError) as error:
        retrieve(db, *context[:2], symptom_family=family, recovery_template="scale_deployment")
    assert error.value.status == 422


def test_duplicate_concurrent_manual_requests_create_one_selection(db, context, tmp_path):
    corpus(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: pin(context).selection.id, range(2)))
    assert ids[0] == ids[1]
    assert db.scalar(select(func.count()).select_from(KnowledgeSelection)) == 1
    assert db.scalar(select(func.count()).select_from(KnowledgeSelectionChunk)) == 1


def test_reindex_during_retrieval_pins_one_complete_generation(db, context, tmp_path, monkeypatch):
    old = corpus(tmp_path)
    started, proceed = Event(), Event()
    original = guidance._retrieve

    def paused(session, scope, query, limit):
        session.get(guidance.KnowledgeIndexState, 1)
        result = original(session, scope, query, limit)
        started.set()
        assert proceed.wait(10)
        return result

    monkeypatch.setattr(guidance, "_retrieve", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pin, context)
        assert started.wait(10)
        new = corpus(tmp_path, version="2")
        proceed.set()
        selected = future.result(timeout=10).selection
    assert selected.retrieval.generation_id == old.generation_id != new.generation_id
    for chunk in selected.retrieval.chunks:
        assert (
            resolve_selection_chunk(
                db,
                *context[:2],
                selected.id,
                chunk.reference.chunk_id,
                chunk.reference.document_version_id,
            ).reference
            == chunk.reference
        )


def test_legacy_request_is_explicit_and_wrong_run_never_falls_back(db, context):
    db.get(InvestigationRun, context[1]).question = None
    db.commit()
    assert pin(context).reason == "legacy_request"
    assert read_guidance(db, *context[:2]).state == "unavailable"
    assert db.scalar(select(func.count()).select_from(KnowledgeSelection)) == 0
    with pytest.raises(DomainError) as error:
        read_guidance(db, uuid4(), context[1])
    assert error.value.status == 404


def test_selection_migration_preserves_existing_catalog_and_refuses_history_loss(
    db, context, tmp_path
):
    indexed = corpus(tmp_path)
    original = db.scalar(select(KnowledgeDocumentVersion)).content
    db.commit()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(config, "0010_knowledge_index")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    db.expire_all()
    assert db.scalar(select(KnowledgeDocumentVersion)).content == original
    assert db.get(InvestigationRun, context[1]).question == "memory"
    db.rollback()
    selected = pin(context).selection
    assert selected.retrieval.generation_id == indexed.generation_id
    with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
        command.downgrade(config, "0010_knowledge_index")
    with sessions()() as fresh:
        assert read_guidance(fresh, *context[:2]).selection.id == selected.id


def test_database_failure_is_typed_and_leaves_no_selection(client, db, context, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    def unavailable(*args):
        raise SQLAlchemyError("private database failure canary")

    monkeypatch.setattr(guidance, "_retrieve", unavailable)
    headers = sign_in(client)
    path = f"/api/incidents/{context[0]}/runs/{context[1]}/knowledge"
    response = client.post(path, headers=headers)
    assert response.status_code == 503
    assert "canary" not in response.text
    assert client.get(path).json()["state"] == "not_selected"
    assert db.scalar(select(func.count()).select_from(KnowledgeSelection)) == 0


def test_api_roles_csrf_reject_browser_scope_and_resolve_exact_membership(
    client, db, context, tmp_path
):
    corpus(tmp_path)
    path = f"/api/incidents/{context[0]}/runs/{context[1]}/knowledge"
    assert client.get(path).status_code == 401
    viewer = sign_in(client, "viewer")
    assert client.get(path).json()["state"] == "not_selected"
    assert client.post(path, headers=viewer).status_code == 403
    operator = sign_in(client, "operator")
    assert client.post(path, headers={"Origin": "http://localhost:5173"}).status_code == 403
    for body in (
        {"consumer": "analysis"},
        {"workload_uid": "other"},
        {"query": "memory"},
        {"recovery_template": "scale_deployment"},
    ):
        assert client.post(path, json=body, headers=operator).status_code == 422
    response = client.post(path, json={}, headers=operator)
    assert response.status_code == 200, response.text
    selected = response.json()["selection"]
    ref = selected["retrieval"]["chunks"][0]["reference"]
    target = f"{path}/selections/{selected['id']}/chunks/{ref['chunk_id']}"
    assert (
        client.get(target, params={"document_version_id": ref["document_version_id"]}).status_code
        == 200
    )
    assert client.get(target, params={"document_version_id": str(uuid4())}).status_code == 404
    sign_in(client, "viewer")
    assert client.get(path).json() == response.json()
    assert (
        client.get(target, params={"document_version_id": ref["document_version_id"]}).status_code
        == 200
    )
