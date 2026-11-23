# E10 S5 release backup and recovery qualification (#124)

This ledger records one qualification run of the [release backup and restore](../../release-restore.md) procedure for [#124](https://github.com/pho-veteran/cloudops-copilot/issues/124) under [E10 #13](https://github.com/pho-veteran/cloudops-copilot/issues/13).

**Environment.** The run used two disposable projects on a Docker Desktop 29.9.2 Linux engine with a WSL2 Ubuntu client:

| Project | Topology | Time (2026-11-23, UTC) |
| --- | --- | --- |
| `cloudops-release-restore` | Writer topology | 14:06:57Z–14:14:41Z |
| `cloudops-release-core-restore` | Core-only, no executor | Immediately afterwards |

Every application, executor, cleanup and check connection used only that project's `cloudops_restore_test` database.

**Out of scope.** This was not an EC2 deployment. The second-operator restore belongs to [#126](https://github.com/pho-veteran/cloudops-copilot/issues/126).

## Identity

| Item | Value |
| --- | --- |
| Candidate and bundle revision | `37ddddb508ad49aca943d544247f8c48f9f25adb`. Built with `scripts/release-bundle.py` and installed from `source.tar` and `images.tar` after `verify --loaded` |
| Bundle SHA-256 | `source.tar` `c21a2323…bf2044b5679`; `images.tar` `a7e5dafa…bd08c848c3276b` |
| Image IDs | backend `sha256:dcd66382…86a99b57`; https `sha256:ba39f22a…5941f8f9d`; db `sha256:837bed18…fc03af3b` |
| Writer topology | `compose.yaml`, `compose.https.yaml`, `compose.integration.yaml`, `compose.release.yaml`, `compose.release-lab.yaml`, `compose.restore-admin.yaml`, `compose.release-restore.yaml`. K3s `v1.37.1-k3s1`, pinned by digest |
| Core-only topology | `compose.yaml`, `compose.https.yaml`, `compose.release.yaml`, `compose.release-core-restore.yaml` |
| Database | PostgreSQL 17.12, migration head `0020_operations_quota_wait_index` |
| Writer targets | `checkout` `840bde43-…ca923b64537b`, which has records; `checkout-standby` `eaa0d6d3-…c67448866916`, configured with no execution history |
| External authorities | Executor deployment `fda9dbf3-62f6-44ff-99ff-1353e1f43a12`, pin SHA-256 `9768f965…f0f5d19d43`; host permit authority `35d9c444-b7f3-4495-824c-a35ca5bc6f6b` |

## Cutoffs

Each cutoff is a `pg_dump -Fc` of the running database with a manifest, `ReleaseRestoreManifest/v1` (`manifest-dN.json`). The manifest records:

- the transaction and time before and after the dump, and the dump SHA-256;
- the PostgreSQL version and migration head;
- the bundle identity;
- table and identifier digests, and per-row digests for incidents, activity, runs, evidence, plans, executions and jobs;
- evidence SHA-256 values;
- permit, journal and maintenance target state;
- configured targets, scope digests and Deployment state.

The driver refuses a cutoff whose non-volatile table digests changed while the dump ran.

| Cutoff | State | Transactions around the dump | Dump | Journal attempts |
| --- | --- | --- | --- | --- |
| D1 | Scale plan proposed, not approved | 804–807 | `017ca729…` (125,856 B) | none |
| D2 | Plan approved, execution `queued`, worker stopped | 818–819 | `3b5346ec…` (126,712 B) | none |
| D3 | Execution dispatched and acknowledged by Kubernetes, `checkout` 1 → 2 replicas | 843–845 | `23f15d13…` (127,012 B) | 1 acknowledged |
| Core | Core-only project with an incident, a note and a run | 763–766 | `127b41e9…` (120,446 B) | no executor |

After D3, the run added a post-cutoff incident, notes and a completed investigation, then recorded `live.json`.

Restores ran in the order D1, D3, D2. D2 went last because the journal is ahead of the database in that case. The journal and pin were never backed up or restored.

## Acceptance

| #124 requirement | Result | Receipt |
| --- | --- | --- |
| Manifest of cutoff, database/migration identity, retained journal and deployment/scope | Recorded for each cutoff. The pin digest and deployment were identical across all three. | `manifest-d1/d2/d3.json`, `core/manifest-core.json` |
| Each backup restores retained identifiers, evidence bytes and histories to its cutoff | After cleanup, 41 (D1), 41 (D3), 38 (D2) and 42 (core) of 45 tables are byte-identical to their manifest. These include incidents, incident activity, runs and evidence, and the evidence SHA-256 values are equal. Cleanup-owned tables keep every identifier and are labelled with the cleanup that changed them; their content is not compared. Sessions, review receipts and heartbeats are not compared at all. No table changed unexpectedly or lost an identifier. Per-row digests show which rows cleanup changed: the invalidated plan in D1, and the retired plan, job and execution in D2. | `restore-dN.json` and `core/core-restore.json` → `tables`, `rows`, `evidence_bytes_equal` |
| Post-cutoff loss made explicit | Each restore lists every row inserted, changed or deleted after the cutoff (`lost_after_cutoff`, `changed_after_cutoff`, `deleted_after_cutoff`). Every restore loses the post-cutoff incident and its run, 12 evidence rows, and the post-cutoff activity. D1 also loses the later execution and the plan's later approval. D2 also loses its execution's later dispatch and acknowledgement, and its job's completion. | `restore-dN.json` → `rows` |
| Start with product and writer dispatch disabled, independent of restored rows | Before each restore, the host permit entered recovery with every class denied. `maintenance enter_recovery` reported `applied` for every configured target; the driver stops if it does not. After each restore, every target was `enabled=false, recovery_mode=true`. This includes D1, which was taken before any execution existed. | `restore-dN.json` → `maintenance_enter`, `permit_after`, `targets_after` |
| Revoke restored sessions, invalidate old approvals/work, retain unknowns and locks | `restore-admin` and the core-only cleanup reported their counts. A session cookie captured at each cutoff returned `401`. Approving a restored plan returned `409 plan_unavailable`. In D2, the queued execution became `outcome_unknown` and kept its target lock, so a primary proposal returned `409 target_locked`. Trusted reconciliation later released the lock; the outcome stayed `outcome_unknown` with `maintenance_released=true`. | `restore-d2.json` → `cleanup`, `executions`; `reconcile-after-d2.json` → `after.executions` |
| Fence old worker and late requests | **Old worker:** the worker from before the restore ran against the restored database (`running`, heartbeat advanced). The permit was in recovery, and `jobs` and `job_attempts` stayed byte-identical. **Stale dispatches** sent with the worker's own key: a retired epoch returned `409 target_quarantined`, both while quarantined and after enablement; reusing the restored execution identity with changed content returned `409 dispatch_conflict`. **Late writer:** a real admitted scale patch was held in the executor's transport. The target was then fenced through `release_uncertain_target` and stayed disabled until an explicit `enable_target`. When the held patch was finally sent, Kubernetes rejected it (`target_changed`) and replicas stayed 1. The attempt stayed `outcome_unknown` in the journal, linked to its exclusion request. A delayed worker result did not relabel the application execution. | `restore-dN.json` → `old_worker`, `late_requests`; `fresh-path.json`; `late-writer.json` |
| Restart and replay add no writes | With the API stopped, the run restarted the executor, replayed the same `enter_recovery` request ID (`replayed: true`) and ran `restore-admin` again. The rerun deleted only the sessions and review receipt created by the checks. Deployment UID, generation, replicas and fence annotations did not change, and neither did the journal attempt list. | `restore-dN.json` → `maintenance_replay`, `cleanup_replay`, `no_write` |
| Quarantined targets readable and investigable, with no new executable plan or approval, including the zero-row UID | Viewer reads of both workloads, incidents and the run returned `200`. After `resume --class collection` only, an investigation of `checkout-standby` collected Kubernetes state, events and logs. Proposals on both targets returned `409` (`target_quarantined`, or `target_locked` for the restored D2 lock). A plan proposed before quarantine got `409 target_quarantined` when approved. | `restore-dN.json` → `reads`, `quarantined_investigation`, `new_proposals_while_quarantined`; `manual-exit.json` → `approval_under_quarantine` |
| Missing authority or late-write proof keeps the target quarantined | `checkout-standby` was scaled to zero, a deliberate out-of-protocol lab action. Its `enable_target` was then denied with `maintenance_state_changed`, and the target stayed `enabled=false, recovery_mode=true`. In the late-writer drill, the target with an unresolved admitted write stayed disabled until a fence proof excluded that write. Without the executor pin, nothing could be enabled (see the manual exit row). | `reconcile-after-d2.json` → `results.proof_unavailable`; `late-writer.json`; `manual-exit.json` |
| Ordinary roles cannot enable | No API route in the generated OpenAPI matches maintenance, enable or recovery-mode operations. The maintenance CLI inside the API container reports the channel as unavailable. Enablement went only through the root peer-credential socket. | `restore-dN.json` → `maintenance_surface` |
| Reconcile, then attest enablement and recovery exit through the trusted CLI | The run first recorded the current Deployment, journal and execution state. Then it ran `enable_target` with fence and health proof for each target, followed by `exit_recovery_mode` with every target. For each request, the executor audit shows the operation, reason, evidence reference, status and root actor (UID 0, peer PID, deployment ID). The host permit returned to `normal` (generation 8) because the saved prior state was normal. | `reconcile-after-d2.json` → `results`, `after.audit` |
| One fresh permitted path | A new proposal was reviewed and explicitly approved (`200`). It produced exactly one new execution, `458ac0ab-…`, acknowledged as `checkout` 2 → 1, and one new journal attempt. No restored plan ran. | `fresh-path.json` |
| Missing authority keeps quarantine; honest manual exit | With dispatch and targets closed, the run removed the executor pin volume. The executor exited with code 1 and logged "Pinned executor authority is missing or changed". No maintenance channel existed. A proposal returned `503 executor_unavailable`, reads stayed `200`, and no Deployment was written. The receipt records the manual handoff. | `manual-exit.json` |
| Core-only restore | On the release package without an executor, the run did the following: entered recovery, restored the dump, migrated, ran `revoke-all-sessions` and `diagnosis_recovery` through the API service, and returned dispatch to its saved state. The cutoff cookie returned `401` and reads returned `200`. A plan proposal was refused (`409 live_target_required`, executor `unconfigured`). The old worker claimed nothing during recovery, and a new investigation ran after recovery exit. | `core/core-restore.json` |
| Owned targets; evidence and volumes outside destructive test databases | Only each project's own `cloudops_restore_test` was restored. The drivers check each running service's connection and every rendered profile. They refuse projects they did not prepare, as well as default or retained projects. Dumps and cookies stayed in an ignored private directory and were deleted with the projects. | `prepare.json`, `core/core-restore.json` → `connections_use_restore_database`; `run.json` |

## Remaining limits

- **Scope of the lab.** The run used a single-node K3s lab with fixture Deployments and a writer limited to two named targets. The supported release is a single host, so a multi-node scheduling or controller conflict is outside this proof. The fence proof is the existing E07 `resource_version_fence_v1`.
- **Application verification.** v1 scale writes still report application verification as `inconclusive`. Qualified verification profiles belong to E07 and the E10 live campaign (#125), not to this restore procedure.
- **What the backup covers.** The backup covers PostgreSQL only, by design (EPIC D4). The external journal, pin and permit stay on the host. Losing them leads to the manual exit, never to reinitialization.
- **Secret scan.** All receipts were scanned for the private passwords, the database password, reader/writer tokens, the dispatch, KRR and intake keys, session cookies, and credential and local-path patterns. There were no hits.
