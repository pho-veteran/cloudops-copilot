# Qualified recovery verification

Recovery verification is opt-in. Without a deployment-qualified profile, existing manual scale retains its acknowledgement-only outcome and advice previews remain unavailable for approval. A fresh profile-enabled plan captures its exact policy, source binding, desired state, image digests and verification profile in the immutable content digest. An older unavailable preview never gains authority when configuration changes.

The worker never asks a model to approve parameters, verify a change or select an inverse. Attribution, verification and permission for future work remain separate in `RecoveryOutcome/v1`. A Kubernetes ACK alone cannot establish application recovery.

## Trusted configuration

Keep the following files outside the source tree and mount them read-only into the API and worker:

- `RECOVERY_POLICY_FILE`: the existing `RecoveryPolicy/v1` resource/image/UID policy.
- `RECOVERY_VERIFICATION_FILE`: a JSON array of `RecoveryVerificationProfile/v1` profiles, at most 64 KiB. The OpenAPI schema defines the closed fields and bounds.
- `KUBE_READ_CONFIG` and `PROMETHEUS_READ_CONFIG`: existing fixed-destination TLS read configurations. Never give these processes the gateway's Kubernetes writer or maintenance socket. Only the worker receives the gateway dispatch credential.

Each profile identifies one original workload UID, template, exact desired regular-container images/resources, allowed absolute replicas, immutable runtime `image_ids`, qualified sample/source version, qualification reference and source digest. Its `id` must match `RecoveryPolicy.verification_policy`. The source digest binds the Kubernetes reader server/CA and Prometheus endpoint, producer, TLS CA, label binding and step; it excludes bearer tokens. A credential renewal can preserve this binding, while an endpoint/CA/profile change requires a newly qualified profile and a new plan. `GET /api/recovery-policy` reports current qualified profiles without contacting either provider.

Qualification is a deployment-operator responsibility. Missing, malformed, duplicate-applicable, stale-source or mismatched profiles cannot enable a v2 approval or produce passed verification. There is no default production traffic floor, memory pair or image. Qualify the exact source/metric semantics and workload scenario before enabling a profile.

The eight required query identities belong to recovery, separately from the six-query diagnosis catalog:

| Query | Required signal |
| --- | --- |
| `request_rate` | Actual served request rate for the original UID/namespace |
| `error_ratio` | Request-derived error ratio |
| `p95_latency` | Request-derived latency histogram percentile |
| `requests` | Monotonic completed-request counter for each `pod_uid` |
| `sample_time` | Actual scrape timestamp of each corresponding counter |
| `resets` | Counter-reset count for each corresponding Pod |
| `offered_rate` | Independently observed attempted/ingress request rate, including attempts while the application is unavailable |
| `offered_sample_time` | Actual scrape timestamp of that offered-request counter |

Queries must bind `{{namespace}}` and `{{workload_uid}}`. The shared Prometheus transport keeps its HTTPS validation, no proxy/redirect behavior, four-second request bound, 2 MiB response cap and 25-series cap. Recovery retains `pod_uid` for counter continuity; normal diagnosis payloads and query identities are unchanged. Do not use a query that hides stale/replaced Pod series or fabricates zero traffic. The offered-load source must remain independent of application availability.

## Fixed observation and outcomes

Before an initial dispatch, the worker reserves and records one fresh 60-second pre-change application window, UID-owned rollout and failure evidence. Adequate offered traffic and an established symptom are required. An already healthy qualified target becomes `no_op` without a gateway call. An interrupted baseline reservation is not repeated against a new window; prepare a new plan.

An exact typed ACK starts the fixed clock: 180 seconds for scale, 300 seconds for restore/memory, with a recorded 60-second settling interval and 30-second slots. These are explicit profile candidates requiring measured qualification, not general service-level objectives. Scoring denominators are five scale or nine restore/memory observations; the minimum coverage bound is 90%, with integer rounding and all required identity/freshness/continuity checks still enforced. Missing data is never zero-filled.

Every slot is reserved before external reads. The ACK anchor, deadline, next slot, sample identity and maximum Job attempts are persisted. Killing a worker can leave a missing slot; takeover cannot resend the functional operation, repeat that slot, extend the window or inflate coverage. Actual observations and bounded errors/read budgets live in `recovery_verifications`, separately from original Runs, evidence and saved handoffs. Authenticated Operators and Viewers can read `GET /api/executions/{execution_id}/verification`; a legacy execution with no such record returns `verification_not_found`.

