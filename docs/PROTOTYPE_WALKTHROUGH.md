# CloudOps prototype walkthrough

This is an interactive React/Vite prototype using shadcn/ui and browser fixtures. It has no backend, Kubernetes, Prometheus, model endpoint or real credentials. Approval, execution, rollback, retry and connection tests only change simulated state; they do not send commands to external systems.

## Run locally

Install Node.js compatible with the Vite version in `apps/web/package.json`, together with npm. From the repository root, run these commands in order:

```powershell
cd apps/web
npm ci
npm run dev
```

Open the local URL printed by Vite, usually `http://127.0.0.1:5173`. If that port is busy, use the actual URL in the output. Keep the terminal running while using the prototype; press `Ctrl+C` to stop it.

Run these checks separately:

```powershell
npm test
npm run build
```

`npm ci` downloads dependencies. After installation and startup, product interactions use local data and do not call a product API. Passing build and test checks does not replace a browser review.

## Demo controls

- **Work queue:** filter or search incidents and open a workspace; **Investigate workload** starts a manual investigation.
- **Workloads:** inventory, workload details, known-good baselines and the **Recommendations** tab. There is no separate Approvals page; decisions belong to the incident workspace.
- **Knowledge:** search/filter and read versioned runbooks with source information, download Markdown, and simulate a degraded index or reindexing.
- **Connections:** inspect health, setup guidance, unknown usage and simulated transport/RCA contract checks.

Use **Choose a story** in the sidebar or **Change story** on the banner to load one of the 24 fixtures. Choosing a story replaces the current business data with a fresh fixture. The restart icon on the banner resets the selected story. The account menu switches between Operator and Viewer and simulates session expiry; the sun/moon button switches themes.

In an incident, **Advance simulation** advances exactly one stage. No background worker runs automatically: keep advancing while the button is available, then read the new state. A typical investigation moves through queued → collecting → analyzing → investigation result; after approval, execution moves through queued → executing → verifying → outcome. Do not use this button to resend a write with an unknown outcome; that scenario has dedicated controls.

Business data and the theme are stored in localStorage. Refreshing normally retains the demo progress. Reload a story for a deterministic starting point; browser storage is not a real incident database. Incident filters and tabs use the URL hash for prototype navigation.

## Suggested walkthroughs

| Role / goal | Interaction path | Expected demo outcome |
|---|---|---|
| Operator: recover from OOM | Choose **OOM recovery** → read Findings → open a citation or Evidence → **Review and approve** → review the diff/target → **Approve change** → **Advance simulation** through the stages | The bounded memory-profile change is simulated; the execution outcome remains separate from incident status. OOM is not automatically described as a memory leak. |
| Operator: reject or test a conflict | For a proposal awaiting approval, choose **Reject** and enter a reason; or open its review, expand **Simulate a conflict before approval**, simulate a target change or expiry, then submit | Rejected/stale/expired plans cannot execute; re-investigate to obtain a fresh plan. Reset the story to try another branch. |
| Operator: prepare a restore baseline | Choose **Prepare a restore baseline** → Workloads → catalog-api → **Confirm known-good** → review revision 17, `APP_MODE=production`, 1/1 Ready and the hash → **Confirm snapshot** → **Demo: inject failed rollout** → open the catalog incident → advance investigation → review/approve restore | The baseline is confirmed before the fault; revision 18 with missing `APP_MODE` is the failed version. Confirmation only records demo metadata. A failed template cannot be confirmed as healthy. |
| Operator: scale conditionally | Choose **Traffic spike** → Findings/Evidence → expand prerequisites, risks and verification → approve the replica diff → advance | Scaling stays within the fixture bounds. Review traffic/latency and capacity together; Ready status alone does not establish service recovery. |
| Operator: diagnose a manual-only pattern | Choose **Pending diagnosis** or **Image pull diagnosis** → Evidence → **Read runbook** → **Manual handoff**, enter a reason → Handoff | These two patterns have no executable plan. The handoff still includes findings, evidence and unverified checks. |
| Operator: start a manual investigation | Work queue → **Investigate workload**, or Workloads → detail → **Start investigation** → choose workload, symptom and window, enter a question → start → advance | A new scoped incident/run preserves the question. No real alert is needed; the question must not be empty. |
| Operator: handle missing evidence/quota | Choose **Insufficient evidence** or **Model quota exhausted** → read the missing-data/waiting state → **Retry investigation**, then advance; eligible waiting runs also offer **Cancel queued run** | Previous evidence remains available; provider failure does not produce a plan from invalid output or trigger a paid fallback. |
| Operator: inspect investigation history | Choose **Failed re-investigation** → read the previous-result banner → **Investigation snapshot** / **View latest run** → **Compare runs**, choose another run | The earlier completed finding remains available; the latest failed run is not presented as a successful fresh conclusion. Each snapshot has its own evidence. |
| Operator: roll back | Choose **Verification and rollback** → approve the action → advance until verification fails → **Review rollback** → review the inverse diff, enter a reason → confirm → advance | Rollback is a separate decision. The rolled_back outcome is not confused with the original action succeeding. |
| Operator: inspect blocked rollback | Choose a rollback-expired, target-changed or unsafe-profile story, advance through the post-check, then review rollback | The guard explains why the inverse write is blocked. In **Rollback cannot complete**, advance the rollback branch to reach manual intervention and hand off. |
| Operator: handle an unknown outcome | Choose **Unknown write outcome** → approve and advance to uncertain → **Reconcile outcome** at most twice → optionally hand off/resolve with a reason → **Verify manually and unlock** | The write is not replayed; resolution retains the unknown outcome and target lock. Unlocking requires evidence, an observed outcome and confirmation that the old request cannot write late. These are fixture declarations, not verification of a real system. |
| Operator: add notes and resolve/reopen | Activity → enter a note → **Add note** → **Resolve incident**, enter a reason → Handoff → select the saved snapshot → **Reopen** with a reason | The resolution snapshot preserves evidence, plan, outcome and notes at resolution; reopening does not modify it. Resolving an incident does not automatically verify recovery. |
| Viewer: browse and export | Choose **Viewer permissions** → open evidence, history and Handoff → **Copy** / **Download Markdown** | Reading/exporting works; acknowledge, approve, note and other mutations are disabled. Clipboard access requires browser permission; use download if copying is unavailable. |
| User with an expired session | Choose **Expired session**, or simulate expiry through the account menu → **Continue with demo account** / **Continue as Viewer** | No real account/password is used. Restoring the session does not replay a previous decision. |
| Resource reviewer | Workloads → Recommendations → choose a report → inspect current/observed/recommended values, units, coverage and exclusions → **Export JSON** | The download contains the six-hour report and its limitations. HPA/recent OOM/insufficient/stale states are not represented as zero-valued recommendations; export does not apply changes or establish bill savings. |
| Knowledge reviewer | Knowledge → search by keyword/tag → **Read runbook** → review source/v3 → download Markdown; try demo reindexing | The catalog remains readable when the index is degraded. Reindexing only changes local state and does not call an embedding provider. |
| Operator: test model connectivity | Connections → **Setup guide** → select a transport/contract/pass/401/429 fixture → **Run demo test** | The UI distinguishes timeout, rejected credentials, rate limiting and invalid structured RCA. HTTP reachability does not imply an RCA contract pass; tool calling is not a baseline requirement. Unavailable usage/quota data remains unavailable/unknown. |

