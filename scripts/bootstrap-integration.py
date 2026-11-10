"""Prepare isolated K3s identities and scoped credentials; never alters host kubectl context."""

import hashlib
import json
import secrets
import subprocess
import time
from local_lab import COMPOSE, ROOT


def kubectl(*args, body=None):
    result = subprocess.run(
        [*COMPOSE, "exec", "-T", "k3s", "/bin/kubectl", *args],
        input=body,
        text=True,
        capture_output=True,
        check=True,
        cwd=ROOT,
    )
    return result.stdout.strip()


def dispatch_permit():
    """Issue the fresh-install DispatchPermit/v1 once; an existing authority is never replaced."""
    admin = [*COMPOSE, "run", "--rm", "-T", "dispatch-admin"]
    status = json.loads(
        subprocess.run(
            [*admin, "status"], capture_output=True, text=True, check=True, cwd=ROOT
        ).stdout
    )
    if status["permit"] is not None:
        print(f"Job dispatch permit present (denial: {status['denial'] or 'none'}); unchanged.")
        return
    if status["audit"]:
        raise SystemExit(
            "A dispatch authority audit exists without a usable permit; use dispatch-admin "
            "recovery operations instead of a fresh-install bootstrap."
        )
    subprocess.run(
        [
            *admin,
            "bootstrap",
            "--fresh-install",
            "--reason",
            "Disposable local lab fresh install",
            "--evidence",
            "scripts/bootstrap-integration.py",
        ],
        check=True,
        cwd=ROOT,
        capture_output=True,
    )
    print("Fresh-install job dispatch permit issued for every job class.")


def main():
    folder = ROOT / "tmp/e01-integration"
    for kind in (
        "reader",
        "writer",
        "dispatcher",
        "krr",
        "intake-api",
        "intake-sender",
        "prometheus",
    ):
        (folder / kind).mkdir(parents=True, exist_ok=True)
    if not (folder / "krr/key").exists():
        (folder / "krr/key").write_text(secrets.token_urlsafe(32), encoding="utf-8")
    sender = folder / "intake-sender/token"
    if not sender.exists():
        sender.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    # The API receives only the digest; the Alertmanager overlay mounts the sender token.
    (folder / "intake-api/config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_id": "lab-alertmanager",
                "cluster": "local-k3s",
                "token_sha256": hashlib.sha256(sender.read_text().strip().encode()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [*COMPOSE, "up", "-d", "--wait", "--wait-timeout", "240", "k3s"], check=True, cwd=ROOT
    )
    kubectl(
        "apply", "-f", "-", body=(ROOT / "config/lab/workload.yaml").read_text(encoding="utf-8")
    )
    kubectl(
        "apply",
        "-f",
        "-",
        body=(ROOT / "config/lab/kubernetes-incident-families.yaml").read_text(encoding="utf-8"),
    )
    kubectl("rollout", "status", "deployment/checkout", "-n", "demo", "--timeout=180s")
    names = (
        "checkout",
        "checkout-crashloop",
        "checkout-oom",
        "checkout-pending",
        "checkout-imagepull",
    )
    for name in names:
        deadline = time.monotonic() + 60
        while True:
            pods = json.loads(
                kubectl("get", "pods", "-n", "demo", "-l", f"app={name}", "-o", "json")
            )
            if pods.get("items"):
                break
            if time.monotonic() >= deadline:
                raise SystemExit(f"Fixture Pod for {name} did not appear.")
            time.sleep(1)
    deployments = {
        name: json.loads(kubectl("get", "deployment", name, "-n", "demo", "-o", "json"))
        for name in names
    }
    prometheus_workloads = [
        {"namespace": "demo", "name": name, "uid": deployments[name]["metadata"]["uid"]}
        for name in names
    ]
    (folder / "prometheus/workloads.json").write_text(
        json.dumps(prometheus_workloads, indent=2) + "\n", encoding="utf-8"
    )
    checkout = deployments["checkout"]
    (folder / "prometheus/workload.json").write_text(
        json.dumps(
            {
                "namespace": "demo",
                "name": "checkout",
                "uid": checkout["metadata"]["uid"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    kubeconfig = json.loads(kubectl("config", "view", "--raw", "-o", "json"))
    ca = kubeconfig["clusters"][0]["cluster"]["certificate-authority-data"]
    workloads = [
        {
            "cluster": "local-k3s",
            "namespace": "demo",
            "name": name,
            "uid": deployments[name]["metadata"]["uid"],
            "source": "live_kubernetes",
        }
        for name in names
    ]
    for kind in ("reader", "writer"):
        token = kubectl("create", "token", f"cloudops-{kind}", "-n", "demo", "--duration=8h")
        client = {"server": "https://k3s:6443", "ca_data": ca, "token": token}
        (folder / kind / "client.json").write_text(json.dumps(client), encoding="utf-8")
        selected = workloads if kind == "reader" else workloads[:1]
        (folder / kind / "workloads.json").write_text(
            json.dumps(selected, indent=2) + "\n", encoding="utf-8"
        )
        config = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "local-k3s",
                    "cluster": {"server": client["server"], "certificate-authority-data": ca},
                }
            ],
            "users": [{"name": kind, "user": {"token": token}}],
            "contexts": [
                {
                    "name": "local-k3s",
                    "context": {"cluster": "local-k3s", "user": kind, "namespace": "demo"},
                }
            ],
            "current-context": "local-k3s",
        }
        (folder / kind / "kubeconfig.json").write_text(json.dumps(config), encoding="utf-8")
    dispatch_permit()
    print(
        "Local K3s Deployment is ready. Separate reader/writer credentials written under ignored tmp/e01-integration; tokens expire after eight hours. No credential values were printed."
    )


if __name__ == "__main__":
    main()
