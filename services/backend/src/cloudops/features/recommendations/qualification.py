"""Join retained UID observations to raw metrics; never calculate recommendations."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from cloudops.features.recommendations.coverage import assess, cpu_points
from cloudops.features.recommendations.schemas import (
    PodEvidence,
    QualifiedEvidence,
    ResourceEligibility,
)
from cloudops.features.recommendations.service import digest
from cloudops.features.recommendations.sources import SourceData, SourceFailure
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.observation_evaluator import extract
from cloudops.features.workloads.observation_models import ObservationAttempt, WorkloadObservation
from cloudops.features.workloads.observation_schemas import Observation


@dataclass(frozen=True)
class History:
    observations: list[Observation]
    attempts: list[dict]
    epoch: int


def load_history(db, identity, window):
    workload = db.get(Workload, identity.workload_id)
    if (
        not workload
        or not workload.active
        or workload.source != "live_kubernetes"
        or (workload.cluster, workload.namespace, workload.name, workload.uid)
        != (identity.cluster, identity.namespace, identity.name, identity.uid)
    ):
        raise SourceFailure("target_changed")
    start = window.raw_input_start - timedelta(minutes=2)
    rows = db.scalars(
        select(WorkloadObservation)
        .where(
            WorkloadObservation.workload_id == identity.workload_id,
            WorkloadObservation.observed_at >= start,
            WorkloadObservation.observed_at <= window.raw_input_end,
        )
        .order_by(WorkloadObservation.observed_at)
        .limit(1025)
    ).all()
    attempts = db.scalars(
        select(ObservationAttempt)
        .where(
            ObservationAttempt.workload_id == identity.workload_id,
            ObservationAttempt.started_at >= start,
            ObservationAttempt.started_at <= window.raw_input_end,
        )
        .order_by(ObservationAttempt.started_at)
        .limit(1025)
    ).all()
    if len(rows) > 1024 or len(attempts) > 1024:
        raise SourceFailure("ownership_history_limit")
    try:
        observations = [Observation.model_validate(row.payload) for row in rows]
    except ValueError:
        raise SourceFailure("ownership_history_invalid") from None
    return History(
        observations,
        [{"started_at": a.started_at, "outcome": a.outcome} for a in attempts],
        workload.observation_epoch,
    )


def _one(source, metric, uid, container=None):
    rows = [
        s
        for s in source.series.get(metric, [])
        if s.labels.get("pod_uid", s.labels.get("uid")) == uid
        and (container is None or s.labels.get("container") == container)
    ]
    return rows[0] if len(rows) == 1 else None


def _constant(series):
    values = {v for _, v in series.samples} if series else set()
    return next(iter(values)) if len(values) == 1 else None


def _times(series, *, positive=False):
    return [t for t, v in series.samples if not positive or v > 0] if series else []


def _date(value):
    return datetime.fromtimestamp(value, timezone.utc)


def qualify(identity, window, history: History, source: SourceData) -> QualifiedEvidence:
    start, end = window.start.timestamp(), window.end.timestamp()
    global_reasons, exclusions = [], []
    if end - start < 21600 or end - start > 22200:
        global_reasons.append("history_window_unqualified")
    before, after = source.before, source.after
    try:
        if any(
            d["metadata"]["uid"] != identity.uid
            or d["metadata"]["name"] != identity.name
            or d["metadata"]["namespace"] != identity.namespace
            for d in (before, after)
        ):
            global_reasons.append("target_changed")
        if (
            before["spec"]["template"] != after["spec"]["template"]
            or before["spec"]["selector"] != after["spec"]["selector"]
        ):
            global_reasons.append("template_changed")
        fields = [f.model_dump(mode="json") for f in extract(before)[0]]
        if (
            digest(fields) != identity.template_digest
            or digest([{k: f[k] for k in ("name", "requests", "limits")} for f in fields])
            != identity.resources_digest
            or sorted(f["name"] for f in fields) != sorted(identity.containers)
        ):
            global_reasons.append("template_changed")
        if identity.pod_template_digest is None:
            global_reasons.append("template_history_unproven")
        elif digest(before["spec"]["template"]) != identity.pod_template_digest:
            global_reasons.append("template_changed")
        spec = before["spec"]["template"]["spec"]
        if any(
            spec.get(k) for k in ("initContainers", "ephemeralContainers", "resources", "overhead")
        ) or any(
            set(c.get("resources", {}).get(kind, {})) - {"cpu", "memory"}
            for c in spec["containers"]
            for kind in ("requests", "limits")
        ):
            exclusions.append("unsupported_resource_shape")
    except (KeyError, ValueError, TypeError):
        global_reasons.append("identity_unproven")
    observations = history.observations
    if not observations:
        global_reasons.append("ownership_history_missing")
    valid = []
    for observation in observations:
        if (
            (
                observation.identity.workload_id,
                observation.identity.cluster,
                observation.identity.namespace,
                observation.identity.name,
                observation.identity.uid,
                observation.identity.epoch,
            )
            != (
                identity.workload_id,
                identity.cluster,
                identity.namespace,
                identity.name,
                identity.uid,
                history.epoch,
            )
            or observation.coverage != "complete"
            or observation.started_at > observation.observed_at
            or observation.observed_at > window.raw_input_end
        ):
            global_reasons.append("ownership_history_invalid")
            continue
        if (
            digest([f.model_dump(mode="json") for f in observation.template_fields])
            != identity.template_digest
        ):
            global_reasons.append("template_changed")
        valid.append(observation)
        if observation.pod_template_digest is None:
            global_reasons.append("template_history_unproven")
        elif observation.pod_template_digest != identity.pod_template_digest:
            global_reasons.append("template_changed")
    history_coverage = assess(start, end, [o.observed_at.timestamp() for o in valid])
    if history_coverage.reasons:
        global_reasons.append("ownership_history_incomplete")
    expected = {}
    seen_names = {}
    for observation in valid:
        for pod in observation.pods:
            expected.setdefault(pod.uid, []).append((observation, pod))
            seen_names.setdefault(pod.name, set()).add(pod.uid)
    if any(len(uids) > 1 for uids in seen_names.values()):
        global_reasons.append("pod_name_reused")
    rs_names = {
        s.labels.get("replicaset")
        for s in source.series.get("kube_replicaset_owner", [])
        if s.labels.get("owner_name") == identity.name
        and s.labels.get("owner_kind") == "Deployment"
        and s.labels.get("owner_is_controller") == "true"
        and _times(s, positive=True)
    }
    for row in source.series.get("kube_pod_info", []):
        if (
            row.labels.get("created_by_kind") == "ReplicaSet"
            and row.labels.get("created_by_name") in rs_names
            and row.labels.get("uid") not in expected
        ):
            global_reasons.append("ownership_history_missing")
    if not expected:
        global_reasons.append("expected_pods_unproven")
    if len(expected) * len(identity.containers) > 100:
        raise SourceFailure("ownership_history_limit")
    pods = []
    failed_history = any(
        start <= a["started_at"].timestamp() <= end and a["outcome"] != "complete"
        for a in history.attempts
    )
    completed_attempts = {a["started_at"] for a in history.attempts if a["outcome"] == "complete"}
    for uid, seen in expected.items():
        name = seen[0][1].name
        owners = {p.replicaset_uid for _, p in seen}
        reasons = []
        if len(owners) != 1 or any(
            not p.current or p.name != name or p.replicaset_uid != o.current_replicaset_uid
            for o, p in seen
        ):
            reasons.append("pod_ownership_ambiguous")
        info = _one(source, "kube_pod_info", uid)
        owner = _one(source, "kube_pod_owner", uid)
        if (
            not info
            or not owner
            or info.labels.get("pod") != name
            or info.labels.get("created_by_kind") != "ReplicaSet"
            or info.labels.get("created_by_name") not in rs_names
            or owner.labels.get("owner_kind") != "ReplicaSet"
            or owner.labels.get("owner_is_controller") != "true"
            or owner.labels.get("owner_name") != info.labels.get("created_by_name")
        ):
            reasons.append("pod_ownership_unproven")
        created = _constant(_one(source, "kube_pod_created", uid))
        completed = _constant(_one(source, "kube_pod_completion_time", uid))
        if completed is not None and completed <= start:
            continue
        last_observation, last_pod = max(seen, key=lambda pair: pair[0].observed_at)
        current = (
            end - last_observation.observed_at.timestamp() <= 60
            and last_pod.phase == "Running"
            and not last_pod.terminating
        )
        lifetime = (
            "completed"
            if completed is not None and start < completed <= end
            else "current"
            if current
            else "unknown"
        )
        if (
            created is None
            or created > end
            or (completed is not None and created is not None and completed <= created)
        ):
            reasons.append("pod_lifetime_unproven")
        if lifetime == "unknown":
            reasons.append("pod_lifetime_unproven")
        pod_start, pod_end = (
            max(start, created if created is not None and created <= end else start),
            min(end, completed) if lifetime == "completed" else end,
        )
        if pod_end <= pod_start:
            continue
        for container in identity.containers:
            local, memory_reasons = list(reasons), []
            active_start = pod_start
            started_series = _one(source, "kube_pod_container_state_started", uid, container)
            container_started = _constant(started_series)
            if (
                container_started is None
                or created is None
                or not created <= container_started < pod_end
            ):
                memory_reasons.append("container_lifetime_unproven")
            elif (
                container_started <= start
                or created >= start
                and all(
                    c.restart_count == 0
                    for _, p in seen
                    for c in p.containers
                    if c.name == container
                )
            ):
                active_start = max(active_start, container_started)
            else:
                memory_reasons.append("container_lifetime_unproven")
            if assess(active_start, pod_end, _times(started_series)).reasons:
                memory_reasons.append("container_lifetime_unproven")
            states = [
                c
                for _, p in seen
                for c in p.containers
                if c.name == container and c.kind == "regular"
            ]
            unresolved = [
                c
                for o, p in seen
                if active_start <= o.observed_at.timestamp() <= pod_end
                for c in p.containers
                if c.name == container
                and c.kind == "regular"
                and (c.state != "running" or c.reason is not None)
                and not (
                    lifetime == "completed"
                    and p.phase == "Succeeded"
                    and c.state == "terminated"
                    and c.exit_code == 0
                    and c.reason is None
                )
            ]
            if unresolved:
                memory_reasons.append("container_state_unresolved")
            if len(states) != len(seen):
                local.append("container_identity_unproven")
            for metric in ("kube_pod_info", "kube_pod_owner", "kube_pod_created"):
                if assess(active_start, pod_end, _times(_one(source, metric, uid))).reasons:
                    local.append("pod_lifetime_unproven")
            owned_coverage = assess(
                active_start, pod_end, [o.observed_at.timestamp() for o, _ in seen]
            )
            if owned_coverage.reasons:
                local.append("ownership_history_incomplete")
            cpu = _one(source, "container_cpu_usage_seconds_total", uid, container)
            memory = _one(source, "container_memory_working_set_bytes", uid, container)
            restarts = _one(source, "kube_pod_container_status_restarts_total", uid, container)
            if any(row and row.labels.get("pod") != name for row in (cpu, memory, restarts)):
                local.append("metric_identity_unproven")
            cpu_rows = (
                [
                    (t, v)
                    for t, v in cpu.samples
                    if container_started is None or t >= container_started
                ]
                if cpu
                else []
            )
            cpu_at, support, reset = cpu_points(cpu_rows, active_start, pod_end)
            cpu_coverage = assess(active_start, pod_end, cpu_at)
            if not support and (container_started is None or container_started <= start):
                cpu_coverage.reasons.append("cpu_rate_input_missing")
            memory_coverage = assess(active_start, pod_end, _times(memory))
            restart_coverage = assess(active_start, pod_end, _times(restarts))
            if reset:
                memory_reasons.append("counter_reset_history")
            if (
                restart_coverage.reasons
                or failed_history
                or any(
                    o.started_at not in completed_attempts
                    for o, _ in seen
                    if active_start <= o.observed_at.timestamp() <= pod_end
                )
                or any(c.restart_count is None for c in states)
            ):
                memory_reasons.append("oom_history_incomplete")
            restart_values = (
                [v for t, v in restarts.samples if active_start <= t <= pod_end] if restarts else []
            )
            baseline = (
                [v for t, v in restarts.samples if active_start - 120 <= t <= active_start]
                if restarts
                else []
            )
            if not baseline:
                memory_reasons.append("oom_history_incomplete")
            recorded = [c.restart_count for c in states if c.restart_count is not None]
            if any(a > b for a, b in zip(restart_values, restart_values[1:])) or any(
                a > b for a, b in zip(recorded, recorded[1:])
            ):
                memory_reasons.append("counter_reset_history")
            if len(set([*baseline[-1:], *restart_values, *recorded])) > 1:
                memory_reasons.append("unexplained_restart")
            oom = [
                s
                for s in source.series.get("kube_pod_container_status_last_terminated_reason", [])
                if s.labels.get("uid") == uid
                and s.labels.get("container") == container
                and s.labels.get("reason") == "OOMKilled"
                and any(active_start <= t <= pod_end and v > 0 for t, v in s.samples)
            ]
            if oom:
                terminated = _one(
                    source, "kube_pod_container_status_last_terminated_timestamp", uid, container
                )
                recent = terminated and any(
                    active_start <= value <= pod_end for _, value in terminated.samples
                )
                memory_reasons.append("recent_oom" if recent else "oom_timing_unproven")
            pods.append(
                PodEvidence(
                    pod=name,
                    uid=uid,
                    replicaset_uid=next(iter(owners)),
                    container=container,
                    start=_date(active_start),
                    end=_date(pod_end),
                    lifetime=lifetime,
                    cpu=cpu_coverage,
                    memory=memory_coverage,
                    restarts=restart_coverage,
                    cpu_raw_samples=len(cpu.samples) if cpu else 0,
                    memory_raw_samples=len(memory.samples) if memory else 0,
                    reasons=sorted(set(local)),
                    memory_reasons=sorted(set(memory_reasons)),
                )
            )
    resources = []
    for container in identity.containers:
        selected = [p for p in pods if p.container == container]
        for resource in ("cpu", "memory"):
            policy_reasons = [*exclusions, *source.controllers[resource]]
            evidence_reasons = [
                *global_reasons,
                *(
                    r
                    for p in selected
                    for r in [
                        *p.reasons,
                        *getattr(p, resource).reasons,
                        *(p.memory_reasons if resource == "memory" else []),
                    ]
                ),
            ]
            if not selected:
                evidence_reasons.append("expected_pods_unproven")
            if resource == "memory" and "recent_oom" in evidence_reasons:
                policy_reasons.append("recent_oom")
            reasons = sorted(set([*policy_reasons, *evidence_reasons]))
            if len(reasons) > 16:
                reasons = ["source_evidence_incomplete", *reasons[:15]]
            state = "excluded" if policy_reasons else "insufficient_data" if reasons else "eligible"
            resources.append(
                ResourceEligibility(
                    container=container, resource=resource, state=state, reasons=reasons
                )
            )
    return QualifiedEvidence(
        identity=identity,
        window=window,
        query_digest=source.query_digest,
        config_digest=source.config_digest,
        history_digest=digest(
            {
                "observations": [o.model_dump(mode="json") for o in observations],
                "attempts": [
                    {**a, "started_at": a["started_at"].isoformat()} for a in history.attempts
                ],
            }
        ),
        collected_at=source.collected_at,
        pods=pods,
        resources=resources,
        reasons=sorted(set(global_reasons)),
    )
