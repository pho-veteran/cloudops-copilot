---
name: contribution-writing
description: Draft or update CloudOps Copilot PR bodies, PR titles and commit messages using the repository conventions. Use when preparing contribution text; not for EPIC decomposition or code review.
---

# Contribution writing

This skill contains the complete writing conventions needed for commits and PRs. Inspect the relevant diff and task context so the text describes the final change, not an abandoned plan. For a commit message, inspect the intended commit's diff; for a PR, inspect the complete branch diff against its base. Do not infer finished work from a title or planning checklist.

## Commit messages and PR titles

Use `type(scope): description`, or `type(scope)!: description` for breaking changes. Allowed types are `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `style` and `revert`. Require a lowercase scope naming the affected feature/module, such as `incidents`, `auth`, `contracts`, `web` or `repo`; it is not a contributor name or issue number. Write a concise English description.

- Describe the concrete result in concise English: `fix(auth): reject expired sessions`, not `fix(auth): address feedback`.
- Match the type to the actual diff: `feat` adds behavior, `fix` corrects behavior, `docs` changes documentation, and `test` changes tests. Do not label a documentation-only delivery as an implemented feature.
- A small change can use a subject alone. When useful, add a blank line and explain the trigger/problem, changed behavior and reason for the approach. Avoid file-by-file narration or a transcript of the work.
- Explain incompatible changes and migration steps; use `!` and/or a `BREAKING CHANGE:` footer as appropriate. Do not invent a version bump or release claim.
- Add `Refs: #123` when a real linked issue helps. Use closing keywords only when the full issue acceptance is met; a partial feature slice must not close its EPIC.

Example message for an implemented fix:

```text
fix(incidents): retain the selected run after refresh

The incident page reset to the newest run when refreshed, so users lost
the evidence snapshot they were inspecting. Restore the selection from
the route and show an explicit unavailable state if that run is missing.
```

Do not claim this example exists in the code. Adapt messages to inspected changes. Do not rewrite existing commits or add commit hooks/CI merely to enforce this writing convention.

## PR body

Use this structure, with detail proportional to the change:

1. **Summary:** concrete problem and resulting behavior, with a short before/after example when useful.
2. **Changes:** the few implementation decisions a reviewer needs. Explain affected contracts, consumers, migrations or UI states only when relevant. Link the EPIC with `Related to` or `Refs`; reserve `Closes` for fully completed task scope.
3. **Verification:** checks actually performed, their results, and material limits. For UI work, link the relevant journey/screenshots; for persistence or API changes, provide the affected behavior evidence. A complex integration may benefit from a flow/result table; a small documentation change does not need one.
4. **Checklist:** mark only demonstrated items. Update affected docs/contracts where applicable. Remove irrelevant boilerplate rather than filling the body with repeated “N/A”.

Separate fixture checks from live integration proof. A failed or skipped check stays explicit, with its remaining gate; a passing build does not establish a working user journey. Summarize relevant evidence and link detailed sanitized artifacts when needed, rather than pasting tool/session logs. Do not add assistant/model reviewer names, approval badges or review-session metadata.

When scope changes, rewrite the title and body around the final implementation. Preserve material unfinished acceptance and link follow-up work without automatically creating new tasks. Use English for authored project text and preserve quoted human evidence in its original language. Never invent checks, issue IDs, owners or screenshots.

## Applying the text

Drafting text alone does not authorize committing, pushing, publishing, closing issues or requesting reviews. Use authorization already present in the task; do not ask again for an authorized update. When using `gh` for a multiline body, write UTF-8 text to a temporary file and pass `--body-file`. Re-read published content to confirm it matches. After an uncertain write, inspect the current state before retrying.
