"""Pure UID ownership, whitelisted extraction and health/comparison policy."""

from copy import deepcopy
from uuid import uuid4

from pydantic import ValidationError

from cloudops.features.workloads.kubernetes import healthy
from cloudops.features.workloads.observation_schemas import (
    Comparison,
    ConfigReference,
    ContainerObservation,
    DeploymentObservation,
    Identity,
    Observation,
    PodObservation,
    Resources,
    TemplateContainer,
)


class ObservationError(Exception):
    def __init__(self, code="invalid_response"):
        self.code = code
        # Provider messages, destinations and credential paths never enter exceptions.
        super().__init__(code)


def items(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ObservationError("payload_limit" if isinstance(value, list) else "invalid_response")
    if not all(isinstance(item, dict) for item in value):
        raise ObservationError()
    return value


def counter(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ObservationError("status_missing")
    return value


def controller(value):
    owners = items(value.get("metadata", {}).get("ownerReferences", []), 16)
    owners = [owner for owner in owners if owner.get("controller") is True]
    if len(owners) > 1:
        raise ObservationError("ownership_invalid")
    return owners[0] if owners else {}


def template(value):
    result = deepcopy(value["spec"]["template"])
    result.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
    return result


def extract(deployment):
    podspec = deployment["spec"]["template"]["spec"]
    regular = items(podspec["containers"], 32)
    init = items(podspec.get("initContainers", []), 32)
    if not regular or len(regular) + len(init) > 32:
        raise ObservationError("payload_limit")
    fields, refs, reasons = [], [], set()
    for group, containers in (("containers", regular), ("initContainers", init)):
        names = set()
        for container in containers:
            name = container["name"]
            if name in names:
                raise ObservationError()
            names.add(name)
            if group == "containers":
                resources = container.get("resources", {})
                fields.append(
                    TemplateContainer(
                        name=name,
                        image=container["image"],
                        **{
                            kind: Resources(
                                **{
                                    key: resources.get(kind, {}).get(key)
                                    for key in ("cpu", "memory")
                                }
                            )
                            for kind in ("requests", "limits")
                        },
                    )
                )
            for env in items(container.get("env", []), 128):
                if "valueFrom" not in env:
                    continue
                source = env["valueFrom"]
                path = f"{group}/{name}/env/{env['name']}"
                if len(source) != 1:
                    reasons.add("unsupported_reference")
                    continue
                for kind, value in source.items():
                    if kind in ("secretKeyRef", "configMapKeyRef"):
                        refs.append(
                            ConfigReference(
                                path=path,
                                kind="Secret" if kind == "secretKeyRef" else "ConfigMap",
                                name=value["name"],
                                key=value["key"],
                                optional=value.get("optional"),
                            )
                        )
                    elif kind == "fieldRef":
                        refs.append(
                            ConfigReference(
                                path=path,
                                kind="Field",
                                selector=value["fieldPath"],
                                api_version=value.get("apiVersion"),
                            )
                        )
                    elif kind == "resourceFieldRef":
                        refs.append(
                            ConfigReference(
                                path=path,
                                kind="Resource",
                                selector=value["resource"],
                                container_name=value.get("containerName"),
                                divisor=value.get("divisor"),
                            )
                        )
                    else:
                        reasons.add("unsupported_reference")
            for index, env in enumerate(items(container.get("envFrom", []), 128)):
                kinds = set(env) - {"prefix"}
                if len(kinds) != 1 or not kinds <= {"secretRef", "configMapRef"}:
                    reasons.add("unsupported_reference")
                    continue
                kind = next(iter(kinds))
                value = env[kind]
                refs.append(
                    ConfigReference(
                        path=f"{group}/{name}/envFrom/{index}",
                        kind="Secret" if kind == "secretRef" else "ConfigMap",
                        name=value["name"],
                        optional=value.get("optional"),
                    )
                )
    for volume in items(podspec.get("volumes", []), 128):
        sources = [
            (key, value, f"volumes/{volume['name']}")
            for key, value in volume.items()
            if key in {"secret", "configMap"}
        ]
        for index, source in enumerate(items(volume.get("projected", {}).get("sources", []), 128)):
            for key, value in source.items():
                if key in {"secret", "configMap"}:
                    sources.append((key, value, f"volumes/{volume['name']}/projected/{index}"))
                else:
                    reasons.add("unsupported_reference")
        for kind, value, path in sources:
            mapping = items(value.get("items", []), 128)
            for selected in mapping or [{}]:
                refs.append(
                    ConfigReference(
                        path=path + (f"/{selected['path']}" if selected else ""),
                        kind="Secret" if kind == "secret" else "ConfigMap",
                        name=value.get("secretName")
                        if kind == "secret" and "secretName" in value
                        else value["name"],
                        key=selected.get("key"),
                        optional=value.get("optional"),
                    )
                )
    if len(refs) > 128:
        raise ObservationError("payload_limit")
    return (
        sorted(fields, key=lambda item: item.name),
        sorted(refs, key=lambda item: item.path),
        reasons,
    )


FAILURES = {
    "CrashLoopBackOff": "crash_loop",
    "ImagePullBackOff": "image_pull",
    "ErrImagePull": "image_pull",
}


def containers(pod, reasons):
    result, positive = [], True
    spec, status = pod["spec"], pod.get("status", {})
    total = len(spec.get("containers", [])) + len(spec.get("initContainers", []))
    if not total or total > 32:
        raise ObservationError("payload_limit")
    for kind, field, status_field in (
        ("regular", "containers", "containerStatuses"),
        ("init", "initContainers", "initContainerStatuses"),
    ):
        required = items(spec.get(field, []), 32)
        statuses = items(status.get(status_field, []), 32)
        by_name = {entry["name"]: entry for entry in statuses}
        if len(by_name) != len(statuses) or len({c["name"] for c in required}) != len(required):
            raise ObservationError()
        if set(by_name) != {entry["name"] for entry in required}:
            reasons.add("status_missing")
        for container in required:
            entry = by_name.get(container["name"], {})
            state = entry.get("state", {})
            state_kind = next(iter(state), "unknown") if len(state) == 1 else "unknown"
            if state_kind not in {"running", "waiting", "terminated"}:
                state_kind = "unknown"
            count = entry.get("restartCount")
            if count is not None:
                counter(count)
            ready = entry.get("ready") if type(entry.get("ready")) is bool else None
            started = entry.get("started") if type(entry.get("started")) is bool else None
            exit_code = state.get("terminated", {}).get("exitCode")
            if exit_code is not None and type(exit_code) is not int:
                raise ObservationError()
            reason = FAILURES.get(state.get("waiting", {}).get("reason"))
            if state_kind == "terminated" and exit_code not in (None, 0):
                reason = "container_terminated"
            if reason:
                reasons.add(reason)
            native_sidecar = kind == "init" and container.get("restartPolicy") == "Always"
            if count is None or state_kind == "unknown" or ready is None:
                reasons.add("status_missing")
            if native_sidecar and started is None:
                reasons.add("status_missing")
            if state_kind == "terminated" and exit_code is None:
                reasons.add("status_missing")
            if kind == "regular":
                positive &= state_kind == "running" and ready is True
            elif native_sidecar:
                positive &= state_kind == "running" and started is True
                if "readinessProbe" in container:
                    positive &= ready is True
            else:
                positive &= state_kind == "terminated" and exit_code == 0
            result.append(
                ContainerObservation(
                    name=container["name"],
                    kind=kind,
                    ready=ready,
                    started=started,
                    state=state_kind,
                    restart_count=count,
                    exit_code=exit_code,
                    reason=reason,
                )
            )
    return result, positive


def evaluate(identity: Identity, start, end, rs_list, pod_list, started_at, observed_at):
    """Raw objects are used only in memory; the returned DTO cannot contain them."""
    try:
        return _evaluate(identity, start, end, rs_list, pod_list, started_at, observed_at)
    except (KeyError, TypeError, AttributeError, ValidationError, ValueError):
        raise ObservationError() from None


def _evaluate(identity, start, end, rs_list, pod_list, started_at, observed_at):
    if (rs_list.get("kind"), rs_list.get("apiVersion")) != ("ReplicaSetList", "apps/v1") or (
        pod_list.get("kind"),
        pod_list.get("apiVersion"),
    ) != ("PodList", "v1"):
        raise ObservationError()
    for deployment in (start, end):
        meta = deployment["metadata"]
        if deployment["kind"] != "Deployment" or deployment["apiVersion"] != "apps/v1":
            raise ObservationError()
        if (meta["namespace"], meta["name"], meta["uid"]) != (
            identity.namespace,
            identity.name,
            identity.uid,
        ):
            raise ObservationError("target_changed")
    reasons = set()
    if (start["metadata"]["generation"], start["spec"]) != (
        end["metadata"]["generation"],
        end["spec"],
    ):
        reasons.add("read_window_changed")
    selected, refs, extraction_reasons = extract(start)
    reasons |= extraction_reasons
    rs = {}
    for value in items(rs_list["items"], 200):
        owner = controller(value)
        if owner.get("kind") == "Deployment" and owner.get("uid") == identity.uid:
            if owner.get("apiVersion") != "apps/v1":
                raise ObservationError("ownership_invalid")
            if (
                value["metadata"]["namespace"] != identity.namespace
                or value.get("kind", "ReplicaSet") != "ReplicaSet"
                or value.get("apiVersion", "apps/v1") != "apps/v1"
            ):
                raise ObservationError("ownership_invalid")
            uid = value["metadata"]["uid"]
            if uid in rs:
                raise ObservationError("pagination_invalid")
            rs[uid] = value
    current = [uid for uid, value in rs.items() if template(value) == template(start)]
    desired = counter(start["spec"]["replicas"])
    pods, unrelated, active, positive = [], 0, [], True
    seen = set()
    for pod in items(pod_list["items"], 500):
        if pod["metadata"]["uid"] in seen:
            raise ObservationError("pagination_invalid")
        seen.add(pod["metadata"]["uid"])
        owner = controller(pod)
        if owner.get("kind") != "ReplicaSet" or owner.get("uid") not in rs:
            unrelated += 1
            continue
        if owner.get("apiVersion") != "apps/v1":
            raise ObservationError("ownership_invalid")
        if (
            pod.get("kind", "Pod") != "Pod"
            or pod.get("apiVersion", "v1") != "v1"
            or pod["metadata"]["namespace"] != identity.namespace
        ):
            raise ObservationError("ownership_invalid")
        meta, status = pod["metadata"], pod.get("status", {})
        phase = status.get("phase", "Unknown")
        terminating = bool(meta.get("deletionTimestamp"))
        excluded = terminating or phase in {"Succeeded", "Failed"}
        pod_reasons = set()
        evidence, container_ready = containers(pod, pod_reasons)
        conditions = items(status.get("conditions", []), 32)
        ready_values = [c["status"] for c in conditions if c.get("type") == "Ready"]
        ready = (
            ready_values[0] == "True"
            if len(ready_values) == 1 and ready_values[0] in {"True", "False"}
            else None
        )
        if not excluded:
            reasons |= pod_reasons
            if ready is None:
                reasons.add("status_missing")
            if any(
                c.get("type") == "PodScheduled"
                and c.get("status") == "False"
                and c.get("reason") == "Unschedulable"
                for c in conditions
            ):
                reasons.add("unschedulable")
            active.append(pod)
            positive &= ready is True and container_ready and owner["uid"] in current
        pods.append(
            PodObservation(
                uid=meta["uid"],
                name=meta["name"],
                replicaset_uid=owner["uid"],
                current=owner["uid"] in current,
                ready=ready,
                phase=phase,
                terminating=terminating,
                excluded=excluded,
                containers=evidence,
            )
        )
    if len(pods) > 100:
        raise ObservationError("payload_limit")
    if len(current) != 1 and not (desired == 0 and not active and not current):
        reasons.add("current_replicaset_ambiguous")
    status = start.get("status", {})
    counters = {
        name: status.get(key)
        for name, key in (
            ("observed_generation", "observedGeneration"),
            ("ready", "readyReplicas"),
            ("available", "availableReplicas"),
            ("updated", "updatedReplicas"),
            ("replicas", "replicas"),
        )
    }
    for value in counters.values():
        if value is not None:
            counter(value)
    if desired > 0 and any(value is None for value in counters.values()):
        reasons.add("status_missing")
    if any(
        c.get("type") == "Progressing"
        and c.get("status") == "False"
        and c.get("reason") == "ProgressDeadlineExceeded"
        for c in items(status.get("conditions", []), 32)
    ):
        reasons.add("progress_deadline_exceeded")
    incomplete = reasons & {
        "status_missing",
        "current_replicaset_ambiguous",
        "read_window_changed",
        "unsupported_reference",
    }
    coverage, health = "partial" if incomplete else "complete", "unknown"
    if not incomplete:
        if desired == 0 and not active:
            health = "scaled_to_zero"
        elif reasons & {
            "progress_deadline_exceeded",
            "crash_loop",
            "image_pull",
            "container_terminated",
            "unschedulable",
        }:
            health = "degraded"
        elif len(active) == desired and positive and healthy(start):
            health = "healthy"
        else:
            health = "progressing"
            reasons.add("rollout_pending")
    return Observation(
        observation_id=uuid4(),
        identity=identity,
        started_at=started_at,
        observed_at=observed_at,
        deployment=DeploymentObservation(
            generation=counter(start["metadata"]["generation"]),
            resource_version=start["metadata"]["resourceVersion"],
            end_resource_version=end["metadata"]["resourceVersion"],
            desired=desired,
            **counters,
        ),
        replicaset_resource_version=rs_list["metadata"]["resourceVersion"],
        pod_resource_version=pod_list["metadata"]["resourceVersion"],
        current_replicaset_uid=current[0] if len(current) == 1 else None,
        coverage=coverage,
        health=health,
        reasons=sorted(reasons),
        pods=sorted(pods, key=lambda p: p.uid),
        unrelated_pods=unrelated,
        template_fields=selected,
        config_references=refs,
    )


def compare(before: Observation | None, after: Observation) -> Comparison:
    if before is None or before.identity != after.identity:
        return Comparison()
    value = Comparison(
        state="comparable",
        from_observation_id=before.observation_id,
        to_observation_id=after.observation_id,
        from_at=before.observed_at,
        to_at=after.observed_at,
        from_generation=before.deployment.generation,
        to_generation=after.deployment.generation,
    )
    for field, code in (
        ("template_fields", "selected_fields_changed"),
        ("config_references", "references_changed"),
    ):
        if getattr(before, field) != getattr(after, field):
            value.changes.append(code)
    if before.deployment.generation != after.deployment.generation:
        value.changes.append("generation_changed")
    if before.deployment.desired != after.deployment.desired:
        value.changes.append("replicas_changed")

    def counts(observation):
        return {
            (p.uid, c.kind, c.name): c.restart_count
            for p in observation.pods
            for c in p.containers
            if not p.excluded
        }

    left, right = counts(before), counts(after)
    if (
        before.coverage != "complete"
        or after.coverage != "complete"
        or left.keys() != right.keys()
        or any(left[key] is None or right[key] is None or right[key] < left[key] for key in left)
    ):
        value.state = "coverage_changed"
    else:
        value.restart_delta = sum(right[key] - left[key] for key in left)
    return value
