# Investigate traffic, latency and error increases

## Applicability

Use this guide when a Deployment's request rate, error ratio or latency changes, or when a traffic pack is being reviewed. The guidance applies to the Run's exact UID, workload scope, immutable window and captured metric series. It is not a claim that the workload is the source of a user-facing incident.

## Evidence to inspect

- Inspect `prometheus.request_rate`, `prometheus.error_ratio` and `prometheus.p95_latency` together. Read the stored query ID/version, resolved query, window, step, unit, source identity, warnings and coverage.
- Inspect `prometheus.memory_working_set`, `prometheus.memory_limit` and `prometheus.restarts` when present as supporting context, not as proof of a cause.
- Inspect `kubernetes.state` and `kubernetes.events` for revision, readiness, restarts and related events. Inspect logs only when their coverage is applicable and usable.
- Read the Run timeline for window, rollout observation, incident activity and execution markers. Markers align recorded times; they do not prove causality.

See [scoped Prometheus metric evidence](../docs/prometheus-metrics.md) and [Run timeline](../docs/prometheus-metrics.md#timeline).

## Supporting and contradicting observations

Supporting observations include an error-ratio increase with a compatible latency/request-rate change, a revision or load change near onset, and metric series that are finite, in-window and bound to the exact UID. A traffic increase alone does not prove application saturation.

Stable latency while errors rise from rejected requests can contradict the hypothesis that every failing request experienced increased latency. A request-rate drop and small denominator can weaken an apparent error-ratio trend without ruling out real failures. Missing series prevent a valid rate/ratio calculation and are an evidence gap, not counter-evidence. Gapped, stale or zero-traffic windows do not establish healthy service or a zero error count.

Never interpolate across missing samples. A healthy workload after a load generator stops does not prove that a scaling or application change restored service.

## Investigation steps

1. Confirm the UID, window, step and pack coverage for all three required traffic metrics. Note any `partial` or `unavailable` source before comparing values.
2. Validate units and denominators. Check whether request rate and error ratio are usable in the same interval; a ratio without a meaningful request denominator remains unverified.
3. Compare latency and error onset with revision, readiness, restarts and load changes from stored state/events/timeline. Keep correlation separate from cause.
4. Inspect per-series gaps and stale points. Do not draw a continuous trend through a gap or treat a missing sample as zero.
5. Separate application behavior from dependency, network and capacity hypotheses. The current evidence boundary does not collect every node, dependency or CPU-throttling source; do not claim those were checked.
6. If the workload has a supported, exact scaling candidate and fresh evidence, continue with [Review bounded Deployment scaling](scale-deployment.md). Scaling is not justified by a metric spike alone.

## Missing and unverified cases

- Missing samples, `no_samples`, `stale`, `truncated`, `ownership_unproven`, `timeout` and `forbidden` are typed limitations, not zero values.
- A traffic pack can be available while Kubernetes logs/events are missing. The pack status does not describe application health.
- Prometheus series are fixed query slots for the configured workload. They do not cover arbitrary services, dependencies, clients or business success rates.
- CPU throttling, node pressure and dependency health are not inferred from these metric slots.
- The timeline is bounded and may be truncated. A missing marker is not proof that an event did not occur.

## Manual continuation and abstention

Continue manually when the metric series are partial, the denominator is unclear, or the evidence cannot distinguish load from application/dependency behavior. Verify fresh scope and collect the missing source through an authorized path; record what remains unverified.

Abstain from a root cause, a ruled-out cause, or a recovery recommendation when traffic evidence is incomplete. Do not scale solely because latency rose, and do not use a runbook hit as an execution approval. Any scaling action needs the exact plan, controller check, capacity/bounds and fresh review described in the recovery guide.

## Related guidance

- [Scoped Prometheus metric evidence](../docs/prometheus-metrics.md)
- [Run timeline](../docs/prometheus-metrics.md#timeline)
- [Review bounded Deployment scaling](scale-deployment.md)
