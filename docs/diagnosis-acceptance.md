# Finite E06 model acceptance

E06 keeps protocol compatibility separate from diagnostic quality. The trusted driver is `services/backend/tests/e06_live_acceptance.py`; ordinary tests exercise it over owned loopback HTTP. CI never launches its live command.

The frozen inputs and independent oracle are in `services/backend/tests/fixtures/e06/`. Only input question and stored telemetry enter context. Oracle labels, expected categories and score thresholds stay outside it. Ten positive variants cover the five families; healthy and injected-log cases use actual inference. The injected case reuses c01's clean input plus hostile text. Required-gap and foreign-workload negatives require zero model calls. Existing controlled quota/unavailable worker tests establish the remaining provider negative without deliberately exhausting a real provider quota.

The campaign uses the exact owner-selected local gateway and two model IDs. The primary's diagnostic responses establish its wire/schema compatibility; the secondary receives only a fixed 64-token probe and remains quality-unqualified. No alias, endpoint fallback or account purchase is performed.

## Owned campaign lab

Use a separately provisioned `cloudops_e06_live_test` PostgreSQL database, disposable operator/viewer accounts and trusted dispatch permit. Set `E06_OWNED_LAB=e06-r5`, `DATABASE_URL`, normal foundation configuration and `JOB_DISPATCH_ROOT`. Keep the credential only in the worker process as `DIAGNOSIS_LIVE_TOKEN`. The API may read secret-free profile/egress/qualification files without this environment variable. Do not start a separate background worker in this finite campaign lab.

After committing and checking the driver/input/oracle, freeze once:

```sh
uv run --project services/backend python services/backend/tests/e06_live_acceptance.py freeze --state-dir /path/to/private-campaign
uv run --project services/backend python services/backend/tests/e06_live_acceptance.py run --state-dir /path/to/private-campaign
```

The private ledger binds source/input/oracle hashes, requested/configured identity, every attempted call, normalized initial outcome, response digest, nullable usage, timing, final case result and exact resolved support targets. Invalid non-JSON text is retained by outcome/digest, not copied into public artifacts. The driver refuses a repeated initial cohort, other database/owner, concurrent invocation or changed frozen inputs. Interrupted/unsettled work is not automatically replayed.

The global limit is **15 requests**, including failed/unknown outcomes and at most **one repair across the whole campaign**: 12 primary evaluation cases, one primary operational smoke and one secondary probe, plus that reserve. The primary output cap is 1,024 tokens; inputs are bounded to 32 KiB. It stops before another call once observed usage reaches 60,000 tokens. Missing usage stays unavailable: this is a request/output bound and observed-usage stop, not an exact billing guarantee. A transport/configuration failure or persistent initial schema failure stops the campaign before repeating it across all cases.

## Score, qualify and inspect

Retain all initial failures and abstentions. Review semantic correctness and the relevance of exact cited values against the frozen oracle, separately from structural acceptance. The gate is at least 8/10 positives, at least 1/2 per family, all four Pending/ImagePull cases observed/manual-only, all five negative/control outcomes safe, and zero invalid scope/citation/template promotion. This small controlled set establishes no production accuracy or live cluster causality.

Only after a passing primary cohort may the deployment owner install `DiagnosisQualification/v1` with that exact primary identity, cohort ID and evidence reference. Then the `smoke` phase uses the same bounded worker path on a fresh operational Run; evaluation artifacts are never adopted as operational results. It requires completed evaluation and exact trusted qualification, counts against the same ledger, and cannot repeat. Keep the secondary unqualified. Changing model/destination/wire/budget/prompt/context/policy invalidates the affected qualification.

Inspect the new stored operational result/citations, Connections identity and export in the persisted UI. Reuse the process/restore and role/session evidence at their actual fingerprints. E07 consumes bounded advice only; E09 may consume stored observations; E10 retains the rules comparator, live traffic/load/recovery campaign and consolidated release qualification.
