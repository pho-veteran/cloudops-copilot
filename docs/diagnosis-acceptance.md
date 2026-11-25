# Finite E06 model acceptance

E06 keeps protocol compatibility separate from diagnostic quality. The trusted driver is `services/backend/tests/e06_live_acceptance.py`; ordinary tests exercise it over owned loopback HTTP. CI never launches its live command.

The frozen inputs and independent oracle are in `services/backend/tests/fixtures/e06/`. Only input question and stored telemetry enter context. Oracle labels, expected categories and score thresholds stay outside it. Ten positive variants cover the five families; healthy and injected-log cases use actual inference. The injected case reuses c01's clean input plus hostile text. Required-gap and foreign-workload negatives require zero model calls. Existing controlled quota/unavailable worker tests establish the remaining provider negative without deliberately exhausting a real provider quota.

The campaign uses the exact owner-selected local gateway and two model IDs. The primary's diagnostic responses establish its wire/schema compatibility; the secondary receives only a fixed 64-token probe and remains quality-unqualified. No alias, endpoint fallback or account purchase is performed.

## Owned campaign lab

Use a separately provisioned `cloudops_e06_gpt_test` PostgreSQL database, disposable operator/viewer accounts and trusted dispatch permit. Set `E06_OWNED_LAB=e06-r5`, `DATABASE_URL`, normal foundation configuration and `JOB_DISPATCH_ROOT`. Keep the credential only in the worker process as `DIAGNOSIS_LIVE_TOKEN`. The API may read secret-free profile/egress/qualification files without this environment variable. Do not start a separate background worker in this finite campaign lab.

The owner selected `gpt-6.1-sol` as primary and `codex/gpt-6.1-terra` as compatibility-only after the retained Kiro failure. The GPT probes passed with `max_completion_tokens=64`; diagnosis uses the same parameter at 1,024. The first Kiro request and both GPT probes count toward the new total of 50.

After committing and checking the driver/input/oracle, freeze once:

```sh
uv run --project services/backend python services/backend/tests/e06_live_acceptance.py freeze --state-dir /path/to/private-campaign --carryover-campaign /path/to/latest/ledger.json
uv run --project services/backend python services/backend/tests/e06_live_acceptance.py run --state-dir /path/to/private-campaign
```

Carryover validates the fixed probe endpoint, exact secondary model, output contract and `OK` marker, retains prior request/usage counts and hashes the original receipt. It does not qualify the secondary for diagnosis.

The private ledger binds source/input/oracle hashes, requested/configured identity, every attempted call, normalized initial outcome, response digest, nullable usage, timing, final case result and exact resolved support targets. Invalid non-JSON text is retained by outcome/digest, not copied into public artifacts. The driver refuses a repeated initial cohort, other database/owner, concurrent invocation or changed frozen inputs. Interrupted/unsettled work is not automatically replayed.

The global limit is **50 requests across the engagement**, including failed/unknown outcomes and at most **one repair across the whole campaign**: 12 primary evaluation cases, one primary operational smoke and that reserve. A passing exact secondary probe may be carried forward without another send. The primary output cap is 1,024 tokens; inputs are bounded to 32 KiB. It stops before another call once the observed token lower bound reaches 170,000: the larger of reported total or known prompt plus completion counts. Nullable original fields remain unchanged. Missing usage stays unavailable: this is a request/output bound and observed-usage stop, not an exact billing guarantee. A transport/configuration failure or persistent initial schema failure stops the campaign before repeating it across all cases.

## Configured campaign for a new primary model

A new model, endpoint, reasoning effort or output limit is a new deployment identity. Earlier qualifications do not apply to it. To qualify such a primary without editing the driver, pass a trusted, secret-free `E06CampaignConfig/v1` with `--campaign-config` to every phase (`freeze`, `run`, `smoke`).

```json
{
  "schema": "E06CampaignConfig/v1",
  "cohort": "<new cohort id>",
  "profile": "profile.json",
  "authorization_id": "<owner authorization>",
  "budget_policy_id": "<budget policy>",
  "owned_database": "<owned database ending in _test>",
  "owned_lab": "<E06_OWNED_LAB value>",
  "max_requests": 24,
  "max_repairs": 4,
  "observed_token_stop": 400000,
  "max_reserved_usd": 1.0,
  "input_usd_per_mtok": 0.10,
  "output_usd_per_mtok": 0.50,
  "rates_date": "YYYY-MM-DD",
  "prior_receipts": ["probe-receipt.json"]
}
```

- **Profile.** `profile` names a trusted `DiagnosisModelProfile/v1`; a relative path resolves against the config directory. The profile must reference the worker-only `DIAGNOSIS_LIVE_TOKEN`. The driver copies it in `evaluation_only` mode, or `operational` mode for `smoke`, with the config's authorization and budget IDs. It keeps the frozen cases, oracle, validator rules and lifecycle unchanged.
- **Cohort scope.** The configured campaign qualifies one primary only. It has no secondary probe and no carryover.
- **Request and spend bounds.** Each reservation counts the profile's `max_input_bytes` as tokens plus its full output limit, which also bounds reasoning tokens, at the dated rates. A reservation that would exceed `max_reserved_usd` is refused before sending.
- **Ledger.** The ledger records:
  - the config, profile and prior-receipt hashes;
  - the reserved spend;
  - the cost estimated from observed usage.

  `billed_usd` stays `null`, because billed charges come only from the provider's billing records.