## All 24 stories

| # | Story selector label | What to observe |
|---|---|---|
| 01 | OOM recovery | Approval → verification → resolution. |
| 02 | Prepare a restore baseline | A healthy baseline is recorded before fault injection. |
| 03 | Traffic spike | Capacity and metrics are checked before scaling. |
| 04 | Pending diagnosis | Investigation only, with manual handoff. |
| 05 | Image pull diagnosis | Registry/image evidence; no secret contents are read. |
| 06 | Insufficient evidence | Missing evidence and retry. |
| 07 | Model quota exhausted | Waiting/retry/cancel, with no paid fallback. |
| 08 | Target changed | A stale plan cannot be used. |
| 09 | Expired plan | The approval TTL is explicit. |
| 10 | Verification and rollback | Review the inverse diff and approve rollback separately. |
| 11 | Rollback cannot complete | Manual intervention; rollback is not reported as successful. |
| 12 | Rollback window expired | Rollback has its own deadline. |
| 13 | Rollback target changed | Changed fields are not overwritten. |
| 14 | Unsafe old profile | Do not return to a memory profile that could trigger OOM. |
| 15 | HPA owns scaling | Do not compete for replica ownership. |
| 16 | Insufficient node headroom | No scale plan without adequate capacity. |
| 17 | No known-good baseline | A failed template cannot become the baseline. |
| 18 | External configuration changed | Template restore does not recover ConfigMap/Secret contents. |
| 19 | Failed re-investigation | Preserve the earlier completed run and compare history. |
| 20 | Unknown write outcome | Bounded reconciliation, target lock and manual verification. |
| 21 | Workload already recovered | No-op is not counted as action success. |
| 22 | Inconclusive recovery | Ready status with inadequate coverage or traffic is inconclusive. |
| 23 | Viewer permissions | Viewers can browse/export but cannot mutate data. |
| 24 | Expired session | Demo login and explicit session restoration. |

## What the prototype does not establish

The state machine, guards and screens simulate the product for review. A production backend must still enforce authorization, immutable approval/hash/expiry, persistent jobs, idempotency, identity/precondition checks, scoped execution, reconciliation, telemetry collection and actual post-checks. LocalStorage does not replace a database/audit trail; a fixture hash is not the checksum of a real resource; a local endpoint test does not validate a real provider.

Charts, timestamps, coverage, KRR suggestions and runbook content illustrate the workflow; they are not benchmark results or resource/billing evidence. Controls provide local feedback and real file exports, but do not prove that a backend has been implemented. This guide does not claim comprehensive browser QA or user acceptance of the design. Walk through dark/light themes, desktop/mobile layouts and keyboard interaction before accepting the experience.