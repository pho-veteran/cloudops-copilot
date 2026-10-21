# Prototype experience check — 21 October 2026

Scope: interactive React/Vite design prototype at `apps/web`, before EPIC planning. This report does not certify production behavior or user acceptance.

## Result

- TypeScript + Vite production build: passed.
- Vitest: 22 state-machine tests passed.
- Independent GPT-6.1 Sol QA: changes requested initially; six findings corrected; final bounded recheck passed. The reviewer directly exercised reducer sequences and read the integrated UI.
- Browser walkthrough: OOM review/approve/execute/verify/resolve and resolution selection; unknown write, two bounded reconciliations and evidence-backed unlock; new manual investigation with no phantom findings, healthy completion without recovery plan; transport HTTP 200 with failed RCA contract.
- Browser layout: default desktop, 390px story chooser, 320px incident and manual-verification form. No document horizontal overflow after fixing body minimum width (305px client/305px scroll at 320px viewport with scrollbar).
- Keyboard: Escape closes story dialog and returns focus to its opener. Dialog/Sheet use Radix focus management plus opener restoration for controlled dialogs.
- Browser screenshot: `../screenshots/prototype-incident.png`.

## Corrected during review

1. Manual intake no longer invents a completed alert-originated run.
2. Healthy manual workload does not receive OOM evidence or an executable recovery plan.
3. Resolved incidents cannot start rollback; rollback uses its original approved execution plan, not the latest investigation's proposal.
4. Ordinary diagnostic handoff works without unknown-write reconciliation prerequisites.
5. Known-good candidate and restore preview agree on catalog revision 17; failed revision is 18.
6. Endpoint contract validates structured RCA, evidence IDs, scope and abstention; model tool calling is optional.
7. Executed approvals remain approved in history; only unused proposals are invalidated/staled.
8. Reconciliation cap is enforced in the reducer; unknown target locks survive resolution and require evidence plus old-writer-stop confirmation to clear.
9. Story resets remount local forms; page transitions return to the top; desktop metrics use two columns; narrow forms and tabs remain usable.

## Verification boundaries

- All cloud actions, data, timestamps and provider results are fixtures. Tests validate the prototype's state transitions, not Kubernetes behavior, provider accuracy or concurrency guarantees.
- Queue loading/error/empty/capacity and duplicate/resolved alert controls are UI previews, not a working scheduler or Alertmanager deduplicator.
- Browser checks are representative, not a claim that every combination of all 24 scenarios, browsers, screen readers and breakpoints was exhaustively tested.
- Current/used runbook examples are v3 fixtures. Known-good eligibility and identity use fixed fixture metadata; backend snapshot storage remains implementation work.
- npm production-dependency audit returned zero advisories at this check. Full development-tool audit reports seven high transitive advisories in shadcn CLI's braces/micromatch/fast-glob chain. No forced major downgrade was applied; CLI is a devDependency. Reassess the toolchain before implementation/release.
- Prior Claude proposal reviews remain tied to their original snapshots. This UI check does not extend their verdict to new code, and neither issue is closed or converted to EPICs.

Next review is experiential: can the user understand each feature by completing its story, see why an action is unavailable, and understand the difference between evidence, proposal, execution and verified outcome?
