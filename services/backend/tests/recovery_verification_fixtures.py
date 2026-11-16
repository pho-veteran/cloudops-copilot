"""Controlled verification matrices; never presented as live application qualification."""

from datetime import datetime, timedelta, timezone

from observation_fixtures import observed

from cloudops.features.recovery.verification_evaluator import _selected, moments
from cloudops.features.recovery.verification_policy import QUERY_IDS, VerificationProfile

ACK = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def profile(template="scale_deployment"):
    return VerificationProfile.model_validate(
        {
            "schema_version": "RecoveryVerificationProfile/v1",
            "id": "controlled-profile",
            "version": "1",
            "workload_uid": "deploy-uid",
            "template": template,
            "containers": _selected(observed())["containers"],
            "replicas": [1, 2],
            "image_ids": {"app": "sha256:" + "b" * 64},
            "source_version": "controlled-fixture/v1",
            "source_digest": "a" * 64,
            "qualification_reference": "controlled source only; no live quality claim",
            "window_seconds": 180 if template == "scale_deployment" else 300,
            "minimum_coverage": 0.9,
            "minimum_rps": 5,
            "minimum_load_ratio": 0.9,
            "maximum_error_ratio": 0.05,
            "maximum_p95_seconds": 0.5,
            "queries": {
                key: {
                    "query": 'metric{namespace="{{namespace}}",deployment_uid="{{workload_uid}}"}',
                    "unit": "fixture",
                }
                for key in sorted(QUERY_IDS)
            },
        }
    )


def metrics(start, end, *, pod_uid="pod-uid", rps=10, error=0, offered=10):
    stamps = [moment.timestamp() for moment in moments(start, end)]
    values = {
        "request_rate": lambda stamp: rps,
        "error_ratio": lambda stamp: error,
        "p95_latency": lambda stamp: 0.2,
        "requests": lambda stamp: 1000 + (stamp - stamps[0]) * rps,
        "sample_time": lambda stamp: stamp - 2,
        "resets": lambda stamp: 0,
        "offered_rate": lambda stamp: offered,
        "offered_sample_time": lambda stamp: stamp - 2,
    }
    return {
        key: {
            "series": [
                {
                    "uid": "series-" + key,
                    **(
                        {"pod_uid": pod_uid} if key in {"requests", "sample_time", "resets"} else {}
                    ),
                    "samples": [[stamp, function(stamp)] for stamp in stamps],
                }
            ]
        }
        for key, function in values.items()
    }


def capture(at, *, identity=None):
    observation = observed(identity, at)
    value = {
        "rollout": observation.model_dump(mode="json"),
        "state": {
            "coverage": {"status": "available"},
            "redacted_bounded_payload": {
                "pods": [
                    {
                        "uid": "pod-uid",
                        "containers": [
                            {"name": "app", "kind": "regular", "image_id": "sha256:" + "b" * 64}
                        ],
                    }
                ]
            },
        },
        "events": {"coverage": {"status": "available"}, "redacted_bounded_payload": {"events": []}},
    }
    for key in ("state", "events"):
        value[key]["source_times"] = {"observed_at": at.isoformat(), "collected_at": at.isoformat()}
    return value


def case(template="scale_deployment"):
    policy = profile(template)
    end = ACK + timedelta(seconds=policy.window_seconds)
    before = ACK - timedelta(seconds=1)
    baseline = {
        **capture(before),
        "window_start": (before - timedelta(seconds=60)).isoformat(),
        "window_end": before.isoformat(),
        "metrics": metrics(before - timedelta(seconds=60), before, error=0.3),
    }
    identity = observed().identity
    samples = {
        str(slot): capture(moment, identity=identity)
        for slot, moment in enumerate(moments(ACK, end))
    }
    desired = _selected(observed())
    raw = metrics(ACK + timedelta(seconds=60), end)
    return policy, desired, baseline, samples, raw
