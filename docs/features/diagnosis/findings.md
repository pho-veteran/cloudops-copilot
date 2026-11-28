# Inspect stored diagnosis

The persisted incident workspace reads the selected Run's analysis. Findings distinguish supporting and contradictory telemetry, alternatives, ruled-out hypotheses, unverified checks, missing evidence, limitations and advisory next steps. A candidate template is advice only. Recovery still requires its own current target checks and explicit approval.

Evidence links retain the exact Run, evidence ID and semantic locator. Runbook links retain the analysis selection, document version and chunk; runbooks provide methodology rather than factual evidence. Expired content remains explicitly unavailable. A newer failed or waiting Run has its own status and links to earlier completed Runs in the loaded history; older history remains available through pagination.

An Operator may explicitly resume an eligible quota hold. The server's earliest retry, original deadline and used budgets remain binding. Cancelling is available only while the server says no call is in flight. Neither resume nor connection checks replay automatically after sign-in. Viewers read and export stored data.

Connections displays the configured endpoint label/model, qualification identity, dispatch state and recent stored checks. Transport success, fixed-probe schema success and diagnostic quality are separate. Missing usage is unavailable; observed tokens are not remaining quota. Configuration and worker credentials remain host-managed; this UI accepts no endpoint or credential.

New resolutions save `HandoffSnapshot/v3` with persisted operational results, exact telemetry/runbook references and provenance. Export and two-Run comparison perform no inference. Evaluation artifacts are excluded. Existing legacy/v1/v2 snapshots and stored Markdown remain unchanged.

## Verification

[Browser evidence](../../evidence/e06-r4-ui/acceptance.json) records the owned PostgreSQL/API/worker/Edge journey using synthetic telemetry and a controlled loopback provider. These fixtures establish UI integration, not live provider quality; #72 owns that gate.
