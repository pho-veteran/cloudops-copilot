# Investigate CrashLoop and startup failures after a change

## Applicability

Use this guide when a Deployment's Pods repeatedly restart, remain in a startup or crash loop, or stop becoming Ready after a template or image change. The guide applies to the Deployment's current immutable workload UID. A same-name Deployment with a different UID is a replacement, not a continuation of this incident.

This is investigation methodology. A CrashLoop symptom does not identify a cause, and a guide match is not a diagnosis.

## Evidence to inspect

- Read the stored Run's exact UID, captured window, pack coverage and source coverage before interpreting any symptom.
- Inspect `kubernetes.state` for the Deployment revision, owned ReplicaSet and Pod identities, container states, restart counts and terminated context.
- Inspect `kubernetes.events` for scheduling, image, probe, restart and container lifecycle events. A successful empty events response means no matching retained events were returned; it does not prove historical absence.
- Inspect `kubernetes.logs.current` and `kubernetes.logs.previous` for regular and init containers when those sources are applicable. A never-started container may make current logs `not_applicable`; a restarted container with missing previous logs is an explicit gap.
- Use `kubernetes.configmap.metadata` only when it is configured and only for reference identity. ConfigMap contents and Secret values are outside this evidence boundary.

See [scoped Kubernetes investigation evidence](../docs/kubernetes-evidence.md) for the source and retention contract.

## Supporting and contradicting observations

Supporting observations for a startup/application failure include a new revision or template change near onset, a container repeatedly exiting with the same termination context, probe failures tied to startup, and logs that reach the same failing initialization step. The revision time is correlation; it is not proof that the change caused the failure.

Contradicting observations include healthy behavior on the same revision, a restart pattern that began before the change, a different failing container or init container, or evidence that the container never started. Check whether `stale`, `timeout`, `forbidden`, `truncated` or `identity_changed` changes what the evidence can support. Missing or partial required sources prevent a confident ruled-out claim.

## Investigation steps

1. Confirm the workload UID and immutable window. Stop if the citation resolves to a different UID or a replacement Deployment.
2. Compare the observed revision and template fields with the onset time. Record the direction and strength of the change rather than assuming causation.
3. Identify the failing container or init container and its state. Separate image-pull, scheduling and Pending symptoms; those have their own guides.
4. Read current logs before previous logs when both are available. Preserve quoted human text in its original language and do not add unredacted secrets.
5. Correlate exit context, probe-related events and restart timing. If the evidence shows only a restart symptom, state that the application failure mode remains unverified.
6. If the evidence points to a supported known-good template difference, continue with [Review a known-good Deployment template restore](restore-deployment-template.md). Do not roll back merely because an older ReplicaSet exists.

## Missing and unverified cases

- Previous-container logs may be unavailable on the local runtime or may be truncated. Record the gap; the absence of a previous log does not exclude an application failure.
- A log segment that extends after the captured window is not a closed historical record. Do not infer behavior after the window as if it were captured evidence.
- Events are retained and bounded. A successful empty response is not evidence of what was never retained.
- The collector does not read Secret contents, arbitrary environment literals, ConfigMap data or injector state. Do not request those through this guide.
- A revision change and a restart can be concurrent with an external dependency failure. Without dependency evidence, keep both hypotheses open.

## Manual continuation and abstention

Continue manually when the stored evidence cannot distinguish startup failure from probe, dependency, scheduling or replacement causes. The operator should check the deployment controller and current cluster state through the authorized operational channel, verify the exact UID, and record what was and was not observed.

Abstain from a root-cause statement, a ruled-out cause, or an automatic recovery when required sources are partial/unavailable, the identity changed, or the evidence window does not cover the onset. A runbook recommendation is not approval to write to the cluster. Recovery remains an explicit E07 decision with its own eligibility, review and verification.

## Related guidance

- [Scoped Kubernetes investigation evidence](../docs/kubernetes-evidence.md)
- [Review a known-good Deployment template restore](restore-deployment-template.md)
- [Review bounded Deployment scaling](scale-deployment.md)
