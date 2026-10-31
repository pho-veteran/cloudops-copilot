"""Scope publication and observation fencing against the same PostgreSQL state."""

import json

import pytest
from observation_fixtures import observed
from sqlalchemy import func, select
from sqlalchemy import text
from alembic import command
from alembic.config import Config

from cloudops.features.workloads import reconcile
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.observation_models import WorkloadObservation
from cloudops.features.workloads.observation_service import claim, detail, finalize
from cloudops.features.workloads.scope_config import Lookup
from cloudops.features.workloads.service import sync_config
from cloudops.shared.config import settings
from cloudops.shared.database import sessions
from cloudops.shared.database import BACKEND_ROOT, engine


@pytest.mark.parametrize("mode", ["legacy", "v2"])
def test_scope_publications_fence_same_uid_and_retirement_readd(mode, tmp_path, monkeypatch):
    path = tmp_path / "scope.json"
    entries = [
        {
            "cluster": "lab",
            "namespace": "demo",
            "name": "checkout",
            "uid": "deploy-uid",
            "source": "live_kubernetes",
        }
    ]
    policy = {
        "schema_version": 2,
        "cluster": {
            "key": "lab",
            "fingerprint_algorithm": "server-ca-sha256/v1",
            "expected_fingerprint": "a" * 64,
        },
        "deployments": [{"namespace": "demo", "name": "checkout"}],
    }

    class Reader:
        def __init__(self, *args):
            pass

        def lookup(self, target, deadline):
            return Lookup("present", "deploy-uid")

    monkeypatch.setattr(reconcile, "ScopeReader", Reader)
    monkeypatch.setenv("WORKLOADS_FILE", str(path))
    settings.cache_clear()

    def sync(active=True):
        value = (
            entries
            if mode == "legacy"
            else {**policy, "deployments": policy["deployments"] if active else []}
        )
        path.write_text(json.dumps(value if active or mode == "v2" else []))
        with sessions()() as db:
            sync_config(db)

    try:
        sync()
        with sessions()() as db:
            identity = db.scalar(select(Workload.id))
        first = claim(identity, "baseline_candidate")
        assert first.scope_fingerprint == ("a" * 64 if mode == "v2" else None)
        assert finalize(first, observed(first.identity, first.started_at)).outcome == "complete"

        pending = claim(identity, "baseline_candidate")
        sync()  # Even the same UID is a new explicit authority window.
        assert (
            finalize(pending, observed(pending.identity, pending.started_at)).outcome
            == "superseded"
        )
        with sessions()() as db:
            projected = detail(db, identity)
            assert projected.summary.effective_health == "unknown"
            assert projected.historical is not None
            assert db.scalar(select(func.count()).select_from(WorkloadObservation)) == 1

        pending = claim(identity, "baseline_candidate")
        sync(False)
        sync(True)
        assert (
            finalize(pending, observed(pending.identity, pending.started_at)).outcome
            == "superseded"
        )
        with sessions()() as db:
            assert db.scalar(select(Workload.id).where(Workload.active)) == identity
            assert db.scalar(select(func.count()).select_from(WorkloadObservation)) == 1
        fresh = claim(identity, "baseline_candidate")
        assert finalize(fresh, observed(fresh.identity, fresh.started_at)).outcome == "complete"
    finally:
        settings.cache_clear()


def test_observation_upgrade_preserves_populated_scope_authority():
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.downgrade(config, "0004_workload_scope")
    with engine().begin() as db:
        db.execute(
            text(
                "INSERT INTO workload_scope_state (id,latest_sequence,applied_sequence,mode,cluster_key,fingerprint,config_digest) VALUES (1,4,4,'v2','lab',:pin,:digest)"
            ),
            {"pin": "a" * 64, "digest": "b" * 64},
        )
        before = db.execute(
            text("SELECT row_to_json(s)::text FROM workload_scope_state s")
        ).scalar_one()
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine().connect() as db:
        assert (
            db.execute(text("SELECT row_to_json(s)::text FROM workload_scope_state s")).scalar_one()
            == before
        )
    command.check(config)
