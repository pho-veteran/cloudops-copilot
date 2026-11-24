# E10 S2 single-host EC2 lab acceptance (#121)

This ledger records the S2 acceptance of the budgeted single-host EC2 lab for [#121](https://github.com/pho-veteran/cloudops-copilot/issues/121) under [E10 #13](https://github.com/pho-veteran/cloudops-copilot/issues/13). It uses the S1 package from [#120](https://github.com/pho-veteran/cloudops-copilot/issues/120) and the [OpenTofu root](../../../deploy/ec2/README.md) at `6ac5bf92d44559066a677bdc3efe1f01656cc473`.

The campaign ran on 2026-11-24 in one session of 3 h 10 min wall time, with 2.66 instance running hours. The lab was destroyed the same day. This is a demo/course deployment, not a retained customer environment: the owner approved destroying it after evidence and redeploying when needed. AWS IDs are shortened, and no IP address, key, password, token or state file is published.

## Identity

| Item | Value |
| --- | --- |
| Account / region / AZ | `0581.7594` / `us-east-1` / `us-east-1a` |
| AMI | `ami-01757311770dc7690`, `debian-13-amd64-20261123-2671`, publisher `136693071363` (Debian), one EBS root mapping |
| Instance | `c6a.2xlarge` at apply, `c5a.2xlarge` after a capacity event; both 8 vCPU, 16 GiB, x86-64, On-Demand. Apply, host preparation, the core install, the tunnel smoke and release-lab separation ran on `c6a`. The owned lab, all measurements and the recorded restarts ran on `c5a`. |
| Root volume | gp3 50 GiB, encrypted, `delete_on_termination = false` |
| Network | Dedicated VPC `10.42.0.0/24`. Ingress TCP 22 from one operator `/32`. Egress TCP 443 only. Public IPv4 on the instance, no Elastic IP. |
| Release bundle | `cloudops-6ac5bf9`. Image IDs: backend `sha256:f2a2dc36.62afe86eb`, https `sha256:6219d2ac.c818808633e`, db `sha256:1eff563f.dcfbc50d7` |
| OpenTofu | 1.13.9 with the committed `hashicorp/aws` 6.73.0 lock. Private local state outside the checkout. |

## Cost and approval

Details are in `cost.json`.

**Approval.** On 2026-11-24 the account owner approved these limits before any paid apply:

- On-Demand pricing.
- A USD 10 ceiling.
- At most two sessions of at most 5 h each, with an on-host shutdown timer.
- Destroy no later than 2026-11-27.
- Full cleanup of every `Project=cloudops-e10-s2` resource, including the retained root volume, with no snapshots.
- The unrelated snapshot stays untouched.

The itemized estimate before apply was USD 3.50 expected and USD 5.90 upper bound, for 10 to 16 running hours. The cleanup owner is the account owner (GitHub `MinhKhoa2209`).

**Actual usage at list price:**

| Item | Usage | USD |
| --- | --- | --- |
| Compute | 0.50 h `c6a.2xlarge` at 0.306, 2.16 h `c5a.2xlarge` at 0.308 | 0.82 |
| Public IPv4 | 2.66 h at 0.005 | 0.01 |
| gp3 50 GiB | 3.17 h at 0.08/GB-month | 0.02 |
| Snapshots, Elastic IP | none | 0 |
| Data transfer out | well under 1 GB, inside the 100 GB monthly allowance | 0 |
| **Total** | | **≈ 0.85** |

The residual cost after destroy is USD 0 per month. Charges are expected to draw down the account's Free Tier credits; the Bills page refreshes daily.

**Alerts.** A monthly cost budget `cloudops-e10-s2` excludes credits, so alerts track real spend. The account already had USD 5.54 of pre-credit spend this month. Alerts therefore sit at that baseline plus the expected estimate (USD 9), the upper estimate (USD 11.5) and the ceiling (USD 15.5 actual and forecast). They go to the owner's email. Alerts can lag, so the shutdown timer and the manual inventory check are the primary controls.

**Over-budget rule.** An estimate above the ceiling, or missing approval, blocks apply. Any increase needs renewed approval.

**Forecast.** S2 finished on 2026-11-24 with 2.66 of the 10 expected running hours. Later use redeploys from the same variables, each with its own dated approval and destroy deadline. The [#126](https://github.com/pho-veteran/cloudops-copilot/issues/126) walkthrough is estimated at about 7.5 running hours (USD 2.60). A demo rehearsal or the demo is estimated at about 4.5 running hours (USD 1.53) each.

## Acceptance

| #121 requirement | Result | Receipt |
| --- | --- | --- |
| Dated regional line items, approval, stop deadline, cleanup owner, forecast | `cost.json` lists Price List API rates effective 2026-11-01 for compute, gp3, IPv4, snapshots, transfer and stopped retention. It also records the itemized estimate, actual usage, owner approval, deadline, cleanup owner and the revised forecast. | `cost.json` |
| Safe plan before spend | `fmt -check`, `init -lockfile=readonly` and `validate` passed before each saved plan. The saved plans were reviewed before applying them: 10 to add, then 10 to destroy. An automatic stop was armed after every start. | `infrastructure.json` |
| Install the S1 build on the authorized lab | The engine had no images before loading. Load took 6 s. `verify` and `verify --loaded` passed. The core started in 20 s at migration head `0020`, and every container ran its manifest image ID. The only application listener was `127.0.0.1:8443`. Dispatch started `permit_missing` until the trusted `bootstrap --fresh-install`. | `install.json` |
| Same-origin secure sessions and readiness | `scripts/smoke-https.py` passed on the host and from the operator workstation through `ssh -L 127.0.0.1:8443:127.0.0.1:8443`. Readiness returned 200, TLS verified against the project CA, the worker was fresh and dispatch was allowed. | `install.json` |
| Allowlisted resources; separated reader, monitoring and executor writer | The bundle-image release lab ran with no local build and no Docker socket mount. The API and worker have no writer credential or maintenance socket, and the API has no dispatch key. Real K3s returned 403 for reader Secret reads and patches, and for writer Secret reads and patches of another target. The executor started with its target disabled in recovery mode. A scoped live collection stored Kubernetes state, events and current logs. Prometheus sources were unavailable because the release lab omits Prometheus by design, so that Run ended `insufficient_evidence`. **Monitoring input:** the source-built owned lab passed `denials`, `containment` and `verify` with the kube-state-metrics, kubelet and sample scrape targets up. Its `collect` stored all six Prometheus sources and reported all five evidence packs available. | `separation.json`, `owned-lab.json` |
| Measure idle and bounded load, pressure and constraints | The table below covers 10 min idle and 5 min each at 5, 15 and 30 rps. Every load returned only HTTP 200 (1500, 4500 and 9000 responses). | `measurements.json` |
| Safe stop and restart | **Core:** with all containers stopped, the instance stopped with no public IPv4, no Elastic IP and the root volume attached. After start, the address changed and the host key was the same. Image IDs, migration head, CA, dispatch permit (normal, generation 1) and the saved incident were unchanged, and the tunnel smoke passed again. **Owned lab:** after another stop/start, the owner UUID, Deployment UIDs and all 26 Docker volumes were unchanged. `status`, `denials`, `containment` and `stored` (`evidence_unchanged=true`) passed. | `install.json`, `infrastructure.json`, `owned-lab.json` |
| Ownership-scoped cleanup, recording what is preserved | `cleanup --confirm-owner <owner UUID>` removed only the owned lab's 12 volumes, its containers and its network. It preserved the core and release-lab projects. `tofu destroy` then removed 10 resources. The retained root volume was deleted only after matching its ID, `Project` tag and name. The final inventory shows 0 project resources, volumes, instances, Elastic IPs, spot requests or state entries. The unrelated snapshot, key pair and VPC were untouched. | `owned-lab.json`, `infrastructure.json` |

## Measurements

The host was `c5a.2xlarge`: 8 vCPU, 15.4 GiB, no swap, gp3 50 GiB. Samples were taken every 5 s from host `/proc` files and from each Docker scope's cgroup files.

| Phase | Host CPU avg / max | Used memory max | Available min | CPU PSI some avg10 max | Memory / IO PSI |
| --- | --- | --- | --- | --- | --- |
| Idle, 10 min | 3.6% / 4.3% | 2.02 GiB | 13.4 GiB | 12.3% | 0 / 0.02 |
| 5 rps, 5 min | 4.1% / 17.8% | 2.13 GiB | 13.3 GiB | 11.5% | 0 / 0 |
| 15 rps, 5 min | 5.0% / 17.9% | 2.16 GiB | 13.3 GiB | 11.6% | 0 / 0 |
| 30 rps, 5 min | 5.9% / 18.6% | 2.13 GiB | 13.3 GiB | 11.2% | 0 / 0 |

- **Configured caps:** K3s is limited to 4 CPU and 4 GiB, and its node advertises 3 CPU and 3 GiB allocatable. Each sample pod is limited to 160 MiB.
- **Largest consumers:** at idle, K3s used about 1.9 GiB of memory, Prometheus 180 MiB and PostgreSQL 140 MiB.
- **After a 10 rps warm-up** (5 rps per Deployment): Prometheus reported p95 latency of 48 ms, an error ratio of 0 and 0 restarts.
- **Root disk:** free space stayed at 38.5 GiB or more.
- **CPU pressure:** CPU PSI stayed near 11–12% even at idle, while host CPU use stayed at 3–6%. This is likely short waits under the capped containers' CFS quotas rather than host saturation. Throttling counters were not sampled.
- **Host headroom:** memory and IO pressure were zero throughout. More than 13 GiB of memory and 6 vCPU remained free at 30 rps.
- **Workload surge headroom:** this figure is source-inspected, not exercised. Each sample Deployment requests 50m / 32Mi and is limited to 400m / 160Mi, and the default rolling update adds one surge Pod per Deployment. With surge, the four sample Pods use at most 1.6 CPU and 640 MiB of the node's 3 CPU / 3 GiB allocatable. System Pods are not counted. Each Pod's measured working set was 15.5 MiB.

## Findings

- **Capacity:** `c6a.2xlarge` returned `InsufficientInstanceCapacity` in `us-east-1a` for 28 minutes (17 start attempts). The stopped instance was changed to `c5a.2xlarge`, which started on the first attempt and costs USD 0.002/h more.
  - The OpenTofu plan shows `associate_public_ip_address` drift while the instance is stopped, and that drift forces replacement. Plan only while the instance runs, and review any replacement.
- **Swap limits reset by systemd:** a systemd `daemon-reload`, triggered by `apt-listchanges`, reset `memory.swap.max` of every Docker scope from `0` to `max` under the systemd cgroup driver. The host has no swap, but the lab's containment guard correctly refused load until each container's own declared limits were re-applied with `docker update`. The apt timers were then paused for the disposable session.
- **Docker Hub rate limit:** owned-lab registry verification reached the anonymous limit of 100 manifest requests per hour for the instance's public IPv4. The 11:52 stop/start was partly made to obtain a new address. That sidesteps a per-address provider limit, so the documented procedure is now to wait for the hourly window. The lab does not fall back to credentials or mirrors.
- **Checkout permissions:** the source checkout used by the owned lab must be readable by the sample container user. A clone made under `umask 077` produced unreadable `/app/sample_app.py`, so the source must be cloned under `umask 022`.
- **Node sizing:** the owned lab sizes kubelet reserves from host memory on the first start. After the instance type changed, the existing node advertised more than 3 GiB allocatable, so containment refused load. A fresh owned-lab start on the new host fixed this.
- **Host keys:** the Debian AMI prints its host keys in the full console output. `get-console-output --latest` omitted them.
- **Shutdown timer:** a scheduled shutdown does not survive a stop/start, so it was re-armed after each of the four starts.

## Limits

- The load generator runs on the same host. The measured rates are the lab's bounded 1–30 rps range, not a capacity limit.
- No rollout surge was exercised. Surge headroom is computed from manifests and measured limits.
- No target was enabled or written, and no recovery trial ran. Those belong to [#125](https://github.com/pho-veteran/cloudops-copilot/issues/125).
- No browser trust or browser exercise was performed. The second-operator walkthrough on a redeployed target belongs to [#126](https://github.com/pho-veteran/cloudops-copilot/issues/126).
- The actual billed amount appears on the Bills page after AWS's daily refresh. This ledger records list-price usage.
