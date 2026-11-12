# E06 R1 model client evidence

These receipts belong to issue #68 only. They exercise a provider-independent transport against an owned loopback fake server, a real local HTTP/TLS server and `MockTransport`. No external endpoint, account, model key or spend was used.

## Files

- `wire-capture.json` — actual local-server request path/headers/body with the synthetic credential redacted, plus the normalized text result.
- `failure-matrix.json` — a selected `CallKind` matrix exercised through `MockTransport` with observed status and retry hint.
- `9router-observation.json` — external observation of a pre-existing local router; **not** compatibility evidence. It records why 9router is not a runtime/CI dependency.
- `checks.json` — commands and results bound to the source commit.

## Proof classes

- Profile, message and request-shape tests: pure Python, no I/O.
- `MockTransport` matrix: in-process HTTP semantics, no socket.
- Local server: actual HTTP path, headers, body, timeout, redirect, compression and proxy behavior.
- TLS: local certificate generated with OpenSSL; verification failure and explicit CA success are proven by `test_diagnosis_transport.py`, not a receipt file.
- 9router: manual external observation only, never claimed as product compatibility or quality.

## Limits

- No live model, account, quota, cost, latency or diagnostic-quality claim.
- Remote enforcement of `max_tokens`/`max_completion_tokens` cannot be proven by the client; #72 owns live qualification.
- The local TLS certificate and proxy recorder are test fixtures, not deployment topology.
