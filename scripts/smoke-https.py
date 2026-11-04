"""Exercise local HTTPS sessions against the running stack; creates one labeled incident."""

import argparse
import json
import ssl
import sys
from getpass import getpass
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ca", required=True, type=Path, help="Exported local Caddy root certificate"
    )
    parser.add_argument("--operator", default="operator")
    parser.add_argument("--viewer", default="viewer")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read Operator then Viewer passwords from stdin",
    )
    parser.add_argument("--output", type=Path, default=Path("tmp/https-smoke.json"))
    args = parser.parse_args()
    passwords = (
        [sys.stdin.readline().rstrip("\r\n") for _ in range(2)]
        if args.password_stdin
        else [getpass("Operator password: "), getpass("Viewer password: ")]
    )
    origin = "https://localhost:8443"
    tls = ssl.create_default_context(cafile=str(args.ca))
    with (
        httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=15) as operator,
        httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=15) as viewer,
    ):
        page = operator.get("/")
        assert page.status_code == 200 and "/assets/" in page.text
        assert operator.get("/api/workloads").status_code == 401
        assert (
            operator.post(
                "/api/login",
                json={"username": args.operator, "password": passwords[0]},
                headers={"Origin": "https://foreign.invalid"},
            ).status_code
            == 403
        )
        signed_in = operator.post(
            "/api/login",
            json={"username": args.operator, "password": passwords[0]},
            headers={"Origin": origin},
        )
        assert signed_in.status_code == 200, "Operator sign-in failed"
        cookie = signed_in.headers["set-cookie"].lower()
        assert all(flag in cookie for flag in ("secure", "httponly", "samesite=lax", "path=/"))
        assert "domain=" not in cookie
        assert signed_in.headers["cache-control"] == "no-store"
        csrf = signed_in.json()["csrf_token"]
        headers = {"Origin": origin, "X-CSRF-Token": csrf}
        workload = operator.get("/api/workloads").json()["items"][0]
        body = {
            "workload_id": workload["id"],
            "title": "Local HTTPS session smoke",
            "description": "Persisted local verification; external evidence remains synthetic.",
        }
        before = operator.get("/api/incidents").json()
        assert (
            operator.post(
                "/api/incidents",
                json=body,
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": "invalid",
                    "Idempotency-Key": str(uuid4()),
                },
            ).status_code
            == 403
        )
        after = operator.get("/api/incidents").json()
        assert after["items"] == before["items"]
        assert after["next_offset"] == before["next_offset"]
        created = operator.post(
            "/api/incidents", json=body, headers={**headers, "Idempotency-Key": str(uuid4())}
        )
        assert created.status_code == 201, "Operator admission failed"
        ids = created.json()
        signed_viewer = viewer.post(
            "/api/login",
            json={"username": args.viewer, "password": passwords[1]},
            headers={"Origin": origin},
        )
        assert signed_viewer.status_code == 200, "Viewer sign-in failed"
        viewer_headers = {
            "Origin": origin,
            "X-CSRF-Token": signed_viewer.json()["csrf_token"],
            "Idempotency-Key": str(uuid4()),
        }
        assert viewer.get(f"/api/incidents/{ids['incident_id']}").status_code == 200
        assert viewer.post("/api/incidents", json=body, headers=viewer_headers).status_code == 403
        assert (
            viewer.post(
                f"/api/incidents/{ids['incident_id']}/runs", headers=viewer_headers
            ).status_code
            == 403
        )
        old_cookie = operator.cookies.get("cloudops_session")
        assert old_cookie
        signed_out = operator.post("/api/logout", headers=headers)
        assert signed_out.status_code == 204
        deleted = signed_out.headers["set-cookie"].lower()
        assert "max-age=0" in deleted and "secure" in deleted and "path=/" in deleted
        assert operator.get("/api/session").status_code == 401
        assert (
            operator.get(
                "/api/session", headers={"Cookie": f"cloudops_session={old_cookie}"}
            ).status_code
            == 401
        )
        assert viewer.post("/api/logout", headers=viewer_headers).status_code == 204
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(ids, indent=2) + "\n", encoding="utf-8")
    print(
        "HTTPS certificate/hostname validation, Secure cookie, Origin/CSRF, persisted admission, Viewer denials, logout and token replay: passed."
    )
    print(
        f"Saved incident/run identities: {args.output}. No credentials or session material were exported."
    )


if __name__ == "__main__":
    main()
