# Configured source checks

On Connections, Operators can check Kubernetes, Prometheus, the application database, and the worker. Viewers can inspect the same saved results. Opening or refreshing the panel reads saved results and the current deployment configuration.

| API | Behavior |
| --- | --- |
| `POST /api/connection-checks` | Accepts only `component`. Requires an Operator session, the configured Origin, and a CSRF token. An optional UUID `Idempotency-Key` identifies an Operator's intent and returns the existing attempt on retry. |
| `GET /api/connection-checks` | Returns up to 50 receipts, newest first. Receipts whose binding differs from the current workload and configuration remain visible as historical. |
| `GET /api/connection-checks/status` | Returns a bounded assessment for the four components. Assessment time, observation time, and attempt and outcome times are separate fields. |

Admission is serialized briefly in PostgreSQL. Each component allows one in-flight attempt and a 60-second cooldown from admission, including for callers without an idempotency key. The attempt commits before the provider read. Settlement checks the captured UID and configuration again. An interrupted attempt expires at its deadline, and a retry with the same key cannot issue another read. The browser keeps the current attempt's retry key across navigation and delayed replies.

Provider checks have an eight-second wall deadline and make at most one request. The deadline includes provider configuration validation and TLS setup. The service prepares provider configuration before acquiring the admission lock. If preparation exhausts the budget, the API records a timeout without sending a request. Kubernetes reads one exact `apps/v1` Deployment using E02's bounded transport. Prometheus uses E04's configured HTTPS client, accepts read configuration files up to 512 KiB, and evaluates the `memory_working_set` slot over the latest four configured steps. The response must bind the selected workload's namespace and UID. Query success, coverage, and raw sample freshness have independent fields. Range points carry evaluation timestamps, so raw freshness remains `unknown`. A successful explicit source check ages out after five minutes. The worker uses the existing heartbeat threshold. The status endpoint assesses the persisted scope and outcomes using one read-only database snapshot.

An API restart with Prometheus selected and a missing or invalid read configuration preserves access to saved receipts. Connections shows `config_missing` or `config_invalid`. The worker requires a valid configuration before it starts collection.

Migration `0019_connection_checks` preserves previous IDs, timestamps and receipt text. Existing unbound receipts stay historical. Downgrade refuses to discard newly recorded attempt evidence.

## Verification

The [local acceptance receipt](evidence/connections105/acceptance.json) records the owned K3s and Prometheus lab results, stopped scrapes, restart continuity and browser behavior. K3s used a ServiceAccount with `get` on one named Deployment. Caddy permitted the Prometheus range endpoint using a scoped bearer credential and its own trusted CA. The test series came from Prometheus self-scrapes labeled with the lab Deployment UID. The follow-up tests verify the deadline during TLS setup and API restart with broken Prometheus configuration against the current source.

The PostgreSQL and controlled-provider checks are in `services/backend/tests/test_connection_checks.py`. Use the dedicated `_test` database and [backend test setup](foundation.md) for authorization, idempotency, cooldown, overlapping requests, migration, and provider fault cases. Run `scripts/contracts.py` and `scripts/test-contract-drift.py` in the backend environment to verify the generated contract.
