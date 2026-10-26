"""Real Linux peer-credential and unavailable-proof denials in the named disposable lab."""

import json
import os
import subprocess
from pathlib import Path

from local_lab import COMPOSE


def call(*args, env=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)


def main():
    prefix = [
        "docker",
        "exec",
        "cloudops-e01-https-executor-1",
        "python",
        "-m",
        "cloudops.features.recovery.maintenance",
    ]
    status = json.loads(call(*prefix, "status").stdout)
    uid = status["targets"][0]["uid"]
    peer = call(
        "docker",
        "exec",
        "--user",
        "10001",
        "cloudops-e01-https-executor-1",
        "python",
        "-m",
        "cloudops.features.recovery.maintenance",
        "status",
    )
    assert peer.returncode == 1 and json.loads(peer.stdout)["code"] == "maintenance_actor_denied"
    denied = []
    for args in (
        ["enable_target"],
        ["enable_target", "--target", uid, "--reason", "Missing evidence"],
        [
            "enable_target",
            "--target",
            "not-configured",
            "--reason",
            "Scope denial",
            "--evidence",
            "local://e01/denial",
        ],
    ):
        response = call(*prefix, *args)
        value = json.loads(response.stdout)
        assert response.returncode == 1 and value["code"] == "maintenance_request_invalid"
        denied.append(value)
    before = json.loads(call(*prefix, "status").stdout)["targets"]
    assert before == status["targets"], "Invalid fields changed target authority"
    unavailable = {**os.environ, "EXECUTOR_PROOF_PROVIDER": "unavailable"}
    assert (
        call(
            *COMPOSE,
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "30",
            "--no-deps",
            "--force-recreate",
            "executor",
            env=unavailable,
        ).returncode
        == 0
    )
    proofs = []
    try:
        for operation in ("enable_target", "release_uncertain_target", "exit_recovery_mode"):
            response = call(
                *prefix,
                operation,
                "--target",
                uid,
                "--reason",
                "Unavailable proof provider acceptance",
                "--evidence",
                "local://e01/no-proof-provider",
            )
            value = json.loads(response.stdout)
            assert response.returncode == 1 and value["code"] == "maintenance_proof_required"
            proofs.append(value)
        status = json.loads(call(*prefix, "status").stdout)
        assert all(not t["enabled"] and t["recovery_mode"] for t in status["targets"])
    finally:
        supported = {**os.environ, "EXECUTOR_PROOF_PROVIDER": "resource_version_fence_v1"}
        assert (
            call(
                *COMPOSE,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "30",
                "--no-deps",
                "--force-recreate",
                "executor",
                env=supported,
            ).returncode
            == 0
        )
    applied = []
    for operation in ("enable_target", "exit_recovery_mode"):
        response = call(
            *prefix,
            operation,
            "--target",
            uid,
            "--reason",
            "Restore supported proof configuration after negative tests",
            "--evidence",
            "local://e01/maintenance-denials",
        )
        value = json.loads(response.stdout)
        assert response.returncode == 0 and value["status"] == "applied"
        applied.append(value)
    Path("tmp/e01-maintenance-denials.json").write_text(
        json.dumps(
            {
                "untrusted_peer": json.loads(peer.stdout),
                "invalid_fields": denied,
                "unavailable_proof": proofs,
                "fresh_enable_exit": applied,
            },
            indent=2,
        )
    )
    print(
        "PASS: untrusted peer and missing/invalid fields denied without changing authority; unavailable proof denied all enable/release/exit operations; supported proof and fresh explicit enable/exit restored readiness."
    )


if __name__ == "__main__":
    main()
