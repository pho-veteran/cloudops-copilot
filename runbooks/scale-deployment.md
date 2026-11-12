# Review bounded Deployment scaling

## Current support

The foundation supports one recovery template, `scale_deployment`, through an exact Operator proposal and decision path. The proposed replica count is strict, bounded to 1–3, and the target must be a live configured Deployment. This is a bounded foundation capability, not a universal policy for every cluster or the E07 release-complete recovery engine.

A guide match is not a scale decision. Scale is not a remedy for Pending or ImagePull families in this release.

## Prerequisites

- An exact active Deployment UID and an incident context that remains open.
- A current live observation of the target's replica count, resource version and controller state.
- A supported topology and declared capacity and quota sufficient for the proposed change.
- A check that HPA/VPA or another controller does not conflict with manual scaling.
- Fresh target authority and no active, blocking or unclassified execution on the same workload.
- The bounded replica policy, currently 1–3, from the actual implementation.

See [scoped Kubernetes investigation evidence](../docs/kubernetes-evidence.md) and [healthy baseline preparation](../docs/workload-baselines.md).

## Evidence and eligibility gates

The proposal path checks the live Deployment, controller ownership and capacity before creating an exact plan. A permission failure does not mean a controller is absent; it means the check is unverified. Lab capacity from a single node must not be presented as production capacity.

The evidence boundary does not collect every node, quota, scheduler, dependency or HPA/VPA source. If the stored evidence cannot support capacity and controller eligibility, abstain or hand off. A traffic or latency increase alone is not sufficient reason to scale; read [Investigate traffic, latency and error increases](traffic-latency-errors.md).

## Review and approval

Before approval, read the exact plan's target, version, digest, before-state, proposed replicas, rationale, expiry and verification/rollback statement. Confirm the live target fields and authority epoch still match. An Operator makes the explicit approval decision; reading this runbook or opening the plan is not approval.

A target change, expired plan, changed authority or controller conflict requires a fresh proposal and review. Do not replay an earlier approval or bypass a locked/unknown target.

## Verification and rollback limits

A Kubernetes acknowledgement confirms only that the API accepted the write. Verify readiness, desired/ready replicas, rollout status, traffic/errors and restarts under the recorded load conditions. A healthy-looking replica count does not prove application recovery.

The foundation does not claim automatic rollback. Returning to an earlier replica count is another separately reviewed change and may not be safe under current capacity or load. Unknown execution outcomes retain their lock/quarantine; do not reset or unlock them from this guide.

## Manual handoff

On target changed, expired plan, blocked authority, active execution, controller conflict, unverified capacity or unsupported topology, hand off to the workload owner. Record the exact UID, plan/version/digest if one existed, observed before-state and missing checks.

Do not provide a patch, manifest or shell command. Do not scale Pending/ImagePull workloads through this template, and do not treat the guide as authorization to write. The executor's target authority, controller check and final write fence remain authoritative.

## References

- [Scoped Kubernetes investigation evidence](../docs/kubernetes-evidence.md)
- [Investigate traffic, latency and error increases](traffic-latency-errors.md)
- [Trusted job dispatch and recovery administration](../docs/job-dispatch.md)
- [Foundation operation and contributor handoff](../docs/foundation.md)
