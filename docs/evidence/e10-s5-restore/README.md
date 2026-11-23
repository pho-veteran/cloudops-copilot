# E10 S5 release backup and recovery qualification (#124)

This ledger records one qualification run of the [release backup and restore](../../release-restore.md) procedure for [#124](https://github.com/pho-veteran/cloudops-copilot/issues/124) under [E10 #13](https://github.com/pho-veteran/cloudops-copilot/issues/13).

- **Environment:** a disposable project, `cloudops-release-restore`, on a Docker Desktop 29.9.2 Linux engine with a WSL2 Ubuntu client. The run lasted from 2026-11-23 12:46:45Z to 12:53:52Z.
- **Database isolation:** every application, executor, cleanup and check connection used only `cloudops_restore_test`.
- **Out of scope:** this was not an EC2 deployment, and it was not the second-operator restore, which belongs to [#126](https://github.com/pho-veteran/cloudops-copilot/issues/126).

## Identity

| Item | Value |
| --- | --- |
| Candidate and bundle revision | `08ad0ad6b25967cc57d5d3f5463264a86cfe97c1`. Built with `scripts/release-bundle.py` and installed from `source.tar` and `images.tar` after `verify --loaded` |
| Bundle SHA-256 (`source.tar` / `images.tar`) | `6ec1578a…c4550da` / `e0a0837b…a953923a` |
| Image IDs | backend `sha256:316cc54d…4fc490a2`, https `sha256:5fee2bba…ef133ddd`, db `sha256:837bed18…fc03af3b` |
| Topology | `compose.yaml`, `compose.https.yaml`, `compose.integration.yaml`, `compose.release.yaml`, `compose.release-lab.yaml`, `compose.restore-admin.yaml`, `compose.release-restore.yaml`. K3s `v1.37.1-k3s1`, pinned by digest |
| Database | PostgreSQL 17.12, migration head `0020_operations_quota_wait_index` |
| Writer targets | `checkout` `bfcdb2f0-…a0ef7884`, which has records. `checkout-standby` `c6ca9e3f-…109860`, which is configured but has no execution history |
| External authorities | Executor deployment `d8b9b717-a5a9-403a-9d2f-ff44b2a6d8e9`, pin SHA-256 `fd78887c…19e4cfb`. Host permit authority `f95a60b7-1460-445f-89b4-e621ea9d6fcc` |

## Cutoffs

Each cutoff is a `pg_dump -Fc` of the running database plus a `ReleaseRestoreManifest/v1` (`manifest-dN.json`). The manifest records:

- the transaction and time before and after the dump;
- the dump SHA-256;
- the PostgreSQL version and migration head;
- the bundle identity;
- table digests and identifier digests;
- per-row digests for incidents, activity, runs, evidence, plans, executions and jobs;
- evidence SHA-256 values;
- permit, journal and maintenance target state;
- configured targets, scope digests and Deployment state.

The driver checked that every non-volatile table digest was the same before and after each dump.

| Cutoff | State | Transactions around the dump | Dump | Journal attempts |
| --- | --- | --- | --- | --- |
| D1 | Scale plan proposed, not approved | 804–806 | `a7b82ee2…` (125,784 B) | none |
| D2 | Plan approved, execution `queued`, worker stopped | 817–818 | `4347fa2d…` (126,644 B) | none |
| D3 | Execution dispatched and acknowledged by Kubernetes; `checkout` 1 → 2 replicas | 831–844 | `6dc4f2e0…` (126,942 B) | 1 acknowledged |

After D3, the run added a post-cutoff incident, notes and a completed investigation, then recorded `live.json`. Restores ran in the order D1, D3, D2. D2 came last because it is the hardest case: the journal is ahead of the database. The journal and pin were never backed up or restored.

## Acceptance

| #124 requirement | Result | Receipt |
| --- | --- | --- |
| Manifest of cutoff, database/migration identity, retained journal and deployment/scope | Recorded for each cutoff. The pin digest and deployment were identical across all three cutoffs. | `manifest-d1/d2/d3.json` |
| Each backup restores retained identifiers, evidence bytes and histories to its cutoff | After cleanup, 41 (D1), 41 (D3) and 38 (D2) of 45 tables are byte-identical to their manifest. These include incidents, incident activity, runs and evidence, and the evidence SHA-256 values are equal. Cleanup-owned tables kept every identifier and are labelled with the cleanup that changed them; their content is not compared. Sessions, review receipts and heartbeats are not compared. No table had an unexpected change or a missing identifier. Per-row digests name the rows that cleanup changed: in D1 the invalidated plan; in D2 the plan, job and execution it retired. | `restore-dN.json` → `tables`, `rows`, `evidence_bytes_equal` |
| Post-cutoff loss made explicit | Each restore lists the identifiers that exist only after the cutoff (`lost_after_cutoff`) and the rows that changed after it (`changed_after_cutoff`). Every restore loses the post-cutoff incident, its run, 12 evidence rows and 2 activity entries. D1 also loses the later execution and the plan's later approval. D2 also loses its execution's later dispatch and acknowledgement, and its job's completion. | `restore-dN.json` → `rows` |
| Start with product and writer dispatch disabled, independent of restored rows | Before each restore, the host permit entered recovery with every class denied. `maintenance enter_recovery` reported `applied` for every configured target; the driver stops if it does not. After each restore, every target was `enabled=false, recovery_mode=true`. This includes D1, which was taken before any execution existed. | `restore-dN.json` → `maintenance_enter`, `permit_after`, `targets_after` |
| Revoke restored sessions, invalidate old approvals/work, retain unknowns and locks | `restore-admin` reported its counts. A session cookie captured at each cutoff returned `401`. Approving a restored plan returned `409 plan_unavailable`. In D2, the queued execution became `outcome_unknown` and held its target lock, so a primary proposal returned `409 target_locked`. The journal kept its acknowledged attempt. After trusted reconciliation, the execution stayed `outcome_unknown` with `maintenance_released=true` and no lock. | `restore-d2.json` → `cleanup`, `executions`; `reconcile-after-d2.json` → `after.executions` |
| Fence old worker and late requests | The worker container from before the restore ran against the restored database: it was `running` and its heartbeat advanced. The permit was in recovery, and `jobs` and `job_attempts` stayed byte-identical. Late dispatches were sent with the worker's own key. A retired epoch returned `409 target_quarantined`, both while the target was quarantined and after it was enabled again. Reusing the restored execution identity with changed content returned `409 dispatch_conflict`. | `restore-dN.json` → `old_worker`, `late_requests`; `fresh-path.json` → `retired_epoch_while_enabled` |
| Restart and replay add no writes | With the API stopped, the run restarted the executor, replayed the same `enter_recovery` request ID and reran `restore-admin`. The replay returned `replayed: true`. The rerun revoked only the sessions opened for the checks. Deployment UID, generation, replicas and fence annotations were unchanged, and so was the journal attempt list. | `restore-dN.json` → `maintenance_replay`, `cleanup_replay`, `no_write` |
| Quarantined targets readable and investigable, with no new executable plan or approval, including the zero-row UID | Viewer reads of both workloads, incidents and the run returned `200`. After `resume --class collection` only, an investigation of `checkout-standby` collected Kubernetes state, events and logs. Proposals on both targets returned `409`: `target_quarantined`, or `target_locked` for the restored D2 lock. A plan proposed before quarantine got `409 target_quarantined` on approval. | `restore-dN.json` → `reads`, `quarantined_investigation`, `new_proposals_while_quarantined`; `manual-exit.json` → `approval_under_quarantine` |
| Missing proof keeps the target quarantined | `checkout-standby` was scaled to zero, a deliberate out-of-protocol lab action. Its `enable_target` was then denied with `maintenance_state_changed`, and the target stayed `enabled=false, recovery_mode=true`. After one replica was restored, the proof passed. | `reconcile-after-d2.json` → `results.proof_unavailable` |
| Ordinary roles cannot enable | No API route matches maintenance, enable or recovery-mode operations in the generated OpenAPI. The maintenance CLI inside the API container reports the channel unavailable. Enablement went only through the root peer-credential socket. | `restore-dN.json` → `maintenance_surface` |
| Reconcile, then attest enablement and recovery exit through the trusted CLI | First the run recorded the current Deployment, journal and execution state. Then it ran `enable_target` with fence and health proof for each target, followed by `exit_recovery_mode` with every target. The executor audit shows each request's operation, reason, evidence reference, status and root actor (UID 0, peer PID, deployment ID). The host permit returned to `normal` (generation 8), because the saved prior state was normal. | `reconcile-after-d2.json` → `results`, `after.audit` |
| One fresh permitted path | A new proposal was reviewed and explicitly approved (`200`). It produced exactly one new execution, `24836fe8-…`, acknowledged as `checkout` 2 → 1, and added one new journal attempt. Restored plans did not run. | `fresh-path.json` |
| Missing authority keeps quarantine; honest manual exit | With dispatch and targets closed, the executor pin volume was removed. The executor then exited with code 1 and logged "Pinned executor authority is missing or changed". No maintenance channel existed. A proposal returned `503 executor_unavailable`, reads stayed `200`, and no Deployment was written. The receipt records the manual handoff. | `manual-exit.json` |
| Owned targets; evidence and volumes outside destructive test databases | Only this project's `cloudops_restore_test` was restored. The driver checks each running service's own connection and every rendered profile. It refuses projects it did not prepare and default or retained projects. Dumps and cookies stayed in an ignored private directory and were deleted with the project. | `prepare.json` → `connections_use_restore_database`; `restore-dN.json` → `executor_uses_restore_database`; `run.json` |

## Limits

- **Targets:** lab fixtures on single-node K3s, with the writer limited to two named Deployments. The fence proof is the existing E07 `resource_version_fence_v1`.
- **Proof failure:** demonstrated through readiness, by scaling the target to zero. A competing out-of-protocol writer was not simulated.
- **Verification:** application verification of the v1 scale writes remains `inconclusive`.
- **What the backup covers:** PostgreSQL only. The external journal, pin and permit stay on the host by design. Losing them leads to the manual exit, not to reinitialization.
- **Core-only installation:** the documented path without an executor was not exercised.
- **Secret scan:** the receipts were scanned for the private passwords, the database password, the reader/writer tokens, the dispatch, KRR and intake keys, session cookies, and credential and local-path patterns. No hits.
