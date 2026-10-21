# CloudOps Copilot

Kubernetes incident investigation and bounded recovery for a small operations team.

**Current state:** a runnable English React/Vite + shadcn/ui prototype using local fixtures. The backend, real model/cluster integrations and deployment runtime are not implemented.

## Project and roadmap

The [system tracker](https://github.com/pho-veteran/cloudops-copilot/issues/3) is the source of truth for product scope, architecture, UX direction, research, dependencies and release acceptance. Each feature EPIC owns its detailed research. Task issues are created only when that EPIC starts.

| EPIC | Feature |
|---|---|
| [E01 · #4](https://github.com/pho-veteran/cloudops-copilot/issues/4) | User access and application sessions |
| [E02 · #5](https://github.com/pho-veteran/cloudops-copilot/issues/5) | Scoped workloads, health and known-good baselines |
| [E03 · #6](https://github.com/pho-veteran/cloudops-copilot/issues/6) | Incident intake, work queue, lifecycle and handoff |
| [E04 · #7](https://github.com/pho-veteran/cloudops-copilot/issues/7) | Evidence collection, durable investigation runs and comparison |
| [E05 · #8](https://github.com/pho-veteran/cloudops-copilot/issues/8) | Versioned runbooks and knowledge retrieval |
| [E06 · #9](https://github.com/pho-veteran/cloudops-copilot/issues/9) | AI diagnosis through OpenAI-compatible endpoints |
| [E07 · #10](https://github.com/pho-veteran/cloudops-copilot/issues/10) | Approved recovery, verification, reconciliation and rollback |
| [E08 · #11](https://github.com/pho-veteran/cloudops-copilot/issues/11) | Resource recommendations, coverage and export |
| [E09 · #12](https://github.com/pho-veteran/cloudops-copilot/issues/12) | Connections, freshness and operating visibility |
| [E10 · #13](https://github.com/pho-veteran/cloudops-copilot/issues/13) | Reproducible lab, evaluation and release delivery |

## Run the prototype

```sh
cd apps/web
npm ci
npm run dev
```

Open the URL printed by Vite. Select **Choose a story** and start with **OOM recovery**. Inspect evidence, review and approve the plan, use **Advance simulation**, then resolve and inspect Handoff. **Change story** and **Restart story** reset fixtures. All 24 scenarios are described in the system tracker.

The demo stores state in localStorage. It does not call Kubernetes, a model provider or a backend. Simulation controls are not production controls.

## Checks

From `apps/web`, run `npm test` and `npm run build`. These verify the prototype, not the future runtime or release gates.

See [CONTRIBUTING.md](CONTRIBUTING.md) for language and planning conventions. Historical research/proposal/review documents were removed from the current tree after migration to Issues; the system tracker retains immutable Git references for provenance.
