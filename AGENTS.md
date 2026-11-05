# GitHub identity — mandatory for this repository

- Use only `pho-veteran` for authenticated GitHub operations on this repository and its worktrees, including reads, pushes, pull requests, comments, reviews, workflow actions and merges. Never fall back to another account.
- Verify the actual client before use. For GitHub CLI, `gh api user --jq .login` must return exactly `pho-veteran`. If necessary, run `gh auth switch --hostname github.com --user pho-veteran`, then verify again. Recheck immediately before every remote write. If other tasks share the CLI, pin this account's credentials in the command process using the CLI credential interface; never print tokens.
- Verify connector and browser identities independently. A verified CLI session does not establish their identity. Use a separately verified client when another client's identity is wrong or unavailable.
- Before pushing, verify the remote URL and permission to update the target branch, including maintainer edits on a contributor's PR branch. Use verified CLI credentials rather than cached Git credentials, for example `git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push <verified-remote> <branch>`.
- Before committing, verify that the Git author name/email belong to `pho-veteran`; use command-scoped overrides when needed without changing global Git settings.
- After a remote write, verify the resulting actor/author and destination where exposed. Stop further writes and report an identity mismatch immediately; do not conceal it or silently republish under another account.
- If `pho-veteran` cannot be verified or lacks access, stop the affected GitHub operation and continue independent local work. Identity verification does not itself authorize publication.

# Repository guidance

- Follow [CONTRIBUTING.md](CONTRIBUTING.md).
- When drafting or updating PR bodies, PR titles or commit messages, use [.agents/skills/contribution-writing/SKILL.md](.agents/skills/contribution-writing/SKILL.md).
- Active project scope, research, UX decisions and EPIC acceptance live in the GitHub system tracker and feature EPICs linked from README.md.
- All product UI, demo copy, accessibility text, generated reports, GitHub issues and EPICs use English. Preserve human-authored input and evidence in their original language.
- The system framework is organized into feature EPICs. Research details inside each EPIC; create task child issues only when that EPIC starts. Do not pre-create the full implementation backlog.
- Keep the runnable prototype and source distinct from unimplemented backend/runtime claims. Do not recreate legacy proposal/research documents as a second planning source.

## Interactive E2E browser

- Prefer the user's already-running Microsoft Edge for interactive E2E verification. Verify that the relay is connected to Edge, open a separate lab tab and preserve unrelated tabs. Use a disposable managed browser only when Edge is unavailable or an isolated profile is explicitly needed; disclose that choice.
- Do not change browser profile permissions or certificate trust without explicit authorization for the target and scope.

## Owned local acceptance environments

- Proceed without repeated confirmation for localhost/Docker setup, interaction, fixture operations, restarts and cleanup within this task's owned local development or acceptance lab. Keep unrelated projects, containers, volumes and host Kubernetes contexts untouched.
- Local-lab authorization does not extend to shared/production targets, real-account changes, private-data disclosure, user-browser permission/trust changes or bypassing a provider safety gate. Preserve explicit target/UID and authority boundaries.

## Implemented foundation boundaries

- The persisted app is the default web entry; `?simulation=1` retains the localStorage simulation. Never promote simulation RCA/actions into API results.
- Backend feature modules own schemas/services/persistence; entrypoints register dependencies. Shared config/database/job mechanics do not import feature policy.
- Regenerate OpenAPI/TypeScript with `scripts/contracts.py --write` in the backend Python environment; do not edit generated transport artifacts by hand.
- Follow README and `docs/foundation.md` for setup, PostgreSQL checks and UI journeys. Test databases must end in `_test`; never run destructive tests against application data.
- Foundation collection is synthetic and bounded. AI diagnosis, live cluster access and execution remain unavailable until their owning EPICs implement them.

## Pull request review and fix loop

- A PR review request includes fixing all confirmed actionable findings locally, including nonblocking findings, rather than stopping at a review report. Preserve unrelated changes and keep fixes scoped to the reviewed PR.
- Use at most two review rounds for the requested task, counting an initial review that has already completed. Round one identifies findings; fix them and run focused behavioral checks before round two.
- Complete the reviewed slice's implementation and visual acceptance before confirmation; distinguish actual browser and live integration evidence from fixtures or source inspection.
- Round two is confirmation-only: map every original finding to addressed or still open, and check regressions caused by those fixes. Do not start a fresh broad audit or introduce an unrelated finding backlog.
- Do not run a third review. If confirmation leaves an original finding open, finish the correction and its focused checks, and disclose that the final adjustment did not receive another independent review.
- Report the final finding dispositions and checks actually run. This loop does not authorize pushing, posting reviews/comments, merging or other publication.
