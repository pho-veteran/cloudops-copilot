# Investigate image pull failures

## Applicability

Use this guide when a Deployment's owned Pods report an image pull waiting reason or a pull-related failure. This is a manual-only diagnosis family: no recovery template is mapped to ImagePull in this release.

An image reference in a template or event is not proof that the image exists, that the registry is reachable, or that credentials are valid. The guide applies to the Run's exact Deployment UID and captured window.

## Evidence to inspect

- Inspect `kubernetes.state` for the Pod phase, container waiting state/reason, restart count, image reference as sanitized by the collector, and owned UID chain.
- Inspect `kubernetes.events` for `Failed`, `ErrImagePull`, `ImagePullBackOff` or related messages with the involved object UID, reason, count and timestamp.
- Inspect current/previous logs only if a container actually started. Logs are not applicable when the image never ran.
- Inspect Deployment template image fields when present. This is declared metadata, not proof of registry availability or digest identity.
- Do not inspect or request `imagePullSecrets`, Secret values or registry credentials through this guide.

See [scoped Kubernetes investigation evidence](../docs/features/investigations/kubernetes-evidence.md).

## Supporting and contradicting observations

Supporting observations include a waiting reason consistent with a pull failure, events bound to the exact Pod UID, and a template image reference that changed near onset. Repeated identical events strengthen the pull hypothesis.

Contradicting observations include an image-pull event for an unrelated UID, a container that actually started, a scheduling/condition failure before image pull, or a later successful start outside the captured window. A tag string does not prove a digest, and absence of logs does not prove the image is correct.

A missing event or an unavailable source leaves registry reachability and authentication unverified. Do not state that credentials or network are healthy from this evidence.

## Investigation steps

1. Confirm the Deployment UID and owned Pod UID. Stop if the event or state belongs to a replacement.
2. Read the waiting reason and event message. Separate invalid/missing image, registry reachability, authentication/rate-limit and runtime configuration hypotheses.
3. Compare the declared image reference with revision/onset. Do not claim a digest when only a mutable tag is stored.
4. Determine whether any container started. Mark logs `not_applicable` when the image never ran.
5. Record which registry, credential, network and policy checks remain outside the evidence boundary. Hand those checks to the authorized owner through a separate channel.
6. Do not propose a write action, image mutation or credential read as part of this investigation.

## Missing and unverified cases

- `imagePullSecrets` references and Secret contents are outside the stored evidence boundary. Do not decode or request them.
- Registry authentication, rate limiting, network policy and cluster image-policy state are not collected by this Run.
- A mutable tag can move independently of the runbook and evidence; it is not a stable identity.
- Events are retained and bounded. A successful empty events response is not proof that pull failures never occurred.
- The current product has no ImagePull recovery template. A guide match is not an executable remedy.

## Manual continuation and abstention

Continue manually with the owner who can inspect the authorized registry and cluster configuration. Use the exact UID/reference, record what was checked, and preserve any human-supplied error text in its original language after redaction.

Abstain from blaming credentials, network or image policy without evidence. Do not ask for or reveal Secret contents, perform a registry login from the application, or change image policy automatically. Any remediation is a separate operational decision with its own approval and verification.

## Related guidance

- [Scoped Kubernetes investigation evidence](../docs/features/investigations/kubernetes-evidence.md)
- [Investigate Pending and unschedulable Pods](pending-unschedulable.md)
- [Investigate CrashLoop and startup failures after a change](crashloop-startup.md)
