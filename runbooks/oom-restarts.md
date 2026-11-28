# Investigate OOM and repeated restarts

## Applicability

Use this guide when a Deployment reports repeated restarts, a container terminated with an out-of-memory reason, or memory pressure is suspected. The guide is tied to the Run's immutable workload UID and captured window.

An OOM event is an observation about a termination. It is not proof of a memory leak, a bad limit, or a permanent fix.

## Evidence to inspect

- Inspect `kubernetes.state` for container restart counts, last termination reason and exit context, current state, resource requests/limits and the owned Pod/ReplicaSet identities.
- Inspect `kubernetes.events` for restart, termination and eviction-related events. Distinguish an OOM kill from probe, image, scheduling or application-exit restarts.
- Inspect `prometheus.memory_working_set` and `prometheus.memory_limit` using the stored window, step, unit, source identity and coverage. Compare the same UID; never align a series from a replacement Deployment.
- Inspect `prometheus.restarts` when available as supporting telemetry, not as a standalone conclusion.
- Inspect `kubernetes.logs.current` and `kubernetes.logs.previous` when applicable. Missing previous logs after a restart are a gap.

See [scoped Prometheus metric evidence](../docs/features/investigations/prometheus-metrics.md) for metric coverage and missing-sample semantics.

## Supporting and contradicting observations

Supporting evidence includes a terminated reason consistent with OOM, a restart count increase in the same window, memory working set approaching the configured limit, and logs or events consistent with memory exhaustion. The coincidence of a recent rollout or traffic increase is a lead, not proof.

Evidence of a different restart cause, such as an application exit or failed probe, can contradict an OOM explanation for that specific restart; image or scheduling failures can instead explain why a container never started. None excludes a separate memory problem in another container or interval. A single OOM observation does not establish a leak, and a working set below a limit in a later window does not disprove earlier pressure. Absent memory series are an evidence gap, not support for or against a memory hypothesis.

`partial`, `no_samples`, `stale`, `truncated`, `ownership_unproven` and `source_unavailable` are coverage facts. Missing samples must not be treated as zero pressure, and an unavailable metric does not rule out memory pressure.

## Investigation steps

1. Confirm the UID, window and pack coverage. Record which required and optional sources are available, partial or unavailable.
2. Determine the container and termination context. Separate OOM from non-memory restarts before comparing memory series.
3. Compare working set and limit over the same window and step. Note gaps and never connect across missing points.
4. Compare restart timing with rollout, load and incident activity from the timeline. Treat alignment as correlation only.
5. Check whether the evidence is sufficient to distinguish leak growth, a low limit, a traffic/load change, or an application allocation pattern. If not, record the unresolved hypotheses.
6. If the evidence supports a bounded memory-profile review, continue with [Review a bounded memory profile change](set-memory-profile.md). Do not choose requests or limits from this guide.

## Missing and unverified cases

- A memory limit may be absent or represented by a source that is not configured; absence is not an unlimited-memory proof.
- Metric samples may be gapped, stale, non-finite or capped. These states do not become numerical zero.
- The evidence boundary does not include Secret contents, arbitrary environment values, node allocatable memory or every cluster-level pressure source.
- A restart counter can include restarts from multiple causes. Do not attribute every restart to OOM.
- The current product has no memory-profile execution API. Any proposed profile is methodology for a future authorized path, not an available action.

## Manual continuation and abstention

Continue manually when the evidence shows memory pressure but not its cause, when a container's termination reason is ambiguous, or when required memory series are not usable. The operator should inspect fresh authorized observations and record the exact UID/time.

Abstain from claiming a leak, a required limit, or a guaranteed fix when the evidence is partial, stale or contradictory. Do not lower memory after a recent OOM as an automatic response. Do not request Secret contents or arbitrary pod access to fill the gap. Any memory or rollout write requires the owning recovery path's fresh eligibility, explicit review and separate verification.

## Related guidance

- [Scoped Prometheus metric evidence](../docs/features/investigations/prometheus-metrics.md)
- [Review a bounded memory profile change](set-memory-profile.md)
- [Review bounded Deployment scaling](scale-deployment.md)
