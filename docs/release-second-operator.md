# Second-operator release walkthrough

Use this run sheet with the [release packet](evidence/e10-s7/README.md) for
[#126](https://github.com/pho-veteran/cloudops-copilot/issues/126). The second
operator follows the published install and product procedures on the supported
lab. Record deviations where they happen so another operator can reproduce the run.

## Prepare and install

1. Ask the deployment owner for the selected version 2 release bundle and
   trusted manifest, the approved S2 target and timebox, and the planned stop
   and retention owner. Set up separate application Operator and Viewer
   accounts. Identify who performs trusted host steps and who performs the
   product walkthrough.
2. On a clean install directory, follow [Pinned HTTPS application package](release-package.md).
   Verify the bundle before extraction and load both `images.tar` and
   `lab-images.tar` before `verify --loaded`. Start the core with
   `--no-build --pull never`. Compare the running bundle image IDs with the
   manifest. Record the revision, archive hashes, manifest and actual image
   IDs, migration head, sanitized target identity and project names under
   `install`.
3. Reach `https://localhost:8443` with the [S2 SSH tunnel and CA procedure](../deploy/ec2/README.md#install-and-reach-the-existing-origin).
   Verify readiness and run [the HTTPS smoke](foundation.md#local-https-exercise)
   with the dedicated accounts. Record the port binding, CA fingerprint, exact
   origin, worker readiness and dispatch state.
4. Stop the core with its volumes retained before starting the separate
   [owned recovery lab](release-package.md#owned-recovery-lab-with-release-images).
   Use a new `cloudops-release-recovery-NAME` project and the same verified
   bundle. Follow that guide's preflight, start and recovery configuration
   sequence. Check the new project's readiness and provision its own Operator
   and Viewer accounts. Perform the Edge walkthrough at the same origin on port
   8443 using the active project's CA and approved browser trust. Verify the
   allowlisted Deployment UID and distinct reader, monitoring and writer
   identities. Record the product mounts and scoped denials. Keep writer
   dispatch closed until the target and action have the required proof.

### Verify the release integration before the walkthrough

The core and owned recovery lab use pinned application images from the same
bundle. Verify the alert and recommendation paths on the owned recovery lab.

| Path | Current implementation | Evidence needed on the candidate |
| --- | --- | --- |
| Alert delivery | The owned recovery lab starts the pinned Alertmanager image as root with only `DAC_OVERRIDE` for the owner-only token and CA. The API receives the separate intake config with the token digest. [Sender contract](alert-intake.md) | Verify the actual mounts and credential readability, the project CA and authenticated delivery for the pinned scope. Show the exact persisted delivery, incident and first Run in Edge and check redelivery. |
| KRR | A v2 bundle includes the pinned KRR runner in `lab-images.tar`. The owned recovery lab supplies Prometheus and the scoped runner configuration. The E08 short driver still expects the retained lab marker. [E08 receipt](evidence/e08-r6-integrated.md) | Record the running backend, worker, KRR and Prometheus image identities, source policy, scope and runner constraints. Verify readiness with a driver bound to the exact owned release project before executing the short run. |

The earlier permission probe used the image's default `65534:65534` user and
dummy files. It explains the original disposable lab's owner-only bind problem.
The owned recovery lab has its own sender configuration and still needs an
actual HTTPS delivery receipt.

Keep source-built rehearsals with their own runtime bindings. If the installed
candidate cannot exercise either path, record the affected AC and the missing
proof in the packet. Carry the exact same candidate identity into the model and
E07 checks. Recheck affected proof when any UID, source, image, policy or
model/profile binding changes. Use the [model profile](diagnosis-model.md) and
[qualification procedure](diagnosis-acceptance.md) to record the public profile
identity, credential-free behavior digest, expected and served model, and
qualification reference. A successful connectivity check and matching quality
qualification are two separate receipts.

## Collect workflow receipts

Record fresh trial and reset IDs for the two S7 reproductions. Suggested cases are a
qualified `recovery-traffic-scale` run and `pending-insufficient-cpu` for the
diagnostic-only branch. Use the matching [S3 case and reset procedures](evidence/e10-s3/README.md).
If the scale binding lacks qualification, choose another qualified S3 recovery
scenario or record that the approved recovery gate remains open. A manual
outcome is also exercised below. Any S7 reproduction counts toward S6 only if
predeclared there.

| Receipt | Operator steps and checks |
| --- | --- |
| `alert` | Send a scoped firing alert through the authorized sender. Open it from **Connections → Alert intake**, record the exact incident and first Run, then inspect evidence and coverage. Redelivery of the same episode leaves one incident/first Run. |
| `manual` | From **Workloads**, create an incident and wait for the first Run to store useful evidence. Request a second investigation in the same incident and wait for its evidence. Record both Run IDs, compare them, and follow an exact saved Knowledge reference. Before forcing a subsequent investigation failure in the owned lab, record the last useful Run ID, evidence ID and payload SHA-256. After failure, open the same evidence and verify its digest. |
| `recovery` | Verify the matching model and application post-check qualification. Review the exact eligible plan, explicitly approve it, then follow apply, verification and incident disposition. Record plan digest, execution and verification IDs, and independent write evidence. Reproduce one diagnostic-only case and show its evidence, manual guidance and absence of an eligible executable plan. |
| `branches` | Walk through manual OOM verification and resolution, stale-plan rejection followed by explicit re-investigation, and unavailable quota with manual handoff. Capture distinct no-op, inconclusive and unknown outcomes using the corresponding supported or controlled receipts. Save which evidence class was used for each outcome. |
| `continuity` | Resolve and download the saved resolution. Reopen, add a new Run, and download the same saved resolution again. Compare SHA-256 and size. During a controlled provider outage, verify saved Incident, evidence, Knowledge and report reads with both roles. Check that polling, reload, reauthentication and Viewer reads cause zero new action/check POSTs. Keep intentional user submissions distinguishable in the request log. |
| `edge` | Verify the matrix below with both roles. Record the Edge version, origin, case, viewport, theme, motion setting, observed focus and exact saved download result. |

Use [stored handoffs](foundation.md#stored-data-incident-handoff),
[Run comparison](investigation-comparison.md), [Knowledge](knowledge-ui.md),
[E07 recovery](e07-local-acceptance.md) and [E09 smoke](evidence/e09-integrated.md#short-smoke-for-e10)
for the underlying interactions.

### Edge matrix

| Viewport | Themes | Interaction |
| --- | --- | --- |
| 1440 px desktop | Light, dark | Full journeys, Viewer export and explicit actions as Operator |
| 768 px tablet | Light, dark | Navigation, detail layout, dialogs and saved reads |
| 320 px and 390 px mobile | Light, dark at both widths | Role permissions, wrapping, exact selection, no horizontal page overflow and export |

At each viewport verify visible focus and readable contrast in the states shown.
Tab to the approval or handoff trigger, open the dialog with the keyboard,
check initial focus and Tab/Shift+Tab containment, close with Escape, then
verify focus returns to the trigger. Recheck with reduced motion enabled. Confirm the
Viewer can inspect and export the same stored objects and has no mutation controls.
Check the backend denial separately. Retain sanitized screenshots and test results.

### Bounded KRR receipt

Keep the [E08 short recipe](evidence/e08-r6-integrated.md#release-handoff) and its
bound. The current `scripts/probe-resource-history.py` looks for the owner marker
under `tmp/local-testing-lab`, while the owned release project's marker is under
`tmp/cloudops-release-recovery-NAME`. Record the deployed short-run gate as
blocked until a supported driver can validate the actual project marker, exact
runtime containers and source bindings. Once it passes read-only readiness on
that deployment, run one `--short`. Retain any runs from the historical E08
lab as evidence bound to that lab.

Record preflight, elapsed time against the 138-second post-preflight cap,
coverage and exclusions, cleanup, report ID and exact report hash. Download the
selected report in Edge and compare its bytes with the authorized API export
from `/api/recommendation-reports/{report_id}/export`. Recheck the bytes after
restart or source outage. A real short run may return `insufficient_data`.
Demonstrate numeric behavior using labeled matching fixtures or eligible
retained records with their original candidate and evidence class.

## Back up, restore and stop

While the owned recovery lab is running on the authorized EC2 target, record
its measured footprint and headroom, including CPU, memory, disk and cgroups
under idle and representative bounded load. Link matching S2 receipts or
repeat the affected measurements when the hosting or runtime binding changes.
Record these measurements before moving to the disposable restore topology.

As the trusted deployment operator, finish and stop the owned recovery lab
with its volumes retained before reusing port 8443. Use the separate
[disposable release-image writer restore topology](release-restore.md#scope-and-compose-files)
with a new project, the same verified bundle and the S5 restore overlays.
Verify the running image IDs and that the database, application, executor and
restore-admin target `cloudops_restore_test` before the drill. The owned
release recovery project refuses the restore overlay.

Record the cutoff, dump hash, migration, executor journal lineage and
external authority bindings before restoring. Capture retained IDs and bytes,
post-cutoff loss, session revocation, no replay and quarantine for every
configured target, including a target with no restored execution history.
Preserve unknown outcomes. Reconcile current state before any fresh enablement,
and retain the manual handoff for unresolved targets.

Stop the disposable writer project with its planned retention. Follow the
[S2 stop and retention procedure](../deploy/ec2/README.md#estimate-stop-and-retain)
for the exact authorized EC2 target. Record the last readiness before stopping
and the confirmed stop time, dated cloud rates and actual costs, retained
volumes, snapshots, IP inventory, residual costs and the responsible owner
after stopping. Attach `restore` and `stop` receipts to the packet.

## Demo script

1. Show the bundle revision, trusted origin and roles. Open the alert incident
   and compare its evidence with the manually created incident.
2. Open one exact evidence payload and a Knowledge citation. Show a qualified
   plan review and its recorded verification, then the diagnostic-only manual
   handoff. Show how Connections explains unavailable services.
3. Switch to Viewer, download a saved resolution and the exact KRR report.
   Show the report's evaluated coverage and state. Open the same resolution
   after reopen and show the matching digest.
4. Show the restore cutoff, readable saved history, quarantine and stop receipts.
   Finish on the evidence index and its current limitations. Record the demo
   against the deployed candidate and keep credentials out of frame.
