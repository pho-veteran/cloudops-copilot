# Owned E07 recovery acceptance

This protocol uses one disposable Linux/Docker K3s lab. It does not authorize a
production cluster, Kubernetes datastore restore, replacement of an executor
journal, cloud deployment or E10 release qualification. The campaign ledger
records actual outcomes and remaining acceptance gates in [the integrated receipt](evidence/e07-integrated.json).

The recorded campaign is incomplete. Real three-cut PostgreSQL restore, delayed
forward/inverse exclusion, lost acknowledgement, approved verified scale and
separate safe inverse have passed. The live model memory/image positives, final
qualification, delivery and cleanup remain open; this guide does not certify E07.

## Bind the lab before any fault

Record the Compose project, original Deployment and Node UIDs, reader/metrics
source identities, actual Node/Pod cgroups and allocatable resources. The serving
Kubernetes writer belongs only to the isolated root gateway. API, collector and
observer get read-only scoped connections; only the finite model worker gets the
provider credential. Preserve the original SQLite journal, flock inode,
independent identity pin and host dispatch authority outside application backups.

The recovery sample has a bounded160MiB resident working set and two serving
slots. Its startup log precedes allocation so a real128MiB OOM leaves observable
current/previous logs. The healthy256MiB limit and192MiB request are lab-specific
qualified values, not production defaults or proof a leak was repaired. The
bad-image variant rejects startup; restoring the confirmed healthy image repairs
that controlled fault. Absolute scaling changes only replicas.

Use finite independently offered traffic and request-derived metrics. Bind the
exact sample/load source and runtime image digests, thresholds, queries and
source identity into each verification profile. Scale uses180seconds; memory and
restore use300seconds, each with60seconds settling,30second slots and the
configured coverage/load rules. Acknowledgement, healthy rollout and falling
traffic cannot establish recovery. Preserve each initial failure, no-op and
inconclusive outcome.

The integrated one-Pod memory/image scenarios use the supported `Recreate`
strategy. Record this fixture choice and restore the original strategy during
cleanup. An input exceeding the qualified model's32KB bound must abstain before
HTTP. In an owned lab, prune only original-UID-owned obsolete ReplicaSets with
zero desired/current replicas and no Pods, using UID/resourceVersion deletion
preconditions. Recollect a fresh bounded window; never remove required evidence
from a stored Run or silently enlarge the qualified profile.

## Finite live worker

`services/backend/tests/e07_live_worker.py` runs only against the explicitly named
`cloudops_e07_integrated_test` database. It is not collected by pytest or CI. Mount
its ledger outside PostgreSQL and preserve it across every restore. The existing
E07 ledger includes the preflight probe and has a50-request ceiling and60,000
observed-token stop. Unknown usage stays recorded. Unsettled requests require
explicit reconciliation; provider failures stop the worker instead of silently
retrying. One structural repair is shared across cases. Every reservation records
its exact Run, Analysis and serialized request digest before HTTP, without request
text. Lock and temporary paths append suffixes to the complete ledger filename;
atomic ledger replacement cannot move the lock to a different inode.

Use `--collect-only --requests 0` to finish real collection and admit Analysis
without sending it. Inspect the assembled context, required source eligibility
and byte bound before permitting inference. The draft worker requires the exact
retained E06 tuple at startup and again at the actual send boundary, including
the client profile. Context v2 changes that tuple and intentionally invalidates
the old qualification. A new complete owning-rubric qualification and the pending
input/token-budget authorization are required before installing its operational
identity; do not edit an old qualification to bypass this gate. Provider output,
validation and citations are never substituted.
`--case`, `--seconds` (maximum900) and `--requests` (maximum3 per invocation) make
the selected attempt finite. Non-model verification uses `--requests 0` and no
model credential/configuration. Save actual responses in the application history;
the external budget retains usage/identity/digests without copying response text.

Plan generation itself never calls the model. Read the exact immutable diff,
obtain a current session-bound review receipt, and explicitly approve. An inverse
is a separate plan, review and decision inside its original fixed window. Unsafe
old images/resources, unknown forward attribution or expired authority remain
manual-only. Do not manufacture an inverse qualification for the fault state.

## Application-database restore

Take real custom-format `pg_dump` snapshots before approval, after approval but
before dispatch, and after a conditional acknowledgement while verification is
pending. Keep dumps private. Include real sessions/reviews and a configured
executor UID absent from application rows. Preserve the external request ledger.

Before **each** `pg_restore`, stop every API/worker/observer using that application
database, enter host dispatch recovery with every class denied, and enter gateway
recovery for **every configured UID**, including absent-row targets. Verify both
independent states before restoring only the owned `_test` database.

With app processes stopped and the original authority mounts present, run the
trusted `cloudops.entrypoints.recovery_restore` and
`cloudops.entrypoints.diagnosis_recovery` entrypoints. They retire restored
sessions, reviews and pending work; absence of a call in a backup is not proof
that no write happened. Retain historical unknown attribution and quarantine.
Verify the old cookie returns401, the old review cannot approve, no restored job
replays, and the absent-row target remains closed.

Use the real OS-authenticated maintenance CLI for current-state and old-writer
exclusion proof, explicit target enablement and recovery-mode exit. Matching
selected fields, timeout, an expired lease, a restored database or a maintenance
note grants no authority. Resume host classes separately. Finally create a new
plan, new review and new explicit approval, and require its fresh verification to
finish. A newly rejected proposal does not prove successful resumption.

## Consumer handoff and cleanup

E09 consumes typed `RecoveryOutcome/v1`, verification observations, fixed inverse
window and current availability independently. Saved handoff bytes are immutable;
a current target state must not rewrite old attribution. E10 retains its15-run
campaign, rules comparison, cloud packaging and release qualification. This
minimum E07 campaign makes no SLO or production sizing claim.

Stop finite load/model workers first. Restore only captured owned fault fields
through original UID/resourceVersion guards; confirm the original workload is
healthy. Deny host classes and quarantine every configured target before stopping
the remaining owned containers. Retain history, journals and pins; remove owned
credential copies and private backup copies only after required receipts are
saved. Preserve unrelated containers, volumes, browser tabs and host contexts.