- **Change protection.** A cohort runs only with the exact config and unchanged profile it was frozen with.

For an owner-authorized gateway without published prices, set `pricing_available: false`.
The numeric reservation fields remain required as nominal accounting units; they are
not prices or a monetary spending guarantee. The ledger records `rates`, `estimated_usd`
and `billed_usd` as `null`, including after observed usage. Request and observed-token
stops still apply. Obtain explicit owner authorization for this accounting mode.
Some gateways ignore output-limit parameters; a requested completion bound does not
establish a remote token or cost cap on those routes. Preserve that limitation with
each campaign's configuration and receipts.

Without `--campaign-config`, the driver keeps the original Sol/Terra local-gateway campaign described above.
## Score, qualify and inspect

Retain all initial failures and abstentions. Review semantic correctness and the relevance of exact cited values against the frozen oracle, separately from structural acceptance. The gate is at least 8/10 positives, at least 1/2 per family, all four Pending/ImagePull cases observed/manual-only, all five negative/control outcomes safe, and zero invalid scope/citation/template promotion. This small controlled set establishes no production accuracy or live cluster causality.

Only after a passing primary cohort may the deployment owner install `DiagnosisQualification/v1` with that exact primary identity, cohort ID and evidence reference. Then the `smoke` phase uses the same bounded worker path on a fresh operational Run; evaluation artifacts are never adopted as operational results. It requires completed evaluation and exact trusted qualification, counts against the same ledger, and persists its start before admission so a post-send read failure cannot enable a repeated smoke. Keep the secondary unqualified. Changing model/destination/wire/budget/prompt/context/policy invalidates the affected qualification.

Inspect the new stored operational result/citations, Connections identity and export in the persisted UI. Reuse the process/restore and role/session evidence at their actual fingerprints. E07 consumes bounded advice only; E09 may consume stored observations; E10 retains the rules comparator, live traffic/load/recovery campaign and consolidated release qualification. E10 S4 retains the evaluation-only rules comparator and sealed scoring contract in [evaluation-baseline.md](evaluation-baseline.md); the finite live cohort and scores remain owned by S6.

## Retained prompt-contract failure and correction

The first GPT cohort at `31bd28a6bc3453a1fd1d1e1ef74fb276d1bbdb87` stopped after c01 and its single repair: both HTTP responses were valid JSON but used an invented wire shape. The strict validator rejected them; neither became operational findings. The trusted prompt had named `RCAAdvice/v1` without supplying its schema. The failure remains in `docs/evidence/e06-r5/gpt-initial.json`.

Prompt `e06.rca_prompt/v2` supplies the JSON Schema from the existing validator, requires manual next steps and clarifies family-specific sufficiency. It changes no oracle labels or validator rules. A new frozen cohort uses `--carryover-campaign /path/to/previous/ledger.json` instead of `--carryover-probe`; it carries every prior request, observed usage, missing usage and spent repair. The second probe is reused.

The v2 initial evaluation reached an observed 63,118-token lower bound after ten positive calls, activating its original 60,000 stop before the two live negatives. Nine positives passed structural validation; c05 failed a ruled-out citation requirement and remains failed. `--continue-cases --carryover-campaign /path/to/v2/ledger.json` retains all sent cases, including failures, and sends only previously uncalled cases under the unchanged deployment/input/oracle identity. The continuation raises the global observed-token stop to 170,000, preserves the 50-request ceiling and spent repair, and reserves one fresh operational smoke. The initial phase remains in `gpt-v2-initial.json`; no positive case is rerun.

The v2 continuation sent exactly two previously uncalled negatives (17 engagement requests, observed lower bound 76,189). Injection passed; healthy abstained semantically but failed strict ruled-out citation coverage. Therefore v2 remains unqualified. Prompt `e06.rca_prompt/v3` now states the existing ruled-out coverage/source rules, citation classes and candidate mapping. A new complete frozen cohort, with no reused v2 quality results, is bounded to 12 primary cases and one operational smoke; the shared repair remains spent. The global observed-token stop is 170,000 to cover this explicitly bounded correction, still within 50 requests. The validator, case inputs and independent oracle remain unchanged.

## Qualified outcome

The [acceptance ledger](evidence/e06-r5/README.md) records the complete v3 cohort and installed API/Edge proof:10/10 positives,2/2 each family,4/4 Pending/ImagePull manual-only,5/5 safe negatives/controls, zero invalid scope/citation/template promotions and a completed fresh operational smoke. The exact Sol tuple is qualified; Terra remains compatibility-only. The engagement is complete at30/50 model requests and151,036 observed tokens as a lower bound, with one original unavailable-usage request. Do not send further calls automatically. Any later cohort must carry the latest ledger, preserve all original outcomes and record a new bounded allowance before execution.
