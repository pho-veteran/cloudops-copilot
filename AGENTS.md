# Repository guidance

- Follow [CONTRIBUTING.md](CONTRIBUTING.md).
- When drafting or updating PR bodies, PR titles or commit messages, use [.agents/skills/contribution-writing/SKILL.md](.agents/skills/contribution-writing/SKILL.md).
- Active project scope, research, UX decisions and EPIC acceptance live in the GitHub system tracker and feature EPICs linked from README.md.
- All product UI, demo copy, accessibility text, generated reports, GitHub issues and EPICs use English. Preserve human-authored input and evidence in their original language.
- The system framework is organized into feature EPICs. Research details inside each EPIC; create task child issues only when that EPIC starts. Do not pre-create the full implementation backlog.
- Keep the runnable prototype and source distinct from unimplemented backend/runtime claims. Do not recreate legacy proposal/research documents as a second planning source.

## Implemented foundation boundaries

- The persisted app is the default web entry; `?simulation=1` retains the localStorage simulation. Never promote simulation RCA/actions into API results.
- Backend feature modules own schemas/services/persistence; entrypoints register dependencies. Shared config/database/job mechanics do not import feature policy.
- Regenerate OpenAPI/TypeScript with `scripts/contracts.py --write` in the backend Python environment; do not edit generated transport artifacts by hand.
- Follow README and `docs/foundation.md` for setup, PostgreSQL checks and UI journeys. Test databases must end in `_test`; never run destructive tests against application data.
- Foundation collection is synthetic and bounded. AI diagnosis, live cluster access and execution remain unavailable until their owning EPICs implement them.
