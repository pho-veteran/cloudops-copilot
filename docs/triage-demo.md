# Local triage demo

One retained K3s node supplies six repeatable incident scenarios. They generate real
Kubernetes events, logs and measured metrics for an agent to inspect. The application
collects evidence; AI diagnosis still requires its owning agent/runtime integration.

Complete the [one-time setup and account creation](e01-local-lab.md#retained-local-testing-lab)
first. For an already provisioned checkout, define this shell function:

```sh
lab() {
  services/backend/.venv/bin/python scripts/manage-local-lab.py \
    --project cloudops-local-testing-lab "$@"
}
lab resume
```

`resume` starts the existing application, monitoring and alert delivery with cached
images. It preserves accounts and data, checks the recorded identities, and keeps
execution quarantined. It does not build images, run a suite, start KRR or launch
background traffic. If an eight-hour credential has expired, renew it explicitly
with `lab renew reader`, `lab renew monitoring` and `lab renew writer`, then resume.

Open **https://localhost:8443** and sign in with the local Operator account. Existing
account credentials remain unchanged. CLI interactions validate the lab CA; browser
trust is a separate user decision. For direct cluster interaction:

```sh
LAB_K3S_ID=$(docker --host unix:///var/run/docker.sock ps -q \
  --filter label=com.docker.compose.project=cloudops-local-testing-lab \
  --filter label=com.docker.compose.service=k3s)
docker --host unix:///var/run/docker.sock exec -it "$LAB_K3S_ID" /bin/kubectl get pods -n demo
```

## Choose a scenario

Each scenario affects only the recorded `demo/checkout` Deployment. `history` stays
available as a comparison workload. Run one scenario at a time, inspect its evidence
in the application or through Kubernetes, then reset it before choosing another.

| Scenario | Command | Signals to inspect |
| --- | --- | --- |
| Startup crash | `lab fault crashloop` | Failed restarts, BackOff events and startup logs. |
| Memory exhaustion | `lab fault oom` | Actual OOMKilled termination and a low memory limit. |
| Image retrieval failure | `lab fault imagepull` | ErrImagePull/ImagePullBackOff and pull events. |
| Unschedulable workload | `lab fault pending` | Pending Pod, FailedScheduling and an excessive CPU request. |
| Slow/erroring requests | `lab fault traffic` | Measured latency, HTTP 503 ratio and a real Alertmanager signal. |
| Retrying initialization | `lab fault init-retry` | Failing init container, restart count and init logs. |

```sh
lab fault pending
lab observe
# Inspect the workload and create an investigation in the application.
lab reset
```

For the traffic scenario, generate a short, bounded stream explicitly:

```sh
lab fault traffic
lab load --target checkout --seconds 45 --rate 2
lab observe
# Inspect the alert and its investigation evidence.
lab reset
```

Previous logs depend on the terminated attempt still being retained by the runtime;
inspect them promptly. Missing logs remain visible as gaps. Reset restores only the
captured fault fields and refuses conflicting changes or unresolved execution state.
An alert's resolved signal does not automatically resolve its incident or prove remediation.

Finish with `lab stop`. It closes dispatch, quarantines execution and stops the owned
stack while retaining data. No benchmark or long-running load is needed for the demo.
