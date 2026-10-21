# Project conventions

## Language

Use English for product UI, demo copy, accessibility text, generated reports, GitHub issues and EPICs. Preserve human-authored input and evidence in their original language. Chat may remain Vietnamese.

## Planning source of truth

Use the [system tracker](https://github.com/pho-veteran/cloudops-copilot/issues/3) and its feature EPICs for active scope, architecture, UX decisions, research, dependencies and acceptance. Do not recreate parallel proposal/research/backlog documents in the repository. Keep source and directly useful implementation/run instructions here.

The proposal is a system framework. Research and refine each feature inside its EPIC. Create task child issues only when that EPIC starts, after settling the contracts needed for its first slice. Do not pre-create all task backlogs or treat roadmap publication as completed design/runtime validation.

Each feature includes its relevant UI, API, data, tests and documentation. Record cross-feature decisions in the system tracker. Use the user-scope `epic-authoring` skill when available, with behavior-based acceptance, evidence and explicit dependencies.

Distinguish prototype, source-inspected, fixture-tested and live-verified claims. Preserve independent incident, investigation and execution states. Record deferred checks with their remaining gate; old review verdicts apply only to their actual candidates.

## Prototype verification

Run `npm ci`, `npm run dev`, `npm test` and `npm run build` from `apps/web` as needed. The prototype uses local fixtures; do not describe its outcomes as live cloud operations.
