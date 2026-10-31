"""Verify actual unsupported-reference partial context, then restore the owned fixture."""

import json
from pathlib import Path

from e02_probe_support import arguments, client, lab, login, metadata, password, save


def main():
    args = arguments(__doc__, "partial-context.json")
    secret = password(args)
    report = metadata(Path(__file__))
    original = json.loads((lab.folder / "e02-config.json").read_text())
    changed = json.loads(json.dumps(original))
    changed["spec"]["template"]["spec"]["volumes"] = [
        {
            "name": "unsupported-projection",
            "projected": {
                "sources": [
                    {
                        "downwardAPI": {
                            "items": [{"path": "name", "fieldRef": {"fieldPath": "metadata.name"}}]
                        }
                    }
                ]
            },
        }
    ]
    with client(args) as connection:
        login(connection, args, secret)
        workload = next(
            w for w in connection.get("/api/workloads").json()["items"] if w["name"] == "e02-config"
        )
        path = "/api/workloads/" + workload["id"]
        history = connection.get(path + "/baselines").json()["items"]
        lab.require(
            bool(history), "Prepare a healthy confirmed baseline with the integration probe first."
        )
        baseline = history[0]
        assert baseline["profile"] == "deployment-images-resources/v1"
        try:
            lab.apply_fixture(changed)
            lab.kube("rollout", "status", "deployment/e02-config", "-n", "demo", "--timeout=90s")
            response = connection.get(
                path + "/recovery-context", params={"baseline_id": baseline["id"]}
            )
            assert response.status_code == 200, response.text
            context = response.json()
            assert context["reason"] == "unsupported" and not context["execution_eligible"]
            observed = connection.get(path + "/observation").json()
            assert (
                observed["summary"]["coverage"] == "partial"
                and observed["summary"]["effective_health"] == "unknown"
            )
            lab.error_response(
                connection.get(path + "/baseline-candidate"), 409, "baseline_unavailable"
            )
        finally:
            lab.apply_fixture(original)
            lab.kube("rollout", "status", "deployment/e02-config", "-n", "demo", "--timeout=90s")
        response = connection.get(path + "/baseline-candidate")
        assert response.status_code == 200, response.text
        report.update(
            source="Actual K3s declared downwardAPI projection outside baseline extraction; original fixture restored.",
            baseline_id=baseline["id"],
            reason=context["reason"],
            coverage=observed["summary"]["coverage"],
            effective_health=observed["summary"]["effective_health"],
            execution_eligible=context["execution_eligible"],
            partial_candidate_denied=True,
            recovered_healthy_candidate=True,
        )
    save(report, args.output)


if __name__ == "__main__":
    main()
