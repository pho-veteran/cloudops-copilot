# Investigate Pending and unschedulable Pods

## Applicability

Use this guide when a Deployment's owned Pods remain Pending or report an unschedulable condition. This is a manual-only diagnosis family: the current release corpus does not map a recovery template to Pending.

Pending is a scheduling observation, not proof of insufficient CPU, memory, quota, node capacity, taints, affinity or storage. The guide applies to the Run's exact UID and captured window.

## Evidence to inspect

- Inspect `kubernetes.state` for Pod phase and Pod-level status conditions (including `PodScheduled` and `Ready`), plus the Deployment, ReplicaSet and Pod UIDs present in the state/revision summaries. The stored state does not include per-container readiness flags or Pod owner references.
- Inspect scoped `kubernetes.events` for `FailedScheduling` or related messages, involved object UID, reason, count and timestamp. A successful empty events result means no matching retained events were returned.
- Inspect declared requests/limits when the state payload includes them. Declared requests are not measured node capacity.
- Check whether a container actually started. If not, current/previous logs may be `not_applicable`; they are not a hidden success.
- Inspect workload observations and timeline for rollout/readiness context, but do not infer node capacity from them.

See [scoped Kubernetes investigation evidence](../docs/features/investigations/kubernetes-evidence.md).

## Supporting and contradicting observations

Supporting observations include an unschedulable condition with an event that names a specific constraint, and consistency between the Pod's requested resources and the reported scheduling reason. A repeated `FailedScheduling` event with the same reason strengthens the lead.

Contradicting observations include a Pending Pod whose event shows a different constraint, a scheduling event for an unrelated UID, or a Pod that was later scheduled after the captured window. A single Pending Pod does not prove a cluster-wide shortage. Permission errors or missing event coverage mean a constraint is unverified, not absent.

## Investigation steps

1. Confirm the exact Deployment UID and owned Pod UIDs. Reject unrelated Pods and same-name replacements.
2. Read the Pod's phase, conditions and scheduling-related events. Record the event UID/time and whether it is inside the captured window.
3. Compare the named scheduling constraint with declared requests, affinity, taints/tolerations and volume context only where those fields are present. Do not invent cluster capacity.
4. Determine whether the container ever ran. Use `not_applicable` for logs that cannot exist; do not treat missing logs as application success.
5. Separate capacity, quota, placement and storage hypotheses. If the stored evidence does not identify the constraint, mark it unverified and request the relevant owner check.
6. End with explicit gaps and the next authorized manual checks. Do not propose scale, memory-profile or template restore as an executable action for this family.

## Missing and unverified cases

- Node allocatable capacity, scheduler profiles, resource quotas and limit ranges may not be present in the Run evidence.
- Scheduling events are retained and bounded; absence of an event is not proof that scheduling was unconstrained.
- Container logs cannot exist before a container starts. A `not_applicable` log outcome is not a healthy application signal.
- A denied or unavailable scheduling read is “not checked”, not “no issue found”.
- The current recovery surface does not make this family executable. No automatic scheduling repair is promised.

## Manual continuation and abstention

Continue through the authorized cluster/operations channel to inspect node capacity, quotas, taints, affinity and storage constraints, using the exact UID and a fresh observation. Record the command's read-only nature and the original language of any quoted event.

Abstain from a capacity or quota conclusion when evidence is missing or partial, and abstain from any write or recovery action. Do not use this guide to justify a scale, memory or restore operation. If a scheduling fix requires a change, the owning operational process must evaluate eligibility, approval and verification outside this runbook.

## Related guidance

- [Scoped Kubernetes investigation evidence](../docs/features/investigations/kubernetes-evidence.md)
- [Investigate image pull failures](image-pull.md)
- [Review bounded Deployment scaling](scale-deployment.md) — read the limitations; scaling is not a Pending remedy in this release.
