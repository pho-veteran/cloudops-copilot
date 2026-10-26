"""Prepare isolated K3s identities and scoped credentials; never alters host kubectl context."""

import json
import secrets
import subprocess
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


def main():
    folder = ROOT / "tmp/e01-integration"
    for kind in ("reader", "writer", "dispatcher", "krr"):
        (folder / kind).mkdir(parents=True, exist_ok=True)
    if not (folder / "krr/key").exists():
        (folder / "krr/key").write_text(secrets.token_urlsafe(32), encoding="utf-8")
    subprocess.run(
        [*COMPOSE, "up", "-d", "--wait", "--wait-timeout", "240", "k3s"], check=True, cwd=ROOT
    )
    kubectl(
        "apply", "-f", "-", body=(ROOT / "config/lab/workload.yaml").read_text(encoding="utf-8")
    )
    kubectl("rollout", "status", "deployment/checkout", "-n", "demo", "--timeout=180s")
    deployment = json.loads(kubectl("get", "deployment", "checkout", "-n", "demo", "-o", "json"))
    kubeconfig = json.loads(kubectl("config", "view", "--raw", "-o", "json"))
    ca = kubeconfig["clusters"][0]["cluster"]["certificate-authority-data"]
    workloads = [
        {
            "cluster": "local-k3s",
            "namespace": "demo",
            "name": "checkout",
            "uid": deployment["metadata"]["uid"],
            "source": "live_kubernetes",
        }
    ]
    for kind in ("reader", "writer"):
        token = kubectl("create", "token", f"cloudops-{kind}", "-n", "demo", "--duration=8h")
        client = {"server": "https://k3s:6443", "ca_data": ca, "token": token}
        (folder / kind / "client.json").write_text(json.dumps(client), encoding="utf-8")
        (folder / kind / "workloads.json").write_text(
            json.dumps(workloads, indent=2) + "\n", encoding="utf-8"
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
    print(
        "Local K3s Deployment is ready. Separate reader/writer credentials written under ignored tmp/e01-integration; tokens expire after eight hours. No credential values were printed."
    )


if __name__ == "__main__":
    main()
