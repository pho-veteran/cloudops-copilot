"""Exercise real owned-Pod restart/replacement and natural five-minute candidate expiry."""

from pathlib import Path
import time

from e02_probe_support import (
    arguments,
    client,
    delete_owned_pod,
    lab,
    login,
    metadata,
    owned_pod,
    password,
    save,
)


def main():
    args = arguments(__doc__, "races.json")
    secret = password(args)
    report = metadata(Path(__file__))
    report["checks"] = {}
    with client(args) as connection:
        headers = login(connection, args, secret)
        workloads = {r["name"]: r for r in connection.get("/api/workloads").json()["items"]}

        def path(name):
            return "/api/workloads/" + workloads[name]["id"]

        def preview(name):
            for _ in range(8):
                response = connection.get(path(name) + "/baseline-candidate")
                if response.status_code == 200:
                    value = response.json()
                    return {"candidate_id": value["id"], "digest": value["digest"]}
                lab.error_response(response, 409, "observation_busy")
                time.sleep(0.5)
            raise RuntimeError("Fresh reader remained busy.")

        def confirm(name, body):
            return connection.post(path(name) + "/baseline", json=body, headers=headers)

        body = preview("e02-ready")
        pod = owned_pod("e02-ready")
        lab.require(pod is not None, "Expected exactly one owned healthy Pod.")
        uid = pod["metadata"]["uid"]
        count = pod["status"]["containerStatuses"][0]["restartCount"]
        # No Deployment spec changes: restart only the process inside this owned disposable Pod.
        lab.command(
            [
                "docker",
                "exec",
                "cloudops-e01-https-k3s-1",
                "/bin/kubectl",
                "exec",
                pod["metadata"]["name"],
                "-n",
                "demo",
                "--",
                "python",
                "-c",
                "import os,signal;os.kill(1,signal.SIGINT)",
            ],
            check=False,
        )
        deadline = time.monotonic() + 100
        current = None
        while time.monotonic() < deadline:
            current = owned_pod("e02-ready")
            if (
                current
                and current["metadata"]["uid"] == uid
                and current.get("status", {})
                .get("containerStatuses", [{}])[0]
                .get("restartCount", 0)
                > count
                and current["status"]["containerStatuses"][0].get("ready")
            ):
                break
            time.sleep(1)
        assert (
            current
            and current["metadata"]["uid"] == uid
            and current["status"]["containerStatuses"][0]["restartCount"] > count
        )
        time.sleep(5.3)
        lab.error_response(confirm("e02-ready", body), 409, "candidate_changed")
        report["checks"]["same_owned_pod_restart_counter_increase_rejects"] = True
        body = preview("e02-ready")
        delete_owned_pod("e02-ready", current)
        lab.kube("rollout", "status", "deployment/e02-ready", "-n", "demo", "--timeout=100s")
        time.sleep(5.3)
        lab.error_response(confirm("e02-ready", body), 409, "candidate_changed")
        report["checks"]["replacement_owned_pod_same_deployment_rejects"] = True
        # The final error must be expiry, not a busy reader or a changed identity.
        body = preview("e02-config")
        before = lab.target_snapshot("e02-config")
        started = time.monotonic()
        print(
            "Owned restart/replacement checks passed; waiting for natural five-minute expiry.",
            flush=True,
        )
        time.sleep(301)
        after = lab.target_snapshot("e02-config")
        assert {k: before[k] for k in ("uid", "generation", "spec_sha256")} == {
            k: after[k] for k in ("uid", "generation", "spec_sha256")
        }
        response = confirm("e02-config", body)
        lab.error_response(response, 409, "candidate_changed")
        assert "five minutes" in response.json()["error"]["message"]
        report["checks"]["naturally_expired_candidate_rejected"] = True
        report["expiry_elapsed_seconds"] = time.monotonic() - started
    save(report, args.output)


if __name__ == "__main__":
    main()
