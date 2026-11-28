# Review a bounded memory profile change

## Current support

No memory-profile execution API exists at the reviewed baseline. There is no server-defined profile ID, no endpoint, and no automatic request/limit selection in this release. This guide is methodology for a future owner-controlled path; it is not an action that the application can currently execute.

A memory-profile change is not a universal remedy for OOM or repeated restarts. The guide must be used after the [OOM investigation](oom-restarts.md), with fresh evidence and explicit ownership.

## Prerequisites

- A terminal or repeated-restart context consistent with memory pressure, not just a single restart.
- Reliable memory working-set/limit series in the same UID and window, with gaps and staleness visible.
- Current declared requests/limits and controller ownership.
- A declared, server-defined bounded profile from the owning policy. The runbook must not invent a profile ID or numeric value.
- Checked capacity/quota and a controller that can accept the change without conflicting with HPA/VPA or another controller.
- An exact target identity and fresh evidence; a same-name replacement is a different target.

See [scoped Prometheus metric evidence](../docs/features/investigations/prometheus-metrics.md) and [healthy baseline preparation](../docs/features/workloads/baselines.md).

## Evidence and eligibility gates

Do not lower memory after a recent OOM merely to reduce usage, and do not increase a limit as a claimed fix for an application leak. The evidence must distinguish memory pressure from probe, scheduling, image and application-exit failures.

The profile's request and limit must come from a trusted bounded declaration, not from free text or a runbook table. The current target authority, controller compatibility and capacity must be checked at decision time. Missing profile policy, missing metrics, stale observations or an unsupported controller configuration means abstain or manual handoff.

## Review and approval

A future path must present the exact target, current observed values, proposed bounded profile, expected rollout impact, rollback limitations and expiry. An Operator reviews and approves the exact plan separately. A runbook hit, a diagnosis guess or an earlier approval is not sufficient.

A change to the target, profile policy, authority epoch or controller state invalidates the review. Do not replay a prior approval against a different observation.

## Verification and rollback limits

Verification must observe rollout/readiness, restarts and memory behavior under recorded load conditions. A successful apply acknowledgement does not prove the OOM cause was resolved or that the new limit is safe.

Rollback to an older profile also requires fresh eligibility and review of the old value's safety. The foundation does not promise an automatic inverse write, and unknown execution outcomes retain their lock/quarantine. Do not unlock or quarantine-reset from this guide.

## Manual handoff

When no trusted profile exists, metrics are incomplete, capacity/quota is unknown or the controller conflicts, hand off to the owner. Record the exact UID/window, observed memory series, current requests/limits, missing policy and the reason the change is not eligible.

Do not supply a patch, manifest, arbitrary resource quantity or executable endpoint. Continue investigation and use the current scale/memory support boundaries explicitly; a runbook cannot make an unimplemented capability usable.

## References

- [Investigate OOM and repeated restarts](oom-restarts.md)
- [Scoped Prometheus metric evidence](../docs/features/investigations/prometheus-metrics.md)
- [Healthy baseline preparation](../docs/features/workloads/baselines.md)
- [Trusted job dispatch and recovery administration](../docs/features/investigations/job-dispatch.md)
