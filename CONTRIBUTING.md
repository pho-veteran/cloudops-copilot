# Project conventions

## Language

Use English for product UI, demo copy, accessibility text, generated reports, GitHub issues and EPICs. Preserve human-authored input and evidence in their original language. Chat may remain Vietnamese.

Keep Issues and PR descriptions focused on project behavior, decisions and verification evidence. Omit assistant/model reviewer names, review-session metadata and tool execution logs. Product requirements for model configuration and endpoint compatibility remain valid technical content.

## Planning source of truth

Use the [system tracker](https://github.com/pho-veteran/cloudops-copilot/issues/3) and its feature EPICs for active scope, architecture, UX decisions, research, dependencies and acceptance. Do not recreate parallel proposal/research/backlog documents in the repository. Keep source and directly useful implementation/run instructions here.

The proposal is a system framework. Research and refine each feature inside its EPIC. Create task child issues only when that EPIC starts, after settling the contracts needed for its first slice. Do not pre-create all task backlogs or treat roadmap publication as completed design/runtime validation.

Each feature includes its relevant UI, API, data, tests and documentation. Record cross-feature decisions in the system tracker. Use the user-scope `epic-authoring` skill when available, with behavior-based acceptance, evidence and explicit dependencies.

Distinguish prototype, source-inspected, fixture-tested and live-verified claims. Preserve independent incident, investigation and execution states. Record deferred checks with their remaining gate; old review verdicts apply only to their actual candidates.

## Start a contribution

1. Read the owning EPIC and [Foundation v0.1 — E00](https://github.com/pho-veteran/cloudops-copilot/issues/15). E00 owns the interactive foundation; E01–E10 retain feature semantics and complete release acceptance. EPIC numbers are identifiers, not a serial delivery order.
2. For a feature slice, record the active owner and settle the interfaces needed by that slice in the EPIC before creating task issues. Small fixes do not need an artificial planning issue.
3. State which modules and contracts you will change. Coordinate shared infrastructure changes with its owning EPIC and affected consumers.
4. Branch from current `main`, keep a PR focused on a reviewable result, and use the PR template. Link the EPIC with `Related to`; reserve closing keywords for the task actually completed.

Use the general Markdown issue template for a feature, bug, improvement or task; omit sections that do not help explain the work. Create implementation child tasks only after their EPIC starts. A native parent relationship should match the EPIC link in the task body. Dependencies refer to concrete outputs, not whole EPICs when a smaller prerequisite is enough.

## Commit and PR titles

Use [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) for new commits and PR titles. This repository requires a lowercase scope: `type(scope): description`, or `type(scope)!: description` for a breaking change. Write a concise English description of the result.

Allowed types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `style`, `revert`. Use `feat` for new behavior and `fix` for corrected behavior; choose a meaningful scope such as `auth`, `incidents`, `contracts`, `web`, `foundation` or `repo`. Explain breaking changes and migration steps in the commit body/PR, using a `BREAKING CHANGE:` footer where appropriate.

Examples: `feat(incidents): persist manual incident intake`, `fix(auth): reject expired sessions`, `docs(repo): explain local setup`.

Apply this convention through contributor instructions and the PR template. Before merging, check the PR title, new commit subjects and final merge message. For squash merge, use the conventional PR title as the final commit subject. No commit/PR-title CI check or hook is required.

For PR-body and commit-message guidance, use the repository [contribution-writing skill](.agents/skills/contribution-writing/SKILL.md). It adapts Synapse's concise change/rationale pattern to this repository; the local template and conventions remain authoritative.

## Ownership and shared changes

Each EPIC owns its feature implementation and consumer integration. Its active owner coordinates review, but contributors can own separate slices. Record named owners when work begins; this repository does not yet have an assigned multi-person CODEOWNERS map.

- API and state-contract changes need producer and affected-consumer review. When only one maintainer is active, document that limitation and use contract tests rather than inventing a second reviewer.
- Coordinate database migrations against current `main`; a migration PR must explain data impact and upgrade/recovery behavior. Do not generate competing heads without an explicit integration plan.
- Keep shared modules small. Add a shared abstraction for a demonstrated cross-feature need, with an owner; avoid copying another feature's persistence or introducing a generic framework.
- Keep changes to shared UI primitives separate from unrelated feature work when possible. Feature-specific components stay with their feature.

System-wide decisions live in #3; the planned foundation module layout, contract pipeline and F0–F4 delivery live in E00 #15. Proposed backend paths and contract tooling are not implemented merely because they appear in the plan. The fixture model is not the future server API schema.

## API and contract changes

Once the backend foundation is implemented, FastAPI/Pydantic request and response models will produce the OpenAPI artifact and generated TypeScript types. Do not hand-maintain a second set of transport DTOs or manually edit generated types. Frontend view models may adapt transport data without changing server semantics.

A contract-changing PR must link the owning decision, identify consumers, regenerate affected artifacts, and include focused producer/consumer evidence. An OpenAPI diff does not prove authorization, state transitions or retry behavior; test the affected behavior too. Keep mock responses conformant to the contract and clearly labeled as fixtures.

Generation/drift checks, backend tests and migration checks become required with the corresponding foundation implementation. They are not available or claimed as passing in the current prototype-only repository.

## Local setup and checks

Use the Node version in `.node-version` and the committed npm lockfile. From the repository root:

```sh
cd apps/web
npm ci
npm run dev
```

Before a PR that affects the app, dependencies or build/test configuration:

```sh
npm test
npm run build
```

On Windows PowerShell, use `npm.cmd` if execution policy blocks `npm.ps1`; changing execution policy is unnecessary.

The repository workflow runs locked installation, prototype tests and the TypeScript/Vite build. It does not deploy, contact a real cluster/model or validate the future backend. A workflow file does not imply branch protection is configured. Documentation-only changes need link/content review; they do not require new tests that only restate prose.

## Review and completion

Explain the problem, resulting behavior, linked scope and checks actually performed. Include UI journey evidence when UI behavior changes, and migration or recovery evidence when persistence changes. Keep evidence free of credentials and sensitive telemetry. Preserve human-authored source text when quoting it.

A merged PR completes its reviewed slice; an EPIC closes only when its integrated acceptance is demonstrated. Record deferred checks and remaining gates in the EPIC. Do not treat fixture tests or a planning review as a live integration pass.
