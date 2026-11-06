"""Read-only local HTTP acceptance probe over explicitly selected stored Runs.

Provision/seed the owned lab separately. This probe never creates fixture
evidence, collects from a provider, changes a lifecycle or grants a pin.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--incident-id", required=True, type=UUID)
    parser.add_argument("--left-run-id", required=True, type=UUID)
    parser.add_argument("--right-run-id", required=True, type=UUID)
    parser.add_argument("--resolution-id", type=UUID)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--compare-with", type=Path)
    args = parser.parse_args()
    for value in (args.base_url, args.origin):
        parsed = urlsplit(value)
        if (
            parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            parser.error("The probe only supports explicit loopback lab origins.")
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        parser.error("Supply the existing lab account password on stdin.")
    with httpx.Client(
        base_url=args.base_url, timeout=10, follow_redirects=False, trust_env=False
    ) as client:
        login = client.post(
            "/api/login",
            headers={"Origin": args.origin},
            json={"username": args.username, "password": password},
        )
        login.raise_for_status()
        role = login.json()["principal"]["role"]
        base = f"/api/incidents/{args.incident_id}/runs/{args.left_run_id}"
        pair = {"right_incident_id": str(args.incident_id), "right_run_id": str(args.right_run_id)}
        result = client.get(base + "/compare", params=pair)
        result.raise_for_status()
        summary = result.json()
        hashes, checks, maximum_bytes = {}, [], len(result.content)
        for source in summary["sources"]:
            for side in ("left", "right"):
                evidence = source[side]
                if not evidence or evidence["content_status"] != "available":
                    continue
                endpoint = f"/api/incidents/{args.incident_id}/runs/{evidence['run_id']}/evidence/{evidence['id']}"
                content = client.get(endpoint + "/content")
                content.raise_for_status()
                actual = sha(canonical(content.json()["payload"]))
                assert actual == evidence["sha256"]
                hashes[evidence["id"]] = actual
                targets = client.get(endpoint + "/citation-targets", params={"limit": 100})
                if targets.status_code == 409:
                    continue  # Explicit unsupported historical semantic schema.
                targets.raise_for_status()
                maximum_bytes = max(maximum_bytes, len(targets.content))
                target = next(
                    (item for item in targets.json()["items"] if not item["ambiguous"]), None
                )
                if target:
                    resolved = client.get(
                        endpoint + "/citation",
                        params={
                            "locator_kind": target["locator"]["kind"],
                            "locator_key": target["locator"]["key"],
                        },
                    )
                    resolved.raise_for_status()
                    assert resolved.json()["citation"]["evidence_id"] == evidence["id"]
                    assert resolved.json()["target"]["locator"] == target["locator"]
            detail = client.get(
                base + "/compare",
                params={
                    **pair,
                    "source_key": source["source_key"],
                    "catalog_version": source["catalog_version"],
                    "limit": 100,
                },
            )
            detail.raise_for_status()
            maximum_bytes = max(maximum_bytes, len(detail.content))
            checks.append(
                {
                    "source_key": source["source_key"],
                    "status": source["status"],
                    "page_rows": len(detail.json()["items"]),
                    "has_more": detail.json()["next_cursor"] is not None,
                }
            )
        wrong = client.get(base + "/compare", params={**pair, "right_incident_id": str(uuid4())})
        assert wrong.status_code == 404
        saved_hash = None
        if args.resolution_id:
            saved = client.get(
                f"/api/incidents/{args.incident_id}/handoff",
                params={"resolution_id": str(args.resolution_id)},
            )
            saved.raise_for_status()
            saved_hash = sha(saved.content)
        assert maximum_bytes <= 256 * 1024
        receipt = {
            "proof_class": "Actual local HTTP reads of pre-existing stored records; producer provenance remains that of the lab fixtures.",
            "role": role,
            "incident_id": str(args.incident_id),
            "left_run": str(args.left_run_id),
            "right_run": str(args.right_run_id),
            "source_payload_hashes": hashes,
            "saved_report_sha256": saved_hash,
            "wrong_scope_status": wrong.status_code,
            "maximum_comparison_response_bytes": maximum_bytes,
            "source_checks": checks,
        }
        if args.compare_with:
            before = json.loads(args.compare_with.read_text(encoding="utf-8"))
            for key in (
                "incident_id",
                "left_run",
                "right_run",
                "source_payload_hashes",
                "saved_report_sha256",
            ):
                assert receipt[key] == before[key], f"Stored history changed: {key}"
            receipt["restart_history_matches"] = True
        args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(
            json.dumps(
                {
                    "sources_checked": len(checks),
                    "payload_hashes_verified": len(hashes),
                    "role": role,
                    "restart_history_matches": receipt.get("restart_history_matches"),
                    "output": str(args.output),
                }
            )
        )


if __name__ == "__main__":
    main()
