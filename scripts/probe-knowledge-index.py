#!/usr/bin/env python3
"""Sanitized runtime probe for the Knowledge read API (#59)."""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlsplit

import httpx


def _origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise SystemExit("Use a loopback HTTP(S) base URL for this local probe.")
    return f"{parsed.scheme}://{parsed.netloc}"


def _request(client, method, path, *, headers, expected=200, **kwargs):
    response = client.request(method, path, headers=headers, **kwargs)
    if response.status_code != expected:
        raise SystemExit(f"{method} {path} returned {response.status_code}: {response.text[:300]}")
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="viewer")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--chunk-id")
    parser.add_argument("--document-version-id")
    args = parser.parse_args()
    if not args.password_stdin:
        parser.error("--password-stdin is required for non-interactive sanitized evidence.")
    password = sys.stdin.readline().rstrip("\r\n")
    origin = _origin(args.base_url)
    with httpx.Client(base_url=args.base_url, timeout=20) as client:
        login = _request(
            client,
            "POST",
            "/api/login",
            headers={"Origin": origin},
            json={"username": args.username, "password": password},
        )
        headers = {
            "Origin": origin,
            "X-CSRF-Token": login["csrf_token"],
        }
        status = _request(client, "GET", "/api/knowledge/index-status", headers=headers)
        page = _request(
            client, "GET", "/api/knowledge/documents", headers=headers, params={"limit": 25}
        )
        receipt = {
            "schema": "Knowledge59Probe/v1",
            "base_url": args.base_url,
            "status": {
                "state": status["state"],
                "has_active_generation": status["has_active_generation"],
                "read_available": status["read_available"],
                "catalog_search_available": status["catalog_search_available"],
                "active_generation_id": (
                    status["active_generation"]["id"] if status["active_generation"] else None
                ),
                "document_count": (
                    status["active_generation"]["document_count"]
                    if status["active_generation"]
                    else None
                ),
                "chunk_count": (
                    status["active_generation"]["chunk_count"]
                    if status["active_generation"]
                    else None
                ),
            },
            "catalog": {
                "generation_id": page["generation_id"],
                "source_digest": page["source_digest"],
                "count": len(page["items"]),
                "document_keys": [item["document_key"] for item in page["items"]],
            },
            "exact": [],
        }
        for item in page["items"][:3]:
            detail = _request(
                client,
                "GET",
                f"/api/knowledge/documents/{item['id']}",
                headers=headers,
            )
            receipt["exact"].append(
                {
                    "document_version_id": detail["id"],
                    "content_sha256": detail["content_sha256"],
                    "bytes": detail["bytes"],
                }
            )
        if args.chunk_id and args.document_version_id:
            chunk = _request(
                client,
                "GET",
                f"/api/knowledge/chunks/{args.chunk_id}",
                headers=headers,
                params={"document_version_id": args.document_version_id},
            )
            receipt["chunk"] = {
                "chunk_id": chunk["id"],
                "document_version_id": chunk["document_version_id"],
                "content_sha256": chunk["content_sha256"],
                "bytes": chunk["bytes"],
                "heading_path": chunk["heading_path"],
            }
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        from pathlib import Path

        Path(args.output).write_text(encoded, encoding="utf-8", newline="\n")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
