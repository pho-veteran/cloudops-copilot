from copy import deepcopy
from datetime import timedelta

import pytest
from observation_fixtures import objects, observed

from cloudops.features.workloads.observation_evaluator import ObservationError, compare, evaluate


def test_owned_health_and_whitelist_exclude_sensitive_sources():
    value = observed()
    assert value.health == "healthy" and value.coverage == "complete"
    assert value.current_replicaset_uid == "rs-uid"
    assert value.pods[0].uid == "pod-uid"
    assert value.template_fields[0].requests.cpu == "100m"
    assert value.template_fields[0].requests.model_dump() == {"cpu": "100m"}
    assert value.template_fields[0].limits.model_dump() == {}
    assert all(
        sentinel not in value.model_dump_json()
        for sentinel in (
            "private-env-sentinel",
            "private-args-sentinel",
            "private-annotation-sentinel",
            "private-pod-sentinel",
            "private-termination-sentinel",
        )
    )


@pytest.mark.parametrize("owner", [None, "replacement-rs", "matching-label"])
def test_matching_labels_do_not_establish_ownership(owner):
    data = objects()
    pod = deepcopy(data[2]["items"][0])
    pod["metadata"]["uid"] = "unrelated"
    pod["metadata"]["ownerReferences"] = (
        [] if owner is None else [{"controller": True, "kind": "ReplicaSet", "uid": owner}]
    )
    data[2]["items"].append(pod)
    value = observed(data=data)
    assert value.health == "healthy" and len(value.pods) == 1 and value.unrelated_pods == 1


@pytest.mark.parametrize(
    "case,health,reason",
    [
        ("missing_status", "unknown", "status_missing"),
        ("missing_counter", "progressing", "rollout_pending"),
        ("no_rs", "unknown", "current_replicaset_ambiguous"),
        ("ambiguous_rs", "unknown", "current_replicaset_ambiguous"),
        ("crash", "degraded", "crash_loop"),
        ("image", "degraded", "image_pull"),
        ("terminated", "degraded", "container_terminated"),
        ("unready", "progressing", "rollout_pending"),
        ("old_pod", "progressing", "rollout_pending"),
        ("terminal", "progressing", "rollout_pending"),
        ("terminating", "progressing", "rollout_pending"),
        ("deadline", "degraded", "progress_deadline_exceeded"),
    ],
)
def test_health_precedence_with_complete_and_partial_sources(case, health, reason):
    data = objects()
    deployment, rs, pods = data
    pod = pods["items"][0]
    if case == "missing_status":
        pod["status"].pop("containerStatuses")
    elif case == "missing_counter":
        deployment["status"].pop("readyReplicas")
    elif case == "no_rs":
        rs["items"] = []
    elif case == "ambiguous_rs":
        duplicate = deepcopy(rs["items"][0])
        duplicate["metadata"]["uid"] = "duplicate-rs"
        rs["items"].append(duplicate)
    elif case in {"crash", "image"}:
        pod["status"]["containerStatuses"][0]["state"] = {
            "waiting": {
                "reason": "CrashLoopBackOff" if case == "crash" else "ErrImagePull",
                "message": "private-failure",
            }
        }
    elif case == "terminated":
        pod["status"]["containerStatuses"][0]["state"] = {
            "terminated": {"exitCode": 1, "message": "private-failure"}
        }
    elif case == "unready":
        pod["status"]["containerStatuses"][0]["ready"] = False
    elif case == "old_pod":
        old_rs = deepcopy(rs["items"][0])
        old_rs["metadata"]["uid"] = "old-rs"
        old_rs["spec"]["template"]["spec"]["containers"][0]["image"] = "fixture:old"
        rs["items"].append(old_rs)
        old_pod = deepcopy(pod)
        old_pod["metadata"]["uid"] = "old-pod"
        old_pod["metadata"]["ownerReferences"][0]["uid"] = "old-rs"
        pods["items"].append(old_pod)
    elif case == "terminal":
        pod["status"]["phase"] = "Succeeded"
    elif case == "terminating":
        pod["metadata"]["deletionTimestamp"] = "2026-10-27T00:00:00Z"
    elif case == "deadline":
        deployment["status"]["conditions"] = [
            {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}
        ]
    value = observed(data=data)
    assert value.health == health and reason in value.reasons
    assert "private-failure" not in value.model_dump_json()


def test_scaled_zero_is_complete_without_current_rs_but_never_healthy():
    data = objects()
    data[0]["spec"]["replicas"] = 0
    data[0]["status"] = {}
    data[1]["items"] = []
    data[2]["items"] = []
    result = observed(data=data)
    assert result.health == "scaled_to_zero" and result.coverage == "complete"
    assert result.deployment.ready is None


