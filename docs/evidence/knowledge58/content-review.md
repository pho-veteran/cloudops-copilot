# E05 #58 corpus content review

- Reviewed source revision: `6c8eea37e982f3dc6de2755e7a482eb16aaf1f16`
- Source revision exists in this repository as a commit; review date: `2026-11-11`
- Release corpus: eight documents; OOM and traffic guidance are version `"2"`, the other six guides remain version `"1"`
- Review scope: source/corpus review by the implementer; no independent human approval is claimed
- Runtime claims: none. This review covers repository guidance and an author-side validator, not indexing, retrieval, UI or recovery execution.

## Coverage

| Guide | Kind / family | Evidence and counter-evidence coverage | Gap/manual handling | Current-support review |
|---|---|---|---|---|
| [crashloop-startup](../../../runbooks/crashloop-startup.md) | diagnosis / `crashloop_startup` | State/revision/Pod/container context, events, current/previous logs; rollout correlation is not causation; restart/probe/application/dependency hypotheses stay distinct | Previous logs/truncation/timeout/identity change are gaps; probe configuration is not stored; no Secret/config contents; abstain without sufficient coverage | Links to baseline/restore methodology only when a supported baseline diff exists; no rollback instruction |
| [oom-restarts](../../../runbooks/oom-restarts.md) | diagnosis / `oom_restarts` | Termination reason/exit context, restarts, working set/limit/restart metric slots; missing samples are not zero; OOM is not automatically a leak | Gaps/stale/non-finite/ownership and absent node/dependency sources are explicit; no Secret reads | Memory profile is methodology only; no profile API is implied; no automatic limit change |
| [traffic-latency-errors](../../../runbooks/traffic-latency-errors.md) | diagnosis / `traffic_latency_errors` | Request rate/error ratio/p95 plus supporting memory/restarts/state/events/timeline; correlation is not causality; missing samples are not zero | Denominator/partial/stale/ownership/CPU/dependency gaps remain manual; timeline is bounded | Scaling only after exact evidence/controller/capacity review; no metric spike equals approval |
| [pending-unschedulable](../../../runbooks/pending-unschedulable.md) | diagnosis / `pending_unschedulable` | Pod phase/Pod-level conditions/scheduling events/declared requests; Pending does not prove capacity/quota/placement/storage; `not_applicable` logs are not success | Node/quota/taint/affinity/storage checks are manual; per-container readiness and owner references are not stored; denied reads are not checked; manual-only | No recovery template mapping; scaling is not presented as a Pending remedy |
| [image-pull](../../../runbooks/image-pull.md) | diagnosis / `image_pull` | Waiting reason/sanitized image reference/events; tag is not digest; logs only if a container ran | Registry/auth/rate-limit/policy checks are manual; no `imagePullSecrets`/Secret decode; manual-only | No ImagePull recovery template or automatic image policy change |
| [restore-deployment-template](../../../runbooks/restore-deployment-template.md) | recovery_template / `restore_deployment_template` | Exact UID, confirmed healthy baseline, supported image/resource envelope and digest; current healthy state does not prove historical health | External ConfigMap/Secret contents, mutable tags, unsupported fields and reference mismatch are manual; execution unavailable (`restore_not_implemented`) | Baseline context only; no patch/endpoint; approval and fresh eligibility are future/owning gates |
| [set-memory-profile](../../../runbooks/set-memory-profile.md) | recovery_template / `set_memory_profile` | OOM/restart context plus trustworthy memory, current requests/limits, controller and capacity checks | No profile ID/API/value is invented; missing profile/capacity/fresh evidence is manual | Explicitly unimplemented; no executable profile or automatic lowering after OOM |
| [scale-deployment](../../../runbooks/scale-deployment.md) | recovery_template / `scale_deployment` | Exact target, current replica/resourceVersion, controller, bounds 1–3, capacity/quota, exact plan digest/version/expiry | Unverified capacity/controller/topology is manual; Pending/ImagePull are excluded; no patch/command | Foundation scale path only; acknowledgement is not recovery; no automatic rollback or unlock |

## Cross-guide review

- All five diagnosis families reuse the E04 family IDs and name the real stored source keys; the five pack coverage outcomes are not treated as a diagnosis.
- Missing, partial, stale, truncated, `not_applicable`, ownership and identity-change states remain visible and are never converted to zero or a ruled-out cause.
- OOM and traffic v2 explicitly separate absent metric series from contradictory observations; missing telemetry cannot support or refute a hypothesis. A competing explanation is scoped to the observed restart/container or request interval rather than ruling out unrelated failures.
- Pending and ImagePull have no recovery template mapping. The release checker enforces that mapping.
- Restore guidance links [healthy baseline preparation](../../features/workloads/baselines.md) and reports the current `restore_not_implemented` boundary; it does not instruct an external configuration rollback or unhealthy baseline confirmation.
- Memory guidance records that no profile execution API exists at the reviewed source. Scaling guidance records the actual bounded `scale_deployment` contract (`replicas` 1–3, exact plan, approval, fresh eligibility and controller/capacity checks).
- No guide includes a writable patch, shell command, raw credential, Secret content, external image or HTML execution path. Local links are checked by the corpus validator.

## Review limitations

This is a self-review by the implementation author. The validator proves contract/path/coverage/byte behavior, not that the prose is operationally ideal or that an independent reviewer approved it. No live diagnosis, retrieval, index activation, UI, E07 execution or E10 restore qualification was performed or claimed. Index/retrieval/UI/relevance remain with #59, #60, #61 and #62.
