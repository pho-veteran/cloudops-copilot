# Project conventions

## Language

Use **English** for all product-facing content: screens, user stories, buttons, forms, placeholders, help text, status/error messages, accessibility labels, demo fixtures and generated reports.

Use **English** for GitHub issue and EPIC titles, descriptions, acceptance criteria, checklists and ongoing discussion. Preserve identifiers and technical terminology. Human-authored input and evidence are not automatically translated.

The team may discuss work in Vietnamese. Existing research/proposal documents may remain in their current language; quoted UI labels must match the English interface.

## Planning gate

Keep UI/UX verification in issue #1 and proposal verification in issue #2. Do not create implementation EPICs until the user accepts the shared proposal/prototype baseline and the readiness gate is satisfied.

## Prototype verification

Run commands from `apps/web`:

```sh
npm ci
npm run dev
npm test
npm run build
```

The prototype uses local fixtures. Preserve the distinction between incident lifecycle, investigation status and execution outcome, and do not describe simulations as live cloud operations.
