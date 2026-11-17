"""Controlled source histories for qualification/runner tests; never live evidence."""

import copy
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from observation_fixtures import objects, observed
from cloudops.features.recommendations.qualification import History
from cloudops.features.recommendations.schemas import ReportIdentity, ReportWindow
from cloudops.features.recommendations.service import digest
from cloudops.features.recommendations.sources import OwnershipPolicy, Series, SourceData
from cloudops.features.recommendations.krr_protocol import PROFILE, digest as protocol_digest


def six_hour_case():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=6)
    window = ReportWindow(
        kind="exact",
        start=start,
        end=end,
        raw_input_start=start - timedelta(seconds=75),
        raw_input_end=end,
    )
    deployment, _, _ = objects()
    deployment["spec"]["selector"] = {"matchLabels": {"app": "checkout"}}
    first = observed(started_at=start)
    fields = [x.model_dump(mode="json") for x in first.template_fields]
    identity = ReportIdentity(
        workload_id=first.identity.workload_id,
        cluster="lab",
        cluster_fingerprint="a" * 64,
        namespace="demo",
        name="checkout",
        uid="deploy-uid",
        containers=["app"],
        observed_at=start,
        template_digest=digest(fields),
        pod_template_digest=first.pod_template_digest,
        resources_digest=digest(
            [{k: f[k] for k in ("name", "requests", "limits")} for f in fields]
        ),
    )
    observations = [
        first.model_copy(
            deep=True,
            update={
                "started_at": start + timedelta(seconds=t),
                "observed_at": start + timedelta(seconds=t),
            },
        )
        for t in range(-120, 21601, 30)
    ]
    history = History(
        observations, [{"started_at": o.started_at, "outcome": "complete"} for o in observations], 0
    )
    times = [start.timestamp() + t for t in range(-75, 21601, 5)]

    def row(metric, value=1, **labels):
        return Series(
            {"uid": "pod-uid", "pod": "checkout-pod", "container": "app", **labels},
            [(t, value(t) if callable(value) else value) for t in times],
        )

    series = {
        "container_cpu_usage_seconds_total": [
            row("cpu", lambda t: t - times[0], pod_uid="pod-uid")
        ],
        "container_memory_working_set_bytes": [row("memory", 0, pod_uid="pod-uid")],
        "kube_pod_info": [row("info", created_by_kind="ReplicaSet", created_by_name="checkout-rs")],
        "kube_pod_owner": [
            row(
                "owner",
                owner_kind="ReplicaSet",
                owner_name="checkout-rs",
                owner_is_controller="true",
            )
        ],
        "kube_replicaset_owner": [
            row(
                "rs",
                replicaset="checkout-rs",
                owner_kind="Deployment",
                owner_name="checkout",
                owner_is_controller="true",
            )
        ],
        "kube_pod_created": [row("created", start.timestamp() - 600)],
        "kube_pod_container_state_started": [row("started", start.timestamp() - 599)],
        "kube_pod_container_status_restarts_total": [row("restarts", 0)],
    }
    policy = OwnershipPolicy(
        schema_version=1,
        cluster_fingerprint="a" * 64,
        operator_managed_uids=["deploy-uid"],
        resource_managers=[],
    )
    source = SourceData(
        series,
        deployment,
        copy.deepcopy(deployment),
        {"cpu": [], "memory": []},
        policy,
        "b" * 64,
        "c" * 64,
        end,
    )
    return identity, window, history, source


def runner_case():
    case = six_hour_case()
    body = {
        "protocol_version": 1,
        "request_id": str(uuid4()),
        "metrics_source_sha256": "a" * 64,
        "identity": case[0].model_dump(mode="json"),
        "profile": {**PROFILE, "image_digest": "sha256:" + "b" * 64},
    }
    raw = {
        "strategy": {
            "name": "simple",
            "settings": {
                "history_duration": 6,
                "timeframe_duration": 1.25,
                "cpu_percentile": 95,
                "memory_buffer_percentage": 15,
                "points_required": 100,
                "allow_hpa": False,
                "use_oomkill_data": False,
            },
        },
        "config": {"token": "private-config-sentinel"},
        "scans": [
            {
                "object": {
                    "namespace": "demo",
                    "name": "checkout",
                    "kind": "Deployment",
                    "container": "app",
                    "pods": [{"name": "checkout-pod", "deleted": False}],
                    "allocations": {
                        "requests": {"cpu": 0.1, "memory": None},
                        "limits": {"cpu": None, "memory": None},
                    },
                    "annotations": {"secret": "private-annotation-sentinel"},
                },
                "recommended": {
                    "requests": {"cpu": {"value": 0.025}, "memory": {"value": 104857600.0}},
                    "limits": {"cpu": {"value": None}, "memory": {"value": 104857600.0}},
                },
            }
        ],
    }
    observed = {
        "request_sha256": protocol_digest(body),
        "parameters": {"cpu_min": 10, "mem_min": 100, "max_workers": 2},
        "observations": [
            {
                "namespace": "demo",
                "name": "checkout",
                "kind": "Deployment",
                "container": "app",
                "cpu": {"observed": 0.025, "pods": ["checkout-pod"]},
                "memory": {"observed": 25165824.0, "pods": ["checkout-pod"]},
            }
        ],
    }
    return case, body, raw, observed
