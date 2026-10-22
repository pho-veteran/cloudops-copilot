# Project conventions

## Language

Use English for product UI, demo copy, accessibility text, generated reports, GitHub issues and EPICs. Preserve human-authored input and evidence in their original language. Chat may remain Vietnamese.

Keep Issues and PR descriptions focused on project behavior, decisions and verification evidence. Omit assistant/model reviewer names, review-session metadata and tool execution logs. Product requirements for model configuration and endpoint compatibility remain valid technical content.

Keep authored code, templates and skills free of research-source links, borrowed-project attribution and assistant identities. Skills must contain their own actionable instructions and work without external reference documents.

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

Use Conventional Commits for new commits and PR titles. This repository requires a lowercase scope: `type(scope): description`, or `type(scope)!: description` for a breaking change. Write a concise English description of the result.

Allowed types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `style`, `revert`. Use `feat` for new behavior and `fix` for corrected behavior; choose a meaningful scope such as `auth`, `incidents`, `contracts`, `web`, `foundation` or `repo`. Explain breaking changes and migration steps in the commit body/PR, using a `BREAKING CHANGE:` footer where appropriate.

Examples: `feat(incidents): persist manual incident intake`, `fix(auth): reject expired sessions`, `docs(repo): explain local setup`.

Apply this convention through contributor instructions and the PR template. Before merging, check the PR title, new commit subjects and final merge message. For squash merge, use the conventional PR title as the final commit subject. No commit/PR-title CI check or hook is required.

The repository [contribution-writing skill](.agents/skills/contribution-writing/SKILL.md) contains self-contained PR-body and commit-message instructions.

## Ownership and shared changes

Each EPIC owns its feature implementation and consumer integration. Its active owner coordinates review, but contributors can own separate slices. Record named owners when work begins; this repository does not yet have an assigned multi-person CODEOWNERS map.

- API and state-contract changes need producer and affected-consumer review. When only one maintainer is active, document that limitation and use contract tests rather than inventing a second reviewer.
- Coordinate database migrations against current `main`; a migration PR must explain data impact and upgrade/recovery behavior. Do not generate competing heads without an explicit integration plan.
- Keep shared modules small. Add a shared abstraction for a demonstrated cross-feature need, with an owner; avoid copying another feature's persistence or introducing a generic framework.
- Keep changes to shared UI primitives separate from unrelated feature work when possible. Feature-specific components stay with their feature.

System-wide decisions live in #3; foundation boundaries and F0–F4 acceptance live in E00 #15. The local runtime is implemented in `services/backend`; the persisted UI uses feature modules in `apps/web/src/features`. The simulation model in `apps/web/src/demo` is not a server API schema. Operational handoff and consumer instructions live in [docs/foundation.md](docs/foundation.md).

## API and contract changes

Feature-owned FastAPI/Pydantic request and response models produce `contracts/openapi.json` and `apps/web/src/lib/api/generated/schema.d.ts`. Do not hand-maintain a second set of transport DTOs or manually edit generated types. Frontend view models may adapt transport data without changing server semantics.

A contract-changing PR must link the owning decision, identify consumers, regenerate affected artifacts, and include focused producer/consumer evidence. An OpenAPI diff does not prove authorization, state transitions or retry behavior; test the affected behavior too. Keep mock responses conformant to the contract and clearly labeled as fixtures.

Contract generation/drift checks, backend behavior tests and migration checks are required for affected changes. Run `uv run --project services/backend python scripts/contracts.py --write` after contract changes, then check with `scripts/contracts.py` and `scripts/test-contract-drift.py` through the same Python environment. The exporter runs without database configuration or external credentials. Contract tooling is isolated in `tools/contracts` to satisfy its TypeScript 5 peer range while the web app retains TypeScript 7.

## Local setup and checks

Follow [README.md](README.md) for Compose startup, migration/account setup, the persisted journey and the simulation entry point. Use Node from `.node-version`, Python from `.python-version`, uv 0.9.26, and the committed npm/uv lockfiles for host tooling.

For web changes:

```sh
npm ci --prefix apps/web
npm --prefix apps/web test
npm --prefix apps/web run build
```

For backend/database changes with Compose configured:

```sh
./scripts/check-backend.sh
```

That script clears the separate `cloudops_test` database, never the application database. Tests reject database URLs without the `_test` suffix. The process interruption test waits for a real 30-second lease, so the suite normally takes at least 35 seconds.

For shared contracts, install locked tooling and check both producer and consumer:

```sh
npm ci --prefix tools/contracts
uv sync --project services/backend --frozen
uv run --project services/backend python scripts/contracts.py
uv run --project services/backend python scripts/test-contract-drift.py
npm --prefix apps/web test
npm --prefix apps/web run build
```

Run `uv run --project services/backend ruff check --config services/backend/pyproject.toml services/backend scripts` and the matching `ruff format --check` command for Python changes. Migrations have one Alembic history; use `uv run --project services/backend alembic -c services/backend/alembic.ini revision --autogenerate -m "describe the change"` with an explicitly configured local `DATABASE_URL`, then inspect the generated operations. Never put secrets in migration source.

On Windows PowerShell, use `npm.cmd` if execution policy blocks `npm.ps1`; changing execution policy is unnecessary. Use a Bash-capable shell for `scripts/check-backend.sh`, or run its Compose commands directly.

CI runs locked web tests/build, backend PostgreSQL integration tests, migration readiness/repeat-upgrade checks and deterministic contract checks. It does not deploy or contact a real cluster/model. A workflow file does not imply branch protection. Documentation-only changes need link/content review; do not add tests that restate prose.

## Review and completion

Explain the problem, resulting behavior, linked scope and checks actually performed. Include UI journey evidence when UI behavior changes, and migration or recovery evidence when persistence changes. Keep evidence free of credentials and sensitive telemetry. Preserve human-authored source text when quoting it.

A merged PR completes its reviewed slice; an EPIC closes only when its integrated acceptance is demonstrated. Record deferred checks and remaining gates in the EPIC. Do not treat fixture tests or a planning review as a live integration pass.
