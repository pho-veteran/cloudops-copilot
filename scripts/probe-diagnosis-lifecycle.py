"""Linux owned-lab restore acceptance stages; no external provider or cluster calls.

Run seed, take a real pg_dump, enter host dispatch recovery, restore the dump,
then run verify. See docs/features/diagnosis/lifecycle.md for the explicit procedure.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["seed", "verify"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owned-test-lab", action="store_true", required=True)
    args = parser.parse_args()
    from cloudops.shared.database import engine

    if os.name == "nt" or not (engine().url.database or "").endswith("_test"):
        parser.error("Use an owned Linux acceptance container and a dedicated _test database.")
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path("/workspace/services/backend/tests")))
    run(args.stage, args.output)


def run(stage, output):
    from datetime import timedelta
    import threading
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select, text
    from diagnosis_fake import respond, serve
    from diagnosis_fixtures import profile, text_payload
    from test_diagnosis_validation import _crashloop, _raw, policy
    from knowledge_fixtures import document, write_corpus
    from cloudops.features.knowledge.indexing import index_corpus
    from cloudops.entrypoints import metadata  # noqa: F401
    from cloudops.entrypoints.api import app
    from cloudops.entrypoints.dispatch import GATE
    from cloudops.entrypoints.worker import HANDLERS, run_once
    from cloudops.features.access.models import LoginSession, User
    from cloudops.features.access.service import provision
    from cloudops.features.diagnosis.lifecycle import ANALYSIS_JOB, admit
    from cloudops.features.diagnosis.models import Analysis, AnalysisResult, AnalysisContext
    from cloudops.features.diagnosis.settings import Deployment
    from cloudops.features.diagnosis.slot import ModelSlot
    from cloudops.features.diagnosis.worker import _prepare, _reserve
    from cloudops.features.incidents.models import Incident
    from cloudops.features.investigations.models import InvestigationRun
    from cloudops.features.workloads.models import Workload
    from cloudops.features.workloads.service import sync_config
    from cloudops.shared.database import BACKEND_ROOT, database_now, sessions
    from cloudops.shared.jobs import Job, claim_next

    def admin(operation, *extra):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cloudops.entrypoints.dispatch_admin",
                operation,
                *extra,
                "--reason",
                "Owned E06 restore acceptance",
                "--evidence",
                "probe-diagnosis-lifecycle",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def snapshot():
        with sessions()() as db:
            tables = [
                "evidence",
                "knowledge_document_versions",
                "knowledge_chunks",
                "knowledge_selections",
                "incident_activity",
                "recommendation_reports",
                "diagnosis_contexts",
                "diagnosis_results",
                "diagnosis_calls",
                "diagnosis_analyses",
            ]
            captured = {}
            for table in tables:
                projection = (
                    "(to_jsonb(t)-'snapshot')"
                    if table == "diagnosis_contexts"
                    else "row_to_json(t)"
                )
                rows = (
                    db.execute(text("SELECT " + projection + "::text FROM " + table + " t"))
                    .scalars()
                    .all()
                )
                captured[table] = hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()
            return captured

    def working_contexts():
        with sessions()() as db:
            return {
                str(c.analysis_id): hashlib.sha256(
                    json.dumps(
                        c.snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                ).hexdigest()
                for c in db.scalars(select(AnalysisContext))
            }

    state_path = output / "private-state.json"
    if stage == "seed":
        command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
        with sessions()() as db:
            assert db.scalar(select(func.count()).select_from(User)) == 0, (
                "Preserve an existing populated lab."
            )
            db.rollback()
            provision(db, "restore-operator", "owned-restoration-fixture-password", "operator")
            sync_config(db)
        admin("bootstrap", "--fresh-install")
        corpus = output / "corpus"
        write_corpus(
            corpus,
            [document(title="CrashLoopBackOff fixture guide")],
            {
                "fixture-guide": "# CrashLoopBackOff fixture guide\n\nCrashLoopBackOff startup evidence.\n"
            },
        )
        assert index_corpus(corpus).outcome == "indexed"
        state = {"runs": [], "analyses": []}
        mode = ["supported"]

        def handler(h):
            respond(
                h,
                text_payload(_raw()) if mode[0] == "supported" else {},
                status=200 if mode[0] == "supported" else 429,
            )

        with serve(handler) as server:
            model, egress = (
                profile(base_url=server.url + "/v1", mode="operational", credential=None),
                policy(),
            )
            for filename, env, payload in (
                (
                    "profile.json",
                    "DIAGNOSIS_PROFILE_FILE",
                    model.model_dump(mode="json", by_alias=True),
                ),
                (
                    "egress.json",
                    "DIAGNOSIS_EGRESS_FILE",
                    egress.model_dump(mode="json", by_alias=True),
                ),
                (
                    "qualification.json",
                    "DIAGNOSIS_QUALIFICATION_FILE",
                    {
                        "schema_version": "DiagnosisQualification/v1",
                        "cohort_id": "owned-loopback-fixture",
                        "evidence_reference": "restore-fixture-no-live-quality",
                        "identity": Deployment(model, egress, None).identity(),
                    },
                ),
            ):
                path = output / filename
                path.write_text(json.dumps(payload))
                os.environ[env] = str(path)
                state[env] = str(path)

            for label in ("supported", "pending", "quota", "unknown"):
                with sessions()() as db:
                    actor, workload = db.scalar(select(User.id)), db.scalar(select(Workload))
                    incident = Incident(
                        workload_id=workload.id,
                        title="Owned " + label,
                        description="Controlled evidence fixture",
                        created_by=actor,
                    )
                    db.add(incident)
                    db.flush()
                    now = database_now(db)
                    run = InvestigationRun(
                        incident_id=incident.id,
                        requested_by=actor,
                        state="insufficient_evidence",
                        question="Why is the workload in CrashLoopBackOff?",
                        question_source="operator",
                        window_start=now - timedelta(minutes=30),
                        window_end=now,
                        window_source="explicit",
                        pack_coverage=[],
                    )
                    db.add(run)
                    db.commit()
                    scope = incident.id, run.id, workload.uid
                    _crashloop(db, scope)
                    analysis = admit(db, run)
                    db.commit()
                    state["runs"].append(str(run.id))
                    state["analyses"].append(str(analysis.id))
                    if label == "supported":
                        assert run_once()
                    elif label == "quota":
                        mode[0] = "quota"
                        assert run_once()
                    else:
                        with ModelSlot() as slot:
                            claim = claim_next({ANALYSIS_JOB: HANDLERS[ANALYSIS_JOB]}, GATE)
                            prepared, _, checked = _prepare(claim, GATE, slot)
                            if label == "unknown":
                                _reserve(claim, GATE, slot, checked, threading.Event(), prepared)
                    if label == "supported":
                        state["completed_incident"] = str(incident.id)
            # Saved real v2 handoff bytes, created through the authenticated lifecycle API.
            with TestClient(app) as client:
                session = client.post(
                    "/api/login",
                    json={
                        "username": "restore-operator",
                        "password": "owned-restoration-fixture-password",
                    },
                    headers={"Origin": "http://localhost:5173"},
                ).json()
                resolved = client.post(
                    f"/api/incidents/{state['completed_incident']}/lifecycle",
                    json={"action": "resolve", "reason": "Owned acceptance resolution"},
                    headers={
                        "Origin": "http://localhost:5173",
                        "X-CSRF-Token": session["csrf_token"],
                    },
                )
                assert resolved.status_code == 200, resolved.status_code
            state["wire_calls"] = len(server.requests)
        state["snapshot"] = snapshot()
        state["working_contexts"] = working_contexts()
        with sessions()() as db:
            state["jobs"] = {
                str(j.id): {"subject": str(j.subject_id), "attempts": j.attempts}
                for j in db.scalars(select(Job))
            }
        state_path.write_text(json.dumps(state))
        print(
            json.dumps(
                {
                    "seeded": True,
                    "analyses": 4,
                    "wire_calls": state["wire_calls"],
                    "proof": "Controlled stored evidence, real transport, worker and saved handoff.",
                }
            )
        )
        return

    state = json.loads(state_path.read_text())
    for name in ("DIAGNOSIS_PROFILE_FILE", "DIAGNOSIS_EGRESS_FILE", "DIAGNOSIS_QUALIFICATION_FILE"):
        os.environ[name] = state[name]
    assert snapshot() == state["snapshot"], (
        "Restored evidence/Knowledge/handoff/result bytes changed."
    )
    assert working_contexts() == state["working_contexts"], (
        "Restored active working context changed."
    )
    with sessions()() as db:
        restored_jobs = {
            str(j.id): {"subject": str(j.subject_id), "attempts": j.attempts}
            for j in db.scalars(select(Job))
        }
        assert restored_jobs == state["jobs"]
        assert db.scalar(select(func.count()).select_from(LoginSession)) > 0
    assert not GATE.status(ANALYSIS_JOB)[0]
    cleanup = subprocess.run(
        [sys.executable, "-m", "cloudops.entrypoints.diagnosis_recovery"],
        capture_output=True,
        text=True,
        check=True,
    )
    admin("resume", "--class", "analysis")
    admin("exit-recovery")
    assert not run_once(), "Restored pending model work must not be dispatched."
    with sessions()() as db:
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 0
        outcomes = {str(a.id): a.reason for a in db.scalars(select(Analysis))}
        assert outcomes[state["analyses"][0]] == "validated_advice"
        assert all(outcomes[x] == "restore_quarantined" for x in state["analyses"][1:3])
        assert outcomes[state["analyses"][3]] == "provider_outcome_unknown"
        assert db.scalar(select(func.count()).select_from(AnalysisResult)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AnalysisContext)
                .where(AnalysisContext.snapshot.is_not(None))
            )
            == 0
        )
    after = snapshot()
    for table in state["snapshot"]:
        if table not in {"diagnosis_calls", "diagnosis_analyses"}:
            assert after[table] == state["snapshot"][table], (
                "Cleanup altered retained historical bytes."
            )
    receipt = {
        "schema": "E06ActualRestore/v1",
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "restored_bytes_unchanged": True,
        "job_ids_attempts_preserved_before_cleanup": True,
        "cleanup": json.loads(cleanup.stdout),
        "post_resume_replay": False,
        "private_working_contexts_cleared": True,
        "context_manifests_preserved": True,
        "operational_provider_quality": "not_measured",
        "source": "Controlled fixture; actual pg_dump/pg_restore and Linux authority.",
    }
    (output / "restore-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
