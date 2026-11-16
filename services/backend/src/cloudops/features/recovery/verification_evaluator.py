"""Deterministic application/rollout checks over a fixed, explicit denominator."""

import math
from datetime import datetime, timedelta
from statistics import median

from cloudops.features.workloads.observation_evaluator import compare
from cloudops.features.workloads.observation_schemas import Observation


def moments(start, end, step=30):
    return [
        start + timedelta(seconds=i * step)
        for i in range(int((end - start).total_seconds()) // step + 1)
    ]


def _points(value, expected):
    result = {}
    for series in value.get("series", []):
        key = series.get("pod_uid") or series["uid"]
        if key in result:
            raise ValueError("duplicate_series")
        rows = {}
        for stamp, number in series["samples"]:
            if not math.isfinite(number) or stamp in rows:
                raise ValueError("invalid_sample")
            rows[stamp] = number
        result[key] = {
            stamp: next(
                (number for actual, number in rows.items() if abs(actual - stamp) <= 0.001), None
            )
            for stamp in expected
        }
    return result


def metric_summary(profile, raw, start, end, *, pod_uids=None, baseline_offered=None):
    expected = [moment.timestamp() for moment in moments(start, end, profile.step_seconds)]
    base = {"expected_samples": len(expected), "valid_samples": 0, "coverage": 0.0}
    try:
        points = {key: _points(raw[key], expected) for key in profile.queries}
        aggregate = {
            "request_rate",
            "error_ratio",
            "p95_latency",
            "offered_rate",
            "offered_sample_time",
        }
        if any(len(points[key]) != 1 for key in aggregate):
            raise ValueError("missing_or_ambiguous_metric")
        identities = set(points["requests"])
        if (
            not identities
            or any(set(points[key]) != identities for key in ("sample_time", "resets"))
            or (pod_uids is not None and identities != set(pod_uids))
            or any(
                series.get("pod_uid") not in identities
                for series in raw["requests"].get("series", [])
            )
        ):
            raise ValueError("pod_coverage_changed")
        values = {key: next(iter(points[key].values())) for key in aggregate}
        valid, failed, reasons, offered = [], [], set(), []
        previous = None
        for stamp in expected:
            row = {key: data[stamp] for key, data in values.items()}
            pods = {
                key: {uid: data[stamp] for uid, data in points[key].items()}
                for key in ("requests", "sample_time", "resets")
            }
            if any(value is None for value in row.values()) or any(
                value is None for data in pods.values() for value in data.values()
            ):
                reasons.add("missing_samples")
                previous = None
                continue
            if not 0 <= stamp - row["offered_sample_time"] <= profile.source_age_seconds or any(
                not 0 <= stamp - value <= profile.source_age_seconds
                for value in pods["sample_time"].values()
            ):
                reasons.add("stale_samples")
                previous = None
                continue
            if any(value != 0 for value in pods["resets"].values()) or any(
                value < 0 for value in pods["requests"].values()
            ):
                reasons.add("counter_reset")
                previous = None
                continue
            if (
                row["offered_rate"] < profile.minimum_rps
                or row["request_rate"] < profile.minimum_rps
            ):
                reasons.add("insufficient_traffic")
                previous = None
                continue
            if (
                baseline_offered is not None
                and row["offered_rate"] < baseline_offered * profile.minimum_load_ratio
            ):
                reasons.add("falling_traffic")
                previous = None
                continue
            if not 0 <= row["error_ratio"] <= 1 or row["p95_latency"] < 0:
                reasons.add("invalid_metric")
                previous = None
                continue
            if previous is not None:
                before_stamp, before_counts = previous
                delta = sum(pods["requests"][uid] - before_counts[uid] for uid in identities)
                if any(pods["requests"][uid] < before_counts[uid] for uid in identities):
                    reasons.add("counter_reset")
                    previous = None
                    continue
                if delta / (stamp - before_stamp) < profile.minimum_rps:
                    reasons.add("insufficient_request_denominator")
                    previous = None
                    continue
            previous = stamp, pods["requests"]
            valid.append(stamp)
            offered.append(row["offered_rate"])
            if (
                row["error_ratio"] > profile.maximum_error_ratio
                or row["p95_latency"] > profile.maximum_p95_seconds
            ):
                failed.append(stamp)
        coverage = len(valid) / len(expected)
        state = (
            "inconclusive"
            if reasons or coverage < profile.minimum_coverage
            else "failed"
            if failed
            else "passed"
        )
        return {
            **base,
            "valid_samples": len(valid),
            "coverage": coverage,
            "state": state,
            "reason_codes": sorted(reasons),
            "failed_samples": failed,
            "offered_rps": median(offered) if offered else None,
            "pod_uids": sorted(identities),
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return {**base, "state": "inconclusive", "reason_codes": ["required_metrics_unavailable"]}


def offered_baseline(profile, raw, start, end):
    """A failed workload may have no served requests; offered traffic is independent."""
    expected = [moment.timestamp() for moment in moments(start, end)]
    try:
        rates = _points(raw["offered_rate"], expected)
        freshness = _points(raw["offered_sample_time"], expected)
        if len(rates) != 1 or len(freshness) != 1:
            return None
        values, times = next(iter(rates.values())), next(iter(freshness.values()))
        if any(
            values[stamp] is None
            or values[stamp] < profile.minimum_rps
            or times[stamp] is None
            or not 0 <= stamp - times[stamp] <= profile.source_age_seconds
            for stamp in expected
        ):
            return None
        return median(values.values())
    except (KeyError, TypeError, ValueError):
        return None


def _selected(observation):
    return {
        "uid": observation.identity.uid,
        "replicas": observation.deployment.desired,
        "containers": [
            {
                "name": c.name,
                "image": c.image,
                "resources": {
                    "requests": c.requests.model_dump(exclude_none=True),
                    "limits": c.limits.model_dump(exclude_none=True),
                },
            }
            for c in observation.template_fields
        ],
    }


def evaluate(profile, acknowledged_at, desired, baseline, samples, raw_metrics):
    scoring_start = acknowledged_at + timedelta(seconds=profile.warmup_seconds)
    deadline = acknowledged_at + timedelta(seconds=profile.window_seconds)
    reasons, failures, changes, valid, previous = set(), set(), [], [], None
    baseline_end = datetime.fromisoformat(baseline["window_end"])
    baseline_start = datetime.fromisoformat(baseline["window_start"])
    if not timedelta(0) <= acknowledged_at - baseline_end <= timedelta(
        seconds=60
    ) or baseline_end - baseline_start != timedelta(seconds=60):
        reasons.add("prechange_window_unavailable")
    offered = offered_baseline(profile, baseline.get("metrics", {}), baseline_start, baseline_end)
    if offered is None:
        reasons.add("baseline_offered_traffic_unavailable")
    event_counts, event_deltas = {}, []
    original_pods = set()
    try:
        previous = Observation.model_validate(baseline["rollout"])
        if previous.identity.uid != desired["uid"] or not timedelta(
            0
        ) <= acknowledged_at - previous.observed_at <= timedelta(seconds=60):
            raise ValueError("prechange_identity_or_clock_unavailable")
        original_pods = {pod.uid for pod in previous.pods}
        if baseline["events"]["coverage"]["status"] != "available":
            raise ValueError("baseline_events_unavailable")
        event_counts = {
            event["uid"]: event["count"]
            for event in baseline["events"]["redacted_bounded_payload"]["events"]
        }
        if any(type(count) is not int or count < 1 for count in event_counts.values()):
            raise ValueError("baseline_event_counter_unavailable")
    except (KeyError, TypeError, ValueError):
        reasons.add("failure_baseline_unavailable")
        previous = None
    expected = moments(acknowledged_at, deadline)
    scored = moments(scoring_start, deadline)
    identities = None
    for slot, due in enumerate(expected):
        sample = samples.get(str(slot))
        if not sample:
            if due >= scoring_start:
                reasons.add("missing_rollout_sample")
            continue
        try:
            observation = Observation.model_validate(sample["rollout"])
            if (
                observation.identity.uid != desired["uid"]
                or observation.started_at < due
                or observation.observed_at
                > due + timedelta(seconds=profile.sample_lateness_seconds)
                or _selected(observation) != desired
                or observation.coverage != "complete"
            ):
                raise ValueError("wrong_identity_window_or_desired_state")
            state, events = sample["state"], sample["events"]
            if (
                state["coverage"]["status"] != "available"
                or events["coverage"]["status"] != "available"
            ):
                raise ValueError("incomplete_failure_evidence")
            for source in (state, events):
                at = datetime.fromisoformat(source["source_times"]["observed_at"])
                collected = datetime.fromisoformat(source["source_times"]["collected_at"])
                if (
                    not due
                    <= at
                    <= collected
                    <= due + timedelta(seconds=profile.sample_lateness_seconds)
                ):
                    raise ValueError("failure_evidence_outside_slot")
            active = {pod.uid for pod in observation.pods if not pod.excluded}
            state_pods = {pod["uid"]: pod for pod in state["redacted_bounded_payload"]["pods"]}
            if due >= scoring_start:
                if not active or not active <= state_pods.keys():
                    raise ValueError("failure_identity_coverage_changed")
                for uid in active:
                    images = {
                        c["name"]: c.get("image_id")
                        for c in state_pods[uid].get("containers", [])
                        if c.get("kind") == "regular"
                    }
                    if images != profile.image_ids:
                        raise ValueError("qualified_runtime_image_changed")
            for pod in state["redacted_bounded_payload"]["pods"]:
                for container in pod.get("containers", []):
                    terminated = container.get("last_state", {}).get("terminated", {})
                    if terminated.get("reason") == "OOMKilled":
                        finished = datetime.fromisoformat(
                            terminated["finished_at"].replace("Z", "+00:00")
                        )
                        if finished >= acknowledged_at:
                            failures.add("new_oom")
            current_counts = {}
            for event in events["redacted_bounded_payload"]["events"]:
                count = event.get("count")
                if type(count) is not int or count < 1:
                    raise ValueError("event_counter_unavailable")
                key = event["uid"]
                current_counts[key] = count
                if key in event_counts and count < event_counts[key]:
                    raise ValueError("event_counter_reset")
                if count > event_counts.get(key, 0):
                    event_deltas.append(
                        {"slot": slot, "uid": key, "delta": count - event_counts.get(key, 0)}
                    )
                if event.get("type") == "Warning" and count > event_counts.get(key, 0):
                    seen = datetime.fromisoformat(event["last_seen"].replace("Z", "+00:00"))
                    if seen >= scoring_start:
                        failures.add("new_warning_event")
            event_counts.update(current_counts)
            if previous is not None:
                delta = compare(previous, observation)
                changes.append(delta.model_dump(mode="json"))
                if delta.restart_delta is not None and delta.restart_delta > 0:
                    failures.add("new_restarts")
                elif due > scoring_start and delta.state != "comparable":
                    raise ValueError("pod_or_restart_coverage_changed")
            if due >= scoring_start and any(
                c.restart_count is None or c.restart_count > 0
                for pod in observation.pods
                if not pod.excluded
                for c in pod.containers
            ):
                # Compatible deltas above retain attribution; a newly observed restarted Pod cannot imply zero failures.
                if previous is None:
                    reasons.add("restart_baseline_unavailable")
            if any(
                c.restart_count is not None and c.restart_count > 0
                for pod in observation.pods
                if pod.uid not in original_pods and not pod.excluded
                for c in pod.containers
            ):
                failures.add("new_pod_restarts")
            previous = observation
            if due >= scoring_start:
                current = {pod.uid for pod in observation.pods if not pod.excluded}
                if identities is not None and current != identities:
                    raise ValueError("pod_coverage_changed")
                identities = current
                if observation.health != "healthy":
                    failures.add("rollout_not_healthy")
                valid.append(due.isoformat())
        except (KeyError, TypeError, ValueError):
            if due >= scoring_start:
                reasons.add("rollout_failure_or_window_coverage_incomplete")
            previous = None
    application = metric_summary(
        profile,
        raw_metrics,
        scoring_start,
        deadline,
        pod_uids=identities,
        baseline_offered=offered,
    )
    coverage = len(valid) / len(scored)
    if application["state"] == "inconclusive" or coverage < profile.minimum_coverage:
        reasons.add("insufficient_qualified_observations")
    if application["state"] == "failed":
        failures.add("application_threshold_failed")
    state = "inconclusive" if reasons else "failed" if failures else "passed"
    return {
        "schema_version": "RecoveryVerificationResult/v1",
        "state": state,
        "reason_codes": sorted(reasons),
        "failed_checks": sorted(failures),
        "expected_rollout_samples": len(scored),
        "valid_rollout_samples": len(valid),
        "rollout_coverage": coverage,
        "application": application,
        "pod_changes": changes,
        "event_deltas": event_deltas,
        "interpretation": "Observed memory mitigation; no repaired-leak claim."
        if profile.template == "set_memory_profile"
        else "Fresh post-change observations; acknowledgement alone is not causal proof.",
    }
