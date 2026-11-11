# Review a known-good Deployment template restore

## Current support

The foundation can prepare and retain a confirmed healthy baseline and expose an exact read-only recovery context. It does **not** implement restore execution, and it does not roll back external ConfigMap or Secret contents. At the reviewed baseline, a supported template difference is reported as `restore_not_implemented` with `execution_eligible=false`.

This guide describes the evidence, review and limitations a future authorized restore path must preserve. It is not an executable procedure and does not create a plan or approval.

## Prerequisites

- An exact active Deployment UID, configured cluster/namespace and current incident context.
- An explicitly selected baseline record confirmed healthy before the incident. The baseline must not be created from unhealthy, partial, scaled-to-zero or identity-changed observations.
- A supported immutable image/resource snapshot with its stored digest and reference identities.
- Fresh current evidence for the same identity. A historical baseline plus a same-name replacement is insufficient.
- An owner who can inspect any external configuration references outside the supported envelope.

See [healthy baseline preparation](../docs/workload-baselines.md) for the current selection and confirmation behavior.

## Evidence and eligibility gates

The baseline envelope is `deployment-images-resources/v1`: owned identity, declared regular container image and resource strings, sanitized reference identities and a canonical digest. It excludes environment literals, commands/arguments, Secret/ConfigMap contents, arbitrary template fields and credentials.

Eligibility must verify the current UID, baseline digest, supported diff, controller ownership, capacity/quota and the current target authority. A healthy observation today does not prove the workload was healthy at an earlier time. External reference mismatch or an unsupported container-set difference remains manual-only. Do not treat a mutable image tag as an immutable restored image.

## Review and approval

A future restore path must show the exact target, selected baseline, supported diff, plan version/digest, expiry and authority state. An Operator must make an explicit, separate approval decision after reviewing the current plan. Reading this runbook, selecting a baseline or receiving a guide match is not approval.

Approvals are bound to the plan, target epoch, authority generation and expiry. A target change, expired plan, blocked authority or changed reference requires a fresh plan and review. Do not replay an earlier approval.

## Verification and rollback limits

A Kubernetes acknowledgement would only prove that a write was accepted. It would not prove readiness, traffic recovery, dependency health or application correctness. Verification must use separately observed state and metrics under recorded load conditions.

The foundation does not provide an automatic inverse restore. A rollback to a previous template also needs fresh eligibility, a new exact review and verification that the older values remain safe. External ConfigMap and Secret contents are not restored by the baseline path; an environment rollback is unverified unless a separate owner proves it.

## Manual handoff

On `restore_not_implemented`, missing baseline, unsupported diff, reference mismatch, stale identity or unavailable authority, hand off to the workload owner. Record the exact UID, baseline ID/digest, missing checks and what was observed. Continue with authorized investigation or manual repair, then capture a fresh healthy baseline after recovery.

Do not provide YAML patches, kubectl commands, arbitrary template payloads or a claim that an execution endpoint exists. Unknown execution outcomes retain their lock/quarantine; do not unlock a target from this guide.

## References

- [Healthy baseline preparation](../docs/workload-baselines.md)
- [Scoped Kubernetes investigation evidence](../docs/kubernetes-evidence.md)
- [Trusted job dispatch and recovery administration](../docs/job-dispatch.md)
- [Investigate CrashLoop and startup failures after a change](crashloop-startup.md)
