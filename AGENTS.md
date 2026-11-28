# GitHub identity — mandatory for this repository

- Use only `pho-veteran` for authenticated GitHub operations on this repository and its worktrees, including reads, pushes, pull requests, comments, reviews, workflow actions and merges. Never fall back to another account.
- This requirement applies to the authenticated client and acting GitHub account, not contributor credit. Preserve original commit/PR authorship. A squash or merge commit may retain the PR author and use GitHub `web-flow` as committer; that is expected and is not an identity mismatch. Do not change authorship or request confirmation solely for that reason.
- Verify the actual client before use. For GitHub CLI, `gh api user --jq .login` must return exactly `pho-veteran`. If necessary, run `gh auth switch --hostname github.com --user pho-veteran`, then verify again. Recheck immediately before every remote write. If other tasks share the CLI, pin this account's credentials in the command process using the CLI credential interface; never print tokens.
- Verify connector and browser identities independently. A verified CLI session does not establish their identity. Use a separately verified client when another client's identity is wrong or unavailable.
- Before pushing, verify the remote URL and permission to update the target branch, including maintainer edits on a contributor's PR branch. Use verified CLI credentials rather than cached Git credentials, for example `git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push <verified-remote> <branch>`.
- Before creating a new local commit, verify that its author name/email belong to `pho-veteran`; use command-scoped overrides when needed without changing global Git settings. This does not require rewriting contributor authors or changing GitHub squash/merge authorship.
- After a remote write, verify the authenticated acting account and destination where exposed (for merges, check `mergedBy`/`merged_by`). Stop further writes and report an identity mismatch only if the action used a different account or an unintended destination; do not treat preserved contributor authorship or GitHub `web-flow` as a mismatch.
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
- Follow README and `docs/guides/foundation.md` for setup, PostgreSQL checks and UI journeys. Test databases must end in `_test`; never run destructive tests against application data.
- Foundation collection is synthetic and bounded. AI diagnosis, live cluster access and execution remain unavailable until their owning EPICs implement them.

