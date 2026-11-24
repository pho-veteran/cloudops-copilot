# Explicit diagnosis reasoning effort

The trusted `DiagnosisModelProfile/v1` accepts an optional `reasoning_effort`:
`none`, `low`, `medium`, `high`, `xhigh` or `max`. Provider/model support must be
verified separately. Unsupported settings are not retried or silently downgraded.

For a provider supporting this Chat Completions setting, configure it alongside
the exact model and API root:

```json
{
  "model": "gpt-6-luna",
  "base_url": "https://api.openai.com/v1",
  "reasoning_effort": "max",
  "output_limit": {"param": "max_completion_tokens", "value": 4096}
}
```

This fragment is not a complete or qualified profile. Store the complete profile
and egress policy outside the checkout. Keep credentials in a separate worker-only
environment file using the existing `compose.diagnosis.yaml` overlay. Do not mount
the credential directory into the API service or include secrets in a profile.

Explicit effort participates in the behavior digest and serialized request byte
bound. Changing effort therefore invalidates a previously qualified identity.
Omitting effort preserves the original request body and behavior digest.

Keep a new deployment disabled until a finite, owner-authorized compatibility
probe succeeds. Compatibility alone does not qualify RCA: the exact endpoint,
model, effort, output budget, prompt and policy tuple needs the existing E06
diagnosis acceptance process before operational use. A metadata lookup does not
prove inference access. An error with unavailable usage must retain an unknown
cost rather than being reported as zero.

## Provider rejection detail

`ModelClient(..., capture_provider_error=True)` keeps a bounded `ProviderError` on a 4xx or 5xx response, so an owner-authorized probe can explain a rejection. It is off by default. The product worker never enables it, and product receipts and APIs never store or return it.

- It keeps at most 8 KiB of an identity-encoded body and stops requesting chunks at that cap, so at most one transport chunk beyond the cap is consumed (`body_bytes_read`). A body that reaches the cap, a compressed body, or a read that runs out of the remaining call deadline is marked `body_truncated` and left unparsed. The rejection status is kept either way.
- Only the OpenAI-style `error.type`, `error.code` and `error.param` identifiers are kept, plus `error.message` and the `x-request-id` header.
- `error.message` is limited to 512 printable characters. The configured credential, `sk-` keys, bearer values, `key=value` secrets, email addresses and long token-like strings are replaced with `[redacted]`.
- An identifier that would need redaction is dropped.
- The raw body, the request body and the `Authorization` header are never retained.

## Connectivity probe

`scripts/probe-diagnosis-connection.py` sends exactly one fixed request ("Reply with exactly OK.") through the client, with provider-error capture enabled.

```sh
uv run --project services/backend python scripts/probe-diagnosis-connection.py \
  --profile /private/diagnosis/profile.json --secret-env /private/secrets/diagnosis.env \
  --reservation-dir /private/diagnosis/probe-<date>-<n> \
  --authorization-id <owner-approval> --budget-policy-id <policy> --ceiling-usd 0.10 \
  --input-usd-per-mtok <dated input rate> --output-usd-per-mtok <dated output rate> \
  --rates-date <YYYY-MM-DD>
```

- **Profile mode.** The probe sends in `evaluation_only` mode with the supplied authorization and budget IDs, whatever mode the stored profile has. These IDs are part of the probe's call identity.
- **Reservation.** Before the request is sent, a reservation is recorded in a new private directory. An existing directory is refused, so a probe is never retried.
- **Cost bound.** The reserved cost counts every request byte as one token and adds the full output limit, which also bounds reasoning tokens. A reservation above the ceiling is refused before anything is sent.
- **Receipt.** The private `receipt.json` contains:
  - the outcome, status, provider error, request ID, served model and usage;
  - the estimated cost from observed usage at the supplied dated rates.

  `billed_usd` stays `null`, because billed charges come only from the provider's billing records. The credential is never printed or written.
- **Exit status.** The probe exits with 0 only for an exact `OK` response from the requested model. A passing probe is compatibility evidence only; it is not RCA qualification.