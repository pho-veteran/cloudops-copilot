# Bounded diagnosis model client

`features/diagnosis` owns one provider-independent transport for a non-streaming OpenAI-compatible chat-completions call. It does not expose an API or worker caller, persist anything, build context, validate RCA or retry/repair. Those responsibilities belong to #69, #70 and #72.

## Profile — `DiagnosisModelProfile/v1`

A trusted deployment supplies a secret-free JSON profile. The credential is referenced by environment variable name; the secret never belongs in this file.

```json
{
  "schema": "DiagnosisModelProfile/v1",
  "config_id": "example-evaluation",
  "config_version": "1",
  "mode": "evaluation_only",
  "endpoint_label": "example-invalid",
  "base_url": "https://model.example.invalid/v1",
  "allow_insecure_loopback": false,
  "model": "replace-with-a-free-model-id",
  "credential": { "kind": "env", "name": "DIAGNOSIS_MODEL_TOKEN" },
  "tls": { "ca_file": null },
  "proxy": null,
  "limits": {
    "max_input_bytes": 65536,
    "max_response_bytes": 262144,
    "connect_timeout_seconds": 3,
    "read_timeout_seconds": 10,
    "pool_timeout_seconds": 2,
    "deadline_seconds": 30
  },
  "output_limit": { "param": "max_tokens", "value": 800, "omit_policy_id": null },
  "authorization_id": "replace-with-deployment-authority",
  "budget_policy_id": "replace-with-budget-policy"
}
```

See `config/diagnosis-model.example.json` for a secret-free starting point.

Rules:

- `mode=disabled` performs no network call and does not read the credential. A missing profile path is treated as unconfigured.
- `evaluation_only` and `operational` are transport modes only. #68 does not grant qualification; #70/#72 own that.
- `base_url` is the API root. Exactly one `chat/completions` is appended. An existing `/v1`, `/v1beta` or `/api/v1` path is retained; no second `/v1` is added.
- HTTP is allowed only for an explicitly flagged loopback host (`localhost`, `127.0.0.1`, `::1`). External endpoints require HTTPS.
- TLS verification cannot be disabled. `tls.ca_file` replaces the trust store with a deployment CA.
- `proxy` is the only proxy that can be used. Ambient `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` settings (and netrc) are ignored (`trust_env=False`).
- `output_limit.param` is exactly one of `max_tokens`, `max_completion_tokens` or `omit`. `omit` requires an explicit `omit_policy_id`; the transport cannot verify remote enforcement.
- `limits` are hard bounds. Input is measured after UTF-8 JSON serialization; response bytes are measured after decoding.
- `settings()` in shared config is not involved and remains free of diagnosis policy.

## Call result

`ModelClient.send(messages, before_send=...)` is the synchronous facade used by the worker. `await ModelClient.asend(...)` is the async form. Calling `send()` inside an existing event loop raises and directs the caller to `asend()`.

The admission callback is required and runs once, after request serialization and immediately before network access. If it raises, the exception propagates and no request is sent. It is synchronous by contract; a blocking callback is the caller's responsibility and is outside the transport's cancellable deadline.

Every HTTP/transport outcome returns a `ModelCall` instead of raising. Invalid serialized input returns `input_rejected`; an unusable profile/CA returns `config_invalid`; neither has made a network call. `CallKind` distinguishes text, truncation, refusal, tool output, unsupported shape/finish, invalid envelope/content type/encoding, timeout, transport error, auth denial, rate limit, upstream error, rejection, redirect, oversized response, credential failure, input/config rejection and unconfigured state.

`kind` is the authoritative outcome. `error_code` is a local echo of `kind` for failure states and is `null` for the semantic text/truncation/refusal/tool outcomes; it is never copied from a provider body.

Usage is never invented:

- absent/null → `usage=None`, `usage_status="unavailable"`;
- malformed/bool/negative → `usage=None`, `usage_status="invalid"`;
- valid → `usage_status="observed"`.

`Retry-After` is parsed from the complete header as nonnegative integer seconds (or HTTP-date), clamped to 0..86400. Only its printable display value is truncated to 64 characters. The transport never retries, rotates credentials/models or follows redirects.

The transport creates one `AsyncClient`, one `SSLContext` and one event loop per call. It does not promise cross-call connection pooling; `pool_timeout_seconds` is still applied as a bounded phase setting but is not expected to be contended. Usage with only some token fields is `observed` with the absent fields set to `null`; `observed` does not mean every field is present.

## Guarantees and non-guarantees

Guaranteed:

- the path, configured headers and body built by this client; exactly one selected output-limit parameter;
- no implicit retry, redirect or environment proxy;
- certificate verification on; no profile field disables it;
- bounded serialized input and response body; responses with a non-identity content encoding are rejected before reading or decompressing their body;
- whole-call deadline, including header and body trickle (except a blocking synchronous admission callback);
- no upstream error body, provider text or credential in the returned classification. The one exception is opt-in: trusted diagnostic tooling may construct `ModelClient(..., capture_provider_error=True)` to keep a bounded `ModelCall.provider_error` (see [reasoning effort and connectivity probes](diagnosis-reasoning-effort.md)). The product worker never enables it, and its receipts never store it.

The default trust store can be influenced by process-level `SSL_CERT_FILE`/`SSL_CERT_DIR`; deployments that require a fixed root must set `tls.ca_file`. A relative `tls.ca_file` is resolved against the profile file loaded by `ModelProfile.load`.

Not guaranteed:

- that a remote endpoint honors the declared output limit;
- token/cost accounting or quota;
- provider error-code semantics;
- model quality or diagnostic correctness;
- exactly-once remote execution.

## Testing

The tests use an owned loopback fake server, not a model account:

```sh
uv run --project services/backend pytest -q services/backend/tests_unit/test_diagnosis_profile.py \
  services/backend/tests_unit/test_diagnosis_messages.py \
  services/backend/tests_unit/test_diagnosis_client.py \
  services/backend/tests_unit/test_diagnosis_transport.py
```

Coverage includes request shape, all normalized outcomes, zero-call paths, real redirects/timeouts/trickle/oversize/compression/proxy behavior and a real TLS endpoint generated with local OpenSSL. The tests require no database, credential, external network or spend.

## 9router

A local 9router gateway was probed only as an external observation. It is not a dependency, default profile or compatibility claim. Observed issues: it injects a large hidden instruction block when no system message is present, ignores `max_tokens`/`max_completion_tokens`, returns refusals as HTTP 200 with `finish_reason="failed"` and zero usage, and answers `/v1/models` without authentication. The non-standard refusal and SSE/content-type behaviors are encoded as negative fixtures; the hidden-prompt and unauthenticated-models observations remain design notes in `9router-observation.json`. No live gateway is required by CI.

## Evidence

Sanitized receipts live in `docs/evidence/e06-r1-model-client/`. They distinguish generated fixtures, a local HTTP server and the out-of-repo router observation. TLS verification is proven by `test_diagnosis_transport.py::test_tls_verification_and_ca_override`; it is not a receipt file. No real endpoint, model key or diagnostic quality is claimed.
