# Diagnosis engine: local environment and loop mechanics

Part of the [engine improvement loop](README.md).

## Environment

| Tier | Purpose | Setup |
| --- | --- | --- |
| T0 offline | Inner loop, unit tests, replay | Host Python 3.13 with `uv sync --project services/backend --frozen`. A PostgreSQL test container is needed only for the backend DB tests (`TEST_DATABASE_URL` must end in `_test`) |
| T1 lab | Case generation | `manage-local-lab.py --project cloudops-engine-lab --directory <lab>` with `preflight` then `start`; Operator account via the CLI. Ceiling 8.5 GiB RAM, 8 CPU, 20 GB disk. Diagnosis stays disabled in the lab |
| T2 model | Replay calls | Local OpenAI-compatible proxy; see [model endpoint](#model-endpoint) and [model call policy](#model-call-policy) |
| T3 cloud | Final demo only | `deploy/ec2` under separately approved budget; not part of the loop |

Dev-lane results are labelled `dev, non-qualifying`. They never substitute for E06
qualification or E10 measurement.

### Reference host

Observed on the loop machine on 2026-11-26. Recheck before starting the lab.

| Item | Value |
| --- | --- |
| OS / Docker | Linux, Docker Engine 29.9.2, cgroup v2, overlayfs |
| CPU / RAM / free disk | 12 CPU, 16 GB RAM (about 10 GB available with unrelated projects running), 54 GB free |
| GPU | None. A local model is not a quality option; use an API endpoint |
| uv | Not on `PATH`. A pinned binary exists at `~/.cache/cloudops-acceptance/uv-bin/uv` |
| Port 5173 | May be held by an unrelated project. The engine lab uses HTTPS on 8443 and does not need 5173 |

Known local issues:

- The default `~/.docker/config.json` uses `credsStore: desktop`, which is missing on
  this host, so public image pulls fail. Use a private Docker config, for example
  `DOCKER_CONFIG=<dir>` containing `{}`. The lab driver already uses its own empty
  config.
- `scripts/check-backend.sh` runs pytest inside the backend image. That image does not
  contain `runbooks/`, `scripts/`, the Compose files, `apps/web/Dockerfile.https` or
  `git`, so 9 modules fail at collection. Run the suite on the host instead:
  `TEST_DATABASE_URL=postgresql+psycopg://...@<db>:5432/cloudops_test uv run --project
  services/backend --frozen pytest -q services/backend/tests_unit services/backend/tests`.
  This is how CI runs it. On `31b9a1c` the host path passed all affected modules.
- Keep the lab and any test database in their own Compose project names. Do not reuse
  `cloudops-local` or the retained `cloudops-local-testing-lab` volumes.

## Model endpoint

| Item | Value |
| --- | --- |
| Base URL | `http://localhost:20128/v1` (local proxy route; use `127.0.0.1` in a `DiagnosisModelProfile/v1` with `allow_insecure_loopback: true`) |
| Requested model | `cmd/deepseek/deepseek-v4.1-flash` (served as `deepseek/deepseek-v4.1-flash`) |
| Variables | `ENGINE_MODEL_BASE_URL`, `ENGINE_MODEL_NAME`, `ENGINE_MODEL_API_KEY` |
| Credential file | `~/.local/state/cloudops-copilot/engine-loop/model.env`, mode 0600, outside Git. Load with `set -a; . <file>; set +a`. Never print, log or commit the key |

Probe results (2026-11-26, 13 requests in total):

| Probe | Result |
| --- | --- |
| `GET /models` | HTTP 200, target model listed |
| Tiny prompt without a system message | HTTP 200, but 7,589 prompt tokens: the route injects about 7.4k tokens of its own prompt when no system message is present. Always send a system message (with one, the same tiny prompt costs 45 tokens) |
| Reasoning controls (`reasoning_effort`, `reasoning`, `thinking`, `chat_template_kwargs`) | All ignored. The model always reasons (about 85-105 reasoning tokens even for a yes/no question) |
| Product `ModelClient` with the B1 `oom-positive` fixture context (4,912 + 17,070 bytes, 5,913 prompt tokens) | `max_tokens` 1,500: truncated twice, once with `response_format: json_object`. `max_tokens` 5,000: truncated, all 5,000 tokens spent on reasoning. `max_tokens` 16,000: finished in 45 s, 3,990 completion tokens of which 3,446 were reasoning. The output passed `parse` and `validate_structural`: `supported`, `oom_restarts`, `set_memory_profile`, 12 cited aliases |
| Truncation behaviour | When truncated, `content` contains the reasoning text, so `parse` reports `json_invalid` |
| Determinism | Reasoning length varies between identical requests at `temperature: 0` (one run finished in under 4,000 tokens, another exceeded 5,000) |
| Prompt cache | Repeated prefixes are reported as cached (5,632 of 5,913 tokens on a repeat) |

Implication: reasoning tokens dominate cost and latency (about 85% of completion tokens
on B1). Shorter, less ambiguous instructions and a compact context are expected to
reduce reasoning, so reasoning tokens per case is a tracked metric.

## Model call policy

There is no fixed cap. The loop continues while the endpoint can be probed, and every
call must earn its cost.

1. **Zero-call first.** Stages A, B and C and their metrics (detector F1, message bytes,
   required-source recall) are evaluated without the model. Call the model only after
   the deterministic gates pass.
2. **Never resend.** Responses are cached by `sha256(model, parameters, messages)`. A
   replay re-sends only the cases whose request digest changed.
3. **Pilot before full.** A new prompt or context variant first runs on a fixed pilot
   set: one dev case per family, plus healthy and injection, 7 cases in total. Run the
   full dev split only if the pilot is not worse. Abort the pilot after 2 regressions.
4. **Output budget.** Use `max_tokens: 8000` and always send a system message. A
   truncated output is a failure (`SCHEMA_FAIL`, reason `truncated`) and is not retried
   automatically.
5. **Repair calls.** The product's one repair call runs only in full-split runs. Pilot
   runs measure first-attempt validity with repair off.
6. **Concurrency.** At most 3 requests in flight.
7. **Self-consistency** (n > 1) only in a late experiment, and only on cases where the
   detector and the LLM disagree.
8. **Accounting.** Record prompt, cached, completion and reasoning tokens, plus latency,
   per call in the run directory and per experiment in the ledger.
9. **Endpoint health.** At session start, send one fixed probe (system message plus
   "Reply with exactly: OK", `max_tokens: 16`). Stop the loop on HTTP 401, 402, 403 or
   429, a quota or credit error, or 3 consecutive 5xx or timeouts. Record the stop in
   the ledger and resume only after a later probe succeeds. Never switch model or route
   to continue.

## Loop mechanics

### Session start checklist

1. `git -C <worktree> fetch origin` and rebase the engine branch on `origin/main`.
2. `uv sync --project services/backend --frozen` in the worktree.
3. Load the model variables and send the fixed endpoint probe ([policy](#model-call-policy) item 9).
4. If new cases are needed, run the lab `status`. Run `preflight` and `start` only when
   the lab is not already running.
5. Pick the latest dataset snapshot and the current best run as the comparison base.

### Three speeds

| Loop | Trigger | Docker | Typical time |
| --- | --- | --- | --- |
| Inner | Change in stages A-E or the prompt | None. Host `uv run` from the worktree | Seconds when deterministic; minutes when the model is called (cached, parallel) |
| Middle | Collector change (what is collected) | Rebuild backend image (~4 s measured), then `up -d --no-deps api worker` | About 20 s plus one verification case |
| Outer | Need for new cases | Lab runs in the background | About 4-5 min per case, serial on one node; an overnight batch |

Do not re-run `manage-local-lab.py start` during iteration. It verifies registries and
host containment and is meant for setup.

### CLI (to build: `scripts/engine-loop.py`)

```text
export   --lab-dir DIR --run RUN_ID --case-id ID --label FILE   # Run -> RawEvidenceBundle
snapshot --from DIR --out datasets/<id>                         # freeze bundles + labels + split
replay   --dataset ID --split dev --config B4 [--variant k=v] [--concurrency 3]
score    --run runs/<run-id>
triage   --run runs/<run-id>                                    # failure tags
compare  --base runs/<a> --head runs/<b>                        # paired diff, McNemar
report   --run runs/<run-id> [--split test]                     # milestone only
```

- **Cache:** key = `sha256(model id, parameters, serialized messages)`. A hit returns
  the stored response and costs nothing.
- **Concurrency:** bounded by the endpoint rate limit and the daily cap. Stop at the
  cap; never retry past it.
- **Determinism:** temperature and seed are fixed and recorded. Self-consistency
  variants record every sample.

### Directory layout (outside Git)

```text
~/.local/state/cloudops-copilot/engine-loop/
  worktree/            # branch for engine changes
  datasets/<snapshot>/ # immutable bundles + labels + manifest
  cache/               # model responses by request digest
  runs/<run-id>/       # config, per-case outputs, scorecard.json, triage.json
  ledger.jsonl         # one line per experiment
  lab/                 # private lab dir (credentials, receipts), mode 0700
```

Ledger line:

```json
{"id": "exp-0007", "date": "...", "stage": "C", "hypothesis": "...",
 "base_run": "...", "head_run": "...", "dataset": "...", "split": "dev",
 "delta": {"macro_f1": 0.0, "overflow": 0}, "decision": "keep|revert",
 "commit": "...", "model_calls": 0, "tokens": 0}
```

