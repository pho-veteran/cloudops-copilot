# Diagnosis engine: local environment and loop mechanics

Part of the [engine improvement loop](README.md).

## Environment

| Tier | Purpose | Setup |
| --- | --- | --- |
| T0 offline | Inner loop, unit tests, replay | Host Python 3.13 with `uv sync --project services/backend --frozen`. A PostgreSQL test container is needed only for the backend DB tests (`TEST_DATABASE_URL` must end in `_test`) |
| T1 lab | Case generation | `manage-local-lab.py --project cloudops-engine-lab --directory <lab>` with `preflight` then `start`; Operator account via the CLI. Ceiling 8.5 GiB RAM, 8 CPU, 20 GB disk. Diagnosis stays disabled in the lab |
| T2 model | Replay calls | OpenAI-compatible endpoint: `ENGINE_MODEL_BASE_URL`, `ENGINE_MODEL_NAME`, key in `ENGINE_MODEL_API_KEY` (never logged), daily caps `ENGINE_MAX_REQUESTS` and `ENGINE_MAX_TOKENS` |
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

## Loop mechanics

### Session start checklist

1. `git -C <worktree> fetch origin` and rebase the engine branch on `origin/main`.
2. `uv sync --project services/backend --frozen` in the worktree.
3. Confirm the model variables are set and read the remaining daily cap from the ledger.
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
replay   --dataset ID --split dev --config B4 [--variant k=v] [--concurrency 6]
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

