# E10 S5 release backup and recovery qualification (#124)

This ledger records one qualification run of the [release backup and restore](../../release-restore.md) procedure for [#124](https://github.com/pho-veteran/cloudops-copilot/issues/124) under [E10 #13](https://github.com/pho-veteran/cloudops-copilot/issues/13). The run is owned, local and disposable. It used project `cloudops-release-restore` and database `cloudops_restore_test` on a Docker Desktop 29.9.2 Linux engine with a WSL2 Ubuntu client, between 2026-11-23 12:07:47Z and 12:13:35Z.

All application, executor and cleanup connections used only that database. It is not an EC2 deployment, and it is not the second-operator restore, which belongs to [#126](https://github.com/pho-veteran/cloudops-copilot/issues/126).

## Identity

| Item | Value |
| --- | --- |
| Candidate and bundle revision | `b488030c2ec90439e6e290a56452fa0d548a5b67`, built with `scripts/release-bundle.py` and installed from `source.tar` and `images.tar` after `verify --loaded` |
| Bundle SHA-256 (`source.tar` / `images.tar`) | `0b2dfac0…caf46911` / `1c9e17ae…f68dc65` |
| Image IDs (backend / https / db) | `sha256:0c9a4780…2e48fc` / `sha256:ea6633d3…b31908` / `sha256:837bed18…fc03af3b` |
| Topology | `compose.yaml`, `compose.https.yaml`, `compose.integration.yaml`, `compose.release.yaml`, `compose.release-lab.yaml`, `compose.release-restore.yaml`. K3s runs `v1.37.1-k3s1` by digest |
| Database | PostgreSQL 17.12, migration head `0020_operations_quota_wait_index` |
| Writer targets | `checkout` `8edb767b-…3a1034` with records; `checkout-standby` `b51bbc5b-…344129`, configured with no execution history |
| External authorities | Executor deployment `7610dcd1-a981-46bc-936e-9731ad8aa73c`, pin SHA-256 `f8452040…07761e8`. Host permit authority `38ce25eb-46f9-410b-84ba-4ae3b5e762a3` |
| Backend suite on the tooling candidate | 2307 passed on Linux (PostgreSQL 17.12). The final candidate changes only the driver's cooldown wait and its tests pass |

## Cutoffs

Each cutoff is a `pg_dump -Fc` of the running database with a `ReleaseRestoreManifest/v1` (`manifest-dN.json`). The manifest records:

- cutoff time and transaction, dump SHA-256 and size;
- PostgreSQL version and migration head;
- bundle identity;
- per-table row digests and identifier digests;
- evidence SHA-256 values;
- permit, journal and maintenance target state;
- configured targets, scope digests and Deployment state.

| Cutoff | State | Transaction | Dump | Journal attempts |
| --- | --- | --- | --- | --- |
| D1 | Scale plan proposed, not approved | 798 | `cc954c57…` (125,791 B) | none |
| D2 | Plan approved, execution `queued`, worker stopped | 815 | `d33a87de…` (126,648 B) | none |
| D3 | Execution dispatched, Kubernetes acknowledged, `checkout` 1 → 2 replicas | 828 | `6bd7db46…` (126,951 B) | 1 acknowledged |

After D3, the run added a post-cutoff incident, notes and a completed investigation, then recorded `live.json`. The journal and pin were never backed up or restored.

## Acceptance

| #124 requirement | Result | Receipt |
| --- | --- | --- |
| Manifest of cutoff, database/migration identity, retained journal and deployment/scope | Recorded for each cutoff. The pin digest and deployment stayed identical across all three. | `manifest-d1/d2/d3.json` |
| Each backup restores retained identifiers, evidence bytes and histories to its cutoff | After cleanup, 40 (D1), 38 (D2) and 41 (D3) of 45 tables were byte-identical to their manifest, including incidents, incident activity, runs and evidence. Evidence SHA-256 values were equal. Every changed table kept its identifiers and changed only for a named reason: revoked sessions, retired reviews, jobs and executions, invalidated plans, or scope sync counters. No unexpected change. | `restore-dN.json` → `tables`, `evidence_bytes_equal` |
| Post-cutoff loss made explicit | Each restore lists the identifiers in the live snapshot that are absent from the restored database. All three restores lost the post-cutoff incident, its run, 12 evidence rows and 2 activity entries. D1 also lost the later execution. | `restore-dN.json` → `post_cutoff_loss` |
| Start with product and writer dispatch disabled, independent of restored rows | Before the restore, the host permit entered recovery with every class denied. `maintenance enter_recovery` closed every configured target. After the restore, every target reported `enabled=false, recovery_mode=true`, including D1, which was taken before any execution existed. | `restore-dN.json` → `permit_after`, `targets_after` |
| Revoke restored sessions, invalidate old approvals/work, retain unknowns and locks | `restore-admin` reported its counts. A session cookie captured at the cutoff returned `401`. Approving the restored plan returned `409 plan_unavailable`. In D2, the queued execution became `outcome_unknown` with its target lock held, and a primary proposal returned `409 target_locked`. The journal kept its acknowledged attempt. | `restore-dN.json` → `cleanup`, `restored_session`, `old_plan_approval`, `executions` |
| Fence old worker and late requests | The worker from before the restore started against the restored database while the permit was in recovery. Jobs and attempts stayed byte-identical. A dispatch sent with the worker's own key and the retired epoch returned `409 target_quarantined`. Reusing the restored execution identity with changed content returned `409 dispatch_conflict`. | `restore-dN.json` → `old_worker`, `late_requests` |
| Restart and replay add no writes | The run restarted the executor, replayed the same `enter_recovery` request ID (`replayed: true`) and ran `restore-admin` again. Afterwards, Deployment UID, generation, replicas and fence annotations were unchanged, and the journal attempt list was unchanged. | `restore-dN.json` → `maintenance_replay`, `cleanup_replay`, `no_write` |
| Quarantined targets readable and investigable, with no new executable plan or approval, including the zero-row UID | Viewer reads of both workloads, the incidents and the run returned `200`. After `resume --class collection` only, an investigation of `checkout-standby` collected Kubernetes state, events and logs. Proposals on both targets returned `409` (`target_quarantined`, or `target_locked` for the restored D2 lock). | `restore-dN.json` → `reads`, `quarantined_investigation`, `new_proposals_while_quarantined` |
| Ordinary roles cannot enable | No maintenance, executor or enable endpoint exists (`404`). Enablement used only the root peer-credential maintenance socket. | `restore-dN.json` → `maintenance_paths`; `reconcile-after-d3.json` |
| Reconcile, then attest enablement and recovery exit through the trusted CLI | The current Deployment and journal state was recorded first. Then `enable_target` ran for each target with fence and health proof, followed by `exit_recovery_mode` with every target. Each receipt carries an actor (deployment root, UID 0, peer PID, deployment ID) and its proof. The driver sends a reason and the reconciliation record as evidence with every request, and the executor stores both in its audit. The host permit returned to `normal` (generation 8) because the saved prior state was normal. | `reconcile-after-d3.json` |
| One fresh permitted path | A new proposal was reviewed and explicitly approved (`200`). It produced exactly one new execution, `23618b83-…`, acknowledged as `checkout` 2 → 1. It added one new journal attempt, and restored plans did not run. | `fresh-path.json` |
| Missing authority keeps quarantine; honest manual exit | With dispatch and targets closed, the executor pin volume was removed. The executor refused to start (`exited`; missing pin). No maintenance channel existed, so enable was unavailable. A proposal returned `503 executor_unavailable`, reads stayed `200`, and Deployments were not written. The receipt records the manual handoff. | `manual-exit.json` |
| Owned targets; evidence and volumes outside destructive test databases | Only the disposable project's `cloudops_restore_test` was restored. The driver refuses any other database or a default/retained project. Dumps and cookies stayed in an ignored private directory and were deleted with the project. | `prepare.json`, `run.json` |

## Limits

- **Kubernetes topology:** the targets are lab fixtures on a single-node K3s. The writer is limited to two named Deployments, and the fence proof is the existing E07 `resource_version_fence_v1`.
- **Verification:** application verification of the scale writes remains `inconclusive`, as for every v1 scale write.
- **Data scope:** the backup covers PostgreSQL only. The external journal, pin and permit are retained on the host by design. Losing them leads to the manual exit above, not to reinitialization.
- **Secret scan:** all receipts were scanned for the private passwords, the database password, the reader/writer tokens, the dispatch, KRR and intake keys, session cookies, and credential and local-path patterns. No hits.