The evaluator checks original UID/desired fields, complete UID-owned Pod/ReplicaSet rollout, immutable runtime images, restart/OOM and event deltas, actual served-request counter continuity, fresh offered traffic, minimum RPS, comparable load, error ratio and p95. Pod replacement after settling, counter reset, stale/late/wrong-window observations, missing/403 metrics, inadequate/falling traffic or insufficient coverage yield `manual_intervention` with `inconclusive`. Qualified complete observations with a failed condition yield `failed`. A pass yields `succeeded`; it remains post-change evidence rather than causal proof. Memory success explicitly means observed mitigation, never a repaired memory leak.

Event deltas retain the entire window. The explicit `after_settling/v1` condition scores new Warning occurrences after the 60-second settling boundary; expected startup probe warnings remain visible in the retained transition evidence. OOM and compatible restart deltas still cover the transition. The adapter recognizes modern singleton Events and series counts/clocks according to the [Kubernetes Event API](https://kubernetes.io/docs/reference/kubernetes-api/core/event-v1/); an unrecognized missing counter remains incomplete.

The first durably finalized verification timestamp is immutable and available to the inverse-policy consumer. Reconciliation retains an unknown write result until an exact receipt is available; matching desired fields alone cannot start qualified verification. A late known ACK retains its original clock rather than receiving a new window.

## Recovery and retention

Migration `0015_recovery_verification` adds a separate bounded observation record without rewriting legacy plan/result/report bytes. Downgrade refuses to remove retained verification evidence. Restore cleanup retires verification Jobs alongside execution/reconciliation work and finalizes an unfinished verification as inconclusive. Root gateway maintenance preserves known ACK/unknown attribution and the first verification finalization time. Neither cleanup nor an inconclusive result grants an inverse or fresh write authority.

Preserve the original host permit, gateway journal and external identity pin. Follow the stopped-application, all-classes/all-targets recovery procedure in [foundation operation](foundation.md#execution-and-reconciliation) before restoring PostgreSQL. Old approvals and sessions must not be replayed.

## Owned local sample

`config/lab/Dockerfile.recovery` reuses the existing request counter/histogram implementation. Its healthy image touches a bounded 160 MiB resident working set before readiness; its `bad-image` variant rejects startup. Two serving slots and a bounded queue create capacity-derived errors at fixed offered load. `recovery_load.py` runs 1–900 seconds at 1–30 RPS with at most 16 clients, counts actual attempts on `/metrics` port 8090, and reports response/transport/client-skip counts. It targets only the owned checkout Service through NodePort 30080. The existing triage sample and lab manager retain their own purpose.

`config/lab/recovery-reset.json` is a fixture reset for the disposable single-container `demo/checkout` sample. Stop its previous finite load first. Independently verify the owned cluster/container and Deployment owner label against the retained lab receipt, retain the original Deployment UID, read a fresh resourceVersion, and substitute the four explicit placeholders with that UID, resourceVersion, qualified healthy image and a new trial UUID. Apply it with `kubectl --kubeconfig <owned-lab-kubeconfig> -n demo patch deployment checkout --type=json --patch-file <rendered-owned-reset.json>`. Never use an implicit host context. It atomically tests UID/resourceVersion/container name, restores the qualified image/resources/one replica, and starts an independent Pod/counter cohort before the next load. It replaces only the disposable sample's trial annotations; it is not a product action or a reset for an arbitrary Deployment. Keep the rendered patch and before/after Pod identities as evidence. A reset after an action cannot be counted as that action's recovery.

Before fault/load, check actual nested Pod cgroups, the owned K3s root limit, Node allocatable/reserves, pressure conditions, existing/terminating allocations and candidate/surge demand. Use a disposable owned namespace/project and retain independent reset, image/source fingerprints, raw metrics and failed calibration trials. A Docker limit or Deployment ready counter alone is insufficient. Do not change a fault flag or lower traffic during a positive and credit the approved action.

Local scale qualification does not qualify restore/memory profiles, arbitrary image tags, production SLOs, leak repair, a full E10 release campaign or the remaining E07 rollback/UI/restore gates.

The [R4 local qualification record](evidence/e07-r4-verification.json) contains the exact sample/profile/source fingerprints, measured scale positive and missing-load control, retained failed diagnostics, containment/reset checks and stopped-lab receipt. It qualifies only its recorded local scale scenario.