@pytest.mark.parametrize("failure", [None, "CrashLoopBackOff", "ErrImagePull"])
def test_omitted_zero_replica_counters_preserve_unready_evidence_and_delta(failure):
    data = objects()
    data[0]["status"].pop("readyReplicas")
    data[0]["status"].pop("availableReplicas")
    container = data[2]["items"][0]["status"]["containerStatuses"][0]
    container["ready"] = False
    if failure:
        container["state"] = {"waiting": {"reason": failure}}
    first = observed(data=data)
    assert first.coverage == "complete"
    assert first.health == ("degraded" if failure else "progressing")
    assert first.deployment.ready == first.deployment.available == 0
    container["restartCount"] += 2
    second = observed(first.identity, first.started_at + timedelta(seconds=1), data)
    assert compare(first, second).restart_delta == 2


def test_absent_controller_observation_is_not_an_encoded_zero():
    data = objects()
    data[0]["status"].pop("observedGeneration")
    data[0]["status"].pop("readyReplicas")
    value = observed(data=data)
    assert value.health == "unknown" and value.coverage == "partial"
    assert value.deployment.observed_generation is None
    assert value.deployment.ready is None


@pytest.mark.parametrize("native", [False, True])
def test_regular_and_init_restart_coverage_including_native_sidecars(native):
    data = objects()
    spec = {"name": "init", "image": "fixture:1", **({"restartPolicy": "Always"} if native else {})}
    for target in (
        data[0]["spec"]["template"]["spec"],
        data[1]["items"][0]["spec"]["template"]["spec"],
        data[2]["items"][0]["spec"],
    ):
        target["initContainers"] = [deepcopy(spec)]
    data[2]["items"][0]["status"]["initContainerStatuses"] = [
        {
            "name": "init",
            "ready": native,
            "started": True,
            "restartCount": 2,
            "state": {"running": {}} if native else {"terminated": {"exitCode": 0}},
        }
    ]
    value = observed(data=data)
    assert value.health == "healthy"
    assert value.pods[0].containers[1].restart_count == 2


def test_full_template_matching_and_read_window_change_are_not_whitelist_matching():
    data = objects()
    data[1]["items"][0]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = (
        "other-private"
    )
    assert observed(data=data).health == "unknown"
    value = observed()
    deployment, rs, pods = objects()
    end = deepcopy(deployment)
    end["metadata"]["generation"] += 1
    result = evaluate(
        value.identity, deployment, end, rs, pods, value.started_at, value.observed_at
    )
    assert "read_window_changed" in result.reasons and result.health == "unknown"


def test_restart_comparison_does_not_invent_zero_across_identity_changes():
    before = observed()
    after = before.model_copy(deep=True)
    after.observation_id = __import__("uuid").uuid4()
    after.observed_at += timedelta(seconds=30)
    after.pods[0].containers[0].restart_count += 3
    assert compare(before, after).restart_delta == 3
    after.pods[0].uid = "replacement-pod"
    result = compare(before, after)
    assert result.state == "coverage_changed" and result.restart_delta is None
    after.pods[0].uid = before.pods[0].uid
    after.pods[0].containers[0].restart_count = -1
    assert compare(before, after).restart_delta is None


def test_changed_selected_fields_and_references_use_the_exact_direct_dto():
    before = observed()
    data = objects()
    env = {
        "name": "CONFIG",
        "valueFrom": {"configMapKeyRef": {"name": "config", "key": "setting", "optional": True}},
    }
    for value in (data[0], data[1]["items"][0]):
        value["spec"]["template"]["spec"]["containers"][0]["env"].append(env)
        value["spec"]["template"]["spec"]["containers"][0]["image"] = "fixture:2"
    data[0]["metadata"]["generation"] += 1
    data[0]["status"]["observedGeneration"] += 1
    after = observed(before.identity, data=data)
    result = compare(before, after)
    assert set(result.changes) == {
        "selected_fields_changed",
        "references_changed",
        "generation_changed",
    }
    assert after.config_references[0].name == "config"


def test_malformed_or_negative_status_never_becomes_healthy_zero():
    data = objects()
    data[2]["items"][0]["status"]["containerStatuses"][0]["restartCount"] = -1
    with pytest.raises(ObservationError):
        observed(data=data)


def test_typed_list_items_can_omit_typemeta_as_in_real_kubernetes_responses():
    data = objects()
    for collection in data[1:]:
        for value in collection["items"]:
            value.pop("kind")
            value.pop("apiVersion")
    assert observed(data=data).health == "healthy"
    data[1]["apiVersion"] = "unsupported/v1"
    with pytest.raises(ObservationError):
        observed(data=data)
