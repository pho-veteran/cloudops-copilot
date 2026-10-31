"""Expire a real HTTPS baseline session, require fresh explicit review, and restore eight-hour sessions."""

from pathlib import Path
import os
import subprocess
import time

from e02_probe_support import arguments, client, lab, login, metadata, password, save, wait_ready


def configure(seconds):
    subprocess.run(
        [*lab.compose, "up", "-d", "--no-deps", "--force-recreate", "api"],
        env={**os.environ, "SESSION_SECONDS": str(seconds)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        cwd=lab.root,
        timeout=90,
    )


def main():
    args = arguments(__doc__, "session-expiry.json")
    secret = password(args)
    report = metadata(Path(__file__))
    try:
        configure(12)
        with client(args) as connection:
            wait_ready(connection)
            headers = login(connection, args, secret)
            workload = next(
                w
                for w in connection.get("/api/workloads").json()["items"]
                if w["name"] == "e02-ready"
            )
            path = "/api/workloads/" + workload["id"]
            response = connection.get(path + "/baseline-candidate")
            assert response.status_code == 200, response.text
            first = response.json()
            body = {"candidate_id": first["id"], "digest": first["digest"]}
            cookie = connection.cookies["cloudops_session"]
            before = [
                lab.stable(row) for row in connection.get(path + "/baselines").json()["items"]
            ]
            time.sleep(13)
            expired = connection.post(
                path + "/baseline",
                json=body,
                headers={**headers, "Cookie": "cloudops_session=" + cookie},
            )
            lab.error_response(expired, 401, "session_expired")
            headers = login(connection, args, secret)
            assert [
                lab.stable(row) for row in connection.get(path + "/baselines").json()["items"]
            ] == before
            lab.error_response(
                connection.post(path + "/baseline", json=body, headers=headers),
                409,
                "candidate_changed",
            )
            assert [
                lab.stable(row) for row in connection.get(path + "/baselines").json()["items"]
            ] == before
            response = connection.get(path + "/baseline-candidate")
            assert response.status_code == 200, response.text
            fresh = response.json()
            assert (
                fresh["snapshot"]["provenance"]["first"]["identity"]
                == first["snapshot"]["provenance"]["first"]["identity"]
            )
            assert (
                fresh["snapshot"]["provenance"]["first"]["deployment"]["generation"]
                == first["snapshot"]["provenance"]["first"]["deployment"]["generation"]
            )
            time.sleep(5.3)
            confirmed = connection.post(
                path + "/baseline",
                json={"candidate_id": fresh["id"], "digest": fresh["digest"]},
                headers=headers,
            )
            assert confirmed.status_code == 201, confirmed.text
            report.update(
                environment="Actual verified HTTPS API/PostgreSQL/K3s; real temporary 12-second sessions, not browser proof.",
                expiry_seconds=12,
                expired_cookie_replay_status=401,
                old_candidate_after_new_login_status=409,
                fresh_explicit_review_confirm_status=201,
                same_uid_generation_epoch=True,
                denied_history_unchanged=True,
                baseline_id=confirmed.json()["id"],
            )
    finally:
        configure(28800)
        with client(args) as connection:
            wait_ready(connection)
        restored = int(
            lab.app(
                "from cloudops.shared.config import settings; print(settings().session_seconds)"
            )
        )
        lab.require(restored == 28800, "Normal session configuration was not restored.")
        report["normal_session_seconds_restored"] = restored
    save(report, args.output)


if __name__ == "__main__":
    main()
