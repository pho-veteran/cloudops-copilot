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

OOM checks inspect both current and previous termination states, including owned failed Pods excluded from active rollout counts. Historical terminations do not become new failures, and an unknown/future termination clock is inconclusive. Compatible Pod/container restart counters retain their highest observed value through settling; a decrease or missing compatible counter cannot be forgotten after later healthy samples. Optional v2 extraction that cannot represent a valid manual-scale target falls back to the existing legacy manual path.

The first durably finalized verification timestamp is immutable and available to the inverse-policy consumer. Reconciliation retains an unknown write result until an exact receipt is available; matching desired fields alone cannot start qualified verification. A late known ACK retains its original clock rather than receiving a new window.

## Separately approved inverse

Inverse proposals are opt-in for newly minted forward plans. Set `inverse_policy` to `captured-old-state/v1` and explicitly configure `inverse_window_seconds: 600`; also qualify a verification profile for the exact captured old state. Restore/memory require an exact original-UID/container entry in `inverse_qualified_states`. All captured requests/limits must remain positive, request≤limit and within policy bounds; restored old images must independently occur in `qualified_images`. A captured safe memory decrease uses its recorded values without selecting another named forward memory profile. An inverse image/resource restore uses the captured values without selecting another baseline. Unsafe old values stay manual-only even when they were previously deployed.

An omitted/null inverse window remains disabled and is omitted from the canonical policy digest, preserving the pre-inverse policy fingerprint. An explicitly configured window participates in the digest.

The immutable forward content captures this inverse safety contract and old-state verification digest before approval. A rollback window is recorded only at the first durable finalization of an exact acknowledged forward operation, after its fixed verification deadline. `eligible_from` is that first finalization time; the independent deadline is the original fixed verification deadline plus 600 seconds. Delayed finalization therefore shortens, rather than renews, the available period. Early retirement, unknown ACK, missing original qualification or a changed safety/source contract cannot arm a window. No read, restart, note, maintenance cleanup or later configuration can backfill one into old history. An inconclusive result by itself grants no authority: the exact ACK, captured qualification and fresh target proof are still mandatory.

Authenticated `GET /api/executions/{id}/rollback` exposes the retained clock, current proposal availability, reason and separately linked inverse execution. An Operator submits only a rationale to `POST /api/executions/{id}/inverse-plans`. The server derives the inverse's before/desired values by exactly swapping the acknowledged request's selected states, preserves its original UID/template/configuration references and limits its new five-minute plan TTL to the original rollback deadline. Caller-supplied alternative values are rejected. The inverse has a new plan ID/digest, `inverse_rollback` origin and immutable `forward_execution_id`; it uses the existing session-bound review and explicit approve/reject endpoints. Rejection may be followed by another proposal within the same original window. Once an inverse is approved, its operation identity remains reserved: inspect or hand off a failed/unknown operation rather than issuing another inverse for that forward.

Review, decision, claim and dispatch recheck the original window, policy, UID/epoch, exact fields, controllers and full candidate/surge capacity. Forward approval, review token or TTL cannot authorize the inverse. The gateway independently checks the durable forward journal, current inverse safety, approved linkage and fixed window without receiving observer credentials. API/worker retain the current qualified-source checks. A retired named forward memory selector alone does not change the captured inverse safety contract; a change to its resource/image bounds, qualification, policy version or observation profile does.

An inverse reserves a fresh comparable-traffic baseline, then uses the same fixed post-check protocol. It can begin from a healthy forward result: the forward remediation symptom/no-op rule does not suppress an explicit inverse. A qualified pass produces `rolled_back` on the child execution; failed or incomplete observations retain `failed` or `manual_intervention/inconclusive`. Unknown acknowledgement retains the shared read-only reconciliation budget and quarantine, with no blind resend. The original forward execution, its verification and saved handoff/export bytes stay unchanged.

Migration `0016_recovery_inverse` adds nullable plan linkage and a separate rollback-window record. It does not synthesize availability for legacy or previously finalized plans and refuses downgrade when inverse authority/linkage is retained. Restore cleanup retires active inverse work through the existing execution/job fences; preserved timestamps never grant a new post-restore window.

The [R5 local inverse record](evidence/e07-r5-inverse.json) binds the actual separately approved scale inverse, independent load phases, natural plan/window expiry and unsafe-resource controls, real worker restart, unchanged history and stopped/quarantined lab. Captured memory/image selector independence uses controlled-source API/gateway cases; full three-template/model/restore composition remains the integrated E07 gate.

## Recovery and retention

Migration `0015_recovery_verification` adds a separate bounded observation record without rewriting legacy plan/result/report bytes. Downgrade refuses to remove retained verification evidence. Restore cleanup retires verification Jobs alongside execution/reconciliation work and finalizes an unfinished verification as inconclusive. Root gateway maintenance preserves known ACK/unknown attribution and the first verification finalization time. Neither cleanup nor an inconclusive result grants an inverse or fresh write authority.

Preserve the original host permit, gateway journal and external identity pin. Follow the stopped-application, all-classes/all-targets recovery procedure in [foundation operation](foundation.md#execution-and-reconciliation) before restoring PostgreSQL. Old approvals and sessions must not be replayed.

## Owned local sample

`config/lab/Dockerfile.recovery` reuses the existing request counter/histogram implementation. Its healthy image touches a bounded 160 MiB resident working set before readiness; its `bad-image` variant rejects startup. Two serving slots and a bounded queue create capacity-derived errors at fixed offered load. `recovery_load.py` runs 1–900 seconds at 1–30 RPS with at most 16 clients, counts actual attempts on `/metrics` port 8090, and reports response/transport/client-skip counts. It targets only the owned checkout Service through NodePort 30080. The existing triage sample and lab manager retain their own purpose.

`config/lab/recovery-reset.json` is a fixture reset for the disposable single-container `demo/checkout` sample. Stop its previous finite load first. Independently verify the owned cluster/container and Deployment owner label against the retained lab receipt, retain the original Deployment UID, read a fresh resourceVersion, and substitute the four explicit placeholders with that UID, resourceVersion, qualified healthy image and a new trial UUID. Apply it with `kubectl --kubeconfig <owned-lab-kubeconfig> -n demo patch deployment checkout --type=json --patch-file <rendered-owned-reset.json>`. Never use an implicit host context. It atomically tests UID/resourceVersion/container name, restores the qualified image/resources/one replica, and starts an independent Pod/counter cohort before the next load. It replaces only the disposable sample's trial annotations; it is not a product action or a reset for an arbitrary Deployment. Keep the rendered patch and before/after Pod identities as evidence. A reset after an action cannot be counted as that action's recovery.

Before fault/load, check actual nested Pod cgroups, the owned K3s root limit, Node allocatable/reserves, pressure conditions, existing/terminating allocations and candidate/surge demand. Use a disposable owned namespace/project and retain independent reset, image/source fingerprints, raw metrics and failed calibration trials. A Docker limit or Deployment ready counter alone is insufficient. Do not change a fault flag or lower traffic during a positive and credit the approved action.

Local scale qualification does not qualify restore/memory profiles, arbitrary image tags, production SLOs, leak repair, a full E10 release campaign or the remaining integrated E07 UI/restore gates.

The [R4 local qualification record](evidence/e07-r4-verification.json) contains the exact sample/profile/source fingerprints, measured scale positive and missing-load control, retained failed diagnostics, containment/reset checks and stopped-lab receipt. It qualifies only its recorded local scale scenario.
