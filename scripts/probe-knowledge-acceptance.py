#!/usr/bin/env python3
"""E05 acceptance on a fresh, explicitly owned Compose lab; never application data.

Run prepare, mutate and finish in order, with browser checks between phases.
The corpus directory must be a disposable copy mounted by the migrate service.
Supply the disposable account password on stdin; receipts exclude credentials.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "scripts/fixtures/knowledge-relevance.json"
TRUSTED = """
import json, os, sys
from uuid import UUID
from cloudops.entrypoints import metadata
from cloudops.features.knowledge.guidance import retrieve, pin_guidance
from cloudops.shared.database import sessions
from cloudops.shared.errors import DomainError
from sqlalchemy.engine import make_url
assert make_url(os.environ['DATABASE_URL']).database.endswith('_test')
value=json.load(sys.stdin)
with sessions()() as db, db.begin():
    try:
        kw={k:value[k] for k in ('query','symptom_family','recovery_template') if value.get(k) is not None}
        args=(db, UUID(value['incident_id']), UUID(value['run_id']))
        if value.get('pin'):
            result=pin_guidance(*args, consumer='analysis', request_identity='e05-acceptance', **kw)
        else:
            result=retrieve(*args, limit=5, **kw)
        print(result.model_dump_json())
    except DomainError as exc:
        print(json.dumps({'error':exc.code,'http_status':exc.status}))
"""


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    return sha(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )


def checked(command, *, stdin=None):
    result = subprocess.run(
        command, cwd=ROOT, input=stdin, text=True, capture_output=True, timeout=90
    )
    if result.returncode:
        raise RuntimeError(
            f"Owned lab command failed (exit {result.returncode}); inspect local diagnostics."
        )
    return result.stdout.strip()


def origin(value):
    parsed = urlsplit(value)
    assert parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    assert not parsed.username and not parsed.password and parsed.path in {"", "/"}
    assert not parsed.query and not parsed.fragment
    return value.rstrip("/")


class Lab:
    def __init__(self, args):
        self.args = args
        self.compose = ["docker", "compose", "--env-file", args.env_file, "-p", args.project]
        for path in args.compose_file:
            self.compose += ["-f", path]
        self.corpus = Path(args.corpus).resolve()
        assert not self.corpus.is_relative_to(ROOT) and self.corpus.is_dir()
        self.images = {}
        for service in ("db", "api", "worker", "web"):
            container = checked(self.compose + ["ps", "-q", service])
            assert container
            info = json.loads(checked(["docker", "inspect", container]))[0]
            assert info["Config"]["Labels"].get("cloudops.task") == "knowledge62"
            assert info["Config"]["Labels"].get("com.docker.compose.project") == args.project
            self.images[service] = info["Image"]
        identity = self.python("""
import json, os, platform
from importlib.metadata import version
from sqlalchemy.engine import make_url
url=make_url(os.environ['DATABASE_URL'])
assert url.database.endswith('_test') and url.host=='db'
assert os.environ.get('COLLECTION_SOURCE_MODE')=='development_fixture'
assert not os.environ.get('KUBE_READ_CONFIG')
print(json.dumps({'database':url.database,'python':platform.python_version(),'source_mode':os.environ['COLLECTION_SOURCE_MODE'],'sqlalchemy':version('sqlalchemy'),'model_mode':'unconfigured'}))
""")
        self.identity = identity

    def python(self, code, value=None):
        return json.loads(
            checked(
                self.compose + ["exec", "-T", "api", "python", "-c", code], stdin=json.dumps(value)
            )
        )

    def cli(self, *args, stdin=None):
        return checked(
            self.compose + ["exec", "-T", "api", "python", "-m", "cloudops.entrypoints.cli", *args],
            stdin=stdin,
        )

    def index(self, expected):
        result = subprocess.run(
            self.compose
            + [
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "migrate",
                "python",
                "-m",
                "cloudops.entrypoints.cli",
                "knowledge-index",
                "--root",
                "/workspace",
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=90,
        )
        payload = json.loads(result.stdout)
        assert payload["outcome"] == expected, payload
        assert result.returncode == (1 if expected == "failed" else 0)
        return payload

    def trusted(self, ids, case, *, pin=False):
        return self.python(
            TRUSTED,
            {
                **ids,
                **{
                    k: case[k]
                    for k in ("query", "symptom_family", "recovery_template")
                    if k in case
                },
                "pin": pin,
            },
        )


def request(client, method, path, expected=200, **kwargs):
    response = client.request(method, path, **kwargs)
    assert response.status_code == expected, (
        method,
        path,
        response.status_code,
        response.text[:200],
    )
    return response


def login(stack, base, web_origin, role, password):
    client = stack.enter_context(
        httpx.Client(base_url=base, headers={"Origin": web_origin}, timeout=30)
    )
    result = request(
        client, "POST", "/api/login", json={"username": role, "password": password}
    ).json()
    assert result["principal"]["role"] == role
    client.headers["X-CSRF-Token"] = result["csrf_token"]
    return client


def create(client, question):
    workload = request(client, "GET", "/api/workloads").json()["items"][0]
    return request(
        client,
        "POST",
        "/api/incidents",
        expected=201,
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "workload_id": workload["id"],
            "title": "E05 installed acceptance",
            "description": "Controlled local acceptance; no cluster/model calls.",
            "question": question,
            "window": {"preset": 30},
        },
    ).json()


def guidance(ids):
    return f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}/knowledge"


def capture(client, receipt):
    selected = request(client, "GET", guidance(receipt["ids"])).json()
    ref = selected["selection"]["retrieval"]["chunks"][0]["reference"]
    target = request(
        client,
        "GET",
        guidance(receipt["ids"])
        + f"/selections/{selected['selection']['id']}/chunks/{ref['chunk_id']}",
        params={"document_version_id": ref["document_version_id"]},
    ).json()
    incident = f"/api/incidents/{receipt['ids']['incident_id']}"
    params = {"resolution_id": receipt["resolution_id"]}
    markdown = request(client, "GET", incident + "/handoff", params=params).content
    preview = request(client, "GET", incident + "/handoff/preview", params=params).json()
    assert markdown == preview["markdown"].encode()
    return {
        "selection": digest(selected),
        "target": digest(target),
        "resolution_markdown": sha(markdown),
        "resolution_preview": digest(preview),
    }


def preserved(operator, viewer, receipt):
    for client in (operator, viewer):
        assert capture(client, receipt) == receipt["original_hashes"]


def rank(case, result):
    chunks = result.get("chunks", [])
    rows = [
        {
            "rank": i,
            "reference": item["reference"],
            "heading_path": item["heading_path"],
            "score": item["score"],
        }
        for i, item in enumerate(chunks, 1)
    ]
    guide_rank = next(
        (r["rank"] for r in rows if r["reference"]["document_key"] == case["document_key"]), None
    )
    section_rank = next(
        (
            r["rank"]
            for r in rows
            if r["reference"]["document_key"] == case["document_key"]
            and any(s in r["heading_path"] for s in case["relevant_sections"])
        ),
        None,
    )
    assert len(rows) <= 5 and result["scope"]["kind"] == "Deployment"
    if case.get("symptom_family"):
        assert all(
            not c["applicability"]["symptom_families"]
            or case["symptom_family"] in c["applicability"]["symptom_families"]
            for c in chunks
        )
    assert all(
        c["applicability"]["recovery_templates"] == []
        or case.get("recovery_template") in c["applicability"]["recovery_templates"]
        for c in chunks
    )
    return {
        "id": case["id"],
        "status": result["status"],
        "generation_id": result["generation_id"],
        "source_digest": result["source_digest"],
        "ranking_config_version": result["ranking_config_version"],
        "query_version": result["query_version"],
        "guide_rank": guide_rank,
        "section_rank": section_rank,
        "results": rows,
    }


def negative(lab, ids, case):
    result = lab.trusted(ids, case)
    passed = (
        result.get("error") == case.get("expected_error")
        if "expected_error" in case
        else result.get("status") == case["expected_status"] and not result.get("chunks")
    )
    return {
        "id": case["id"],
        "passed": passed,
        "status": result.get("status"),
        "error": result.get("error"),
        "returned_chunks": len(result.get("chunks", [])),
    }


def manual_diagnosis(operator, cases):
    rows = []
    for case in cases["positives"]:
        if case.get("recovery_template"):
            continue
        ids = create(operator, case["query"])
        result = request(operator, "POST", guidance(ids), json={}).json()["selection"]["retrieval"]
        assert result["scope"]["symptom_family"] is None
        assert result["scope"]["recovery_template"] is None and result["query"] == case["query"]
        assessed = rank(
            {**case, "symptom_family": None}, {**result, "chunks": result["chunks"][:5]}
        )
        rows.append({**assessed, "run_id": ids["run_id"], "observed_scope": result["scope"]})
    assert len(rows) == 10
    return {
        "results": rows,
        "guide_recall_at_5": sum(r["guide_rank"] is not None for r in rows) / 10,
        "guide_mrr_at_5": sum(1 / r["guide_rank"] for r in rows if r["guide_rank"]) / 10,
        "section_recall_at_5": sum(r["section_rank"] is not None for r in rows) / 10,
    }


def prepare(lab, operator, viewer, receipt, cases):
    status = request(operator, "GET", "/api/knowledge/index-status").json()
    assert status["state"] == "not_indexed"
    assert (
        request(viewer, "GET", "/api/knowledge/documents").json()["catalog_empty_reason"]
        == "not_indexed"
    )
    checked(
        lab.compose
        + [
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "dispatch-admin",
            "bootstrap",
            "--fresh-install",
            "--reason",
            "Owned E05 clean installation",
            "--evidence",
            "Fresh knowledge62 database and dispatch volume",
        ]
    )
    manual = next(c for c in cases["positives"] if c["path"] == "manual_api")
    ids = create(operator, manual["query"])
    receipt["ids"] = ids
    unavailable = request(operator, "POST", guidance(ids), json={}).json()
    assert unavailable["state"] == "unavailable" and unavailable["reason"] == "not_indexed"
    receipt["initial_index"] = lab.index("indexed")
    expected = {c["document_key"] for c in cases["positives"]}
    for client in (operator, viewer):
        items = request(client, "GET", "/api/knowledge/documents", params={"limit": 100}).json()[
            "items"
        ]
        assert {d["document_key"] for d in items} == expected
        for doc in items:
            data = request(client, "GET", f"/api/knowledge/documents/{doc['id']}").json()
            assert sha(data["content"].encode()) == doc["content_sha256"]
    path = f"/api/incidents/{ids['incident_id']}/runs/{ids['run_id']}"
    deadline = time.monotonic() + 40
    while request(operator, "GET", path).json()["state"] not in {"insufficient_evidence", "failed"}:
        assert time.monotonic() < deadline, "Synthetic worker did not complete."
        time.sleep(0.2)
    before = request(operator, "GET", path).json()
    selected = request(operator, "POST", guidance(ids), json={}).json()
    assert selected["state"] == "saved"
    retrieval = selected["selection"]["retrieval"]
    assert (
        retrieval["scope"]["symptom_family"] is None
        and retrieval["scope"]["recovery_template"] is None
    )
    assert selected == request(operator, "POST", guidance(ids), json={}).json()
    assert selected == request(viewer, "GET", guidance(ids)).json()
    for client, headers, body, code in [
        (viewer, {}, {}, 403),
        (operator, {"X-CSRF-Token": "invalid"}, {}, 403),
        (operator, {}, {"consumer": "analysis"}, 422),
    ]:
        request(client, "POST", guidance(ids), expected=code, headers=headers, json=body)
    assert httpx.get(lab.args.base_url + guidance(ids)).status_code == 401
    after = request(operator, "GET", path).json()
    fields = (
        "state",
        "reason",
        "question",
        "question_source",
        "outcome_code",
        "pack_coverage",
        "diagnosis",
        "recovery",
    )
    assert {k: before[k] for k in fields} == {k: after[k] for k in fields}
    receipt["run_outcome"] = {
        k: after[k] for k in ("state", "reason", "outcome_code", "diagnosis", "recovery")
    }
    receipt["positives"] = []
    for case in cases["positives"]:
        result = retrieval if case["path"] == "manual_api" else lab.trusted(ids, case)
        if case["path"] == "manual_api":
            result = {**result, "chunks": result["chunks"][:5]}
        receipt["positives"].append(rank(case, result))
    receipt["negatives"] = [
        negative(lab, ids, c) for c in cases["negatives"] if c["id"] != "removed-current"
    ]
    unmatched = create(operator, "unmatchedguidancexyz")
    manual_empty = request(operator, "POST", guidance(unmatched), json={}).json()
    assert manual_empty["selection"]["retrieval"]["status"] == "no_matches"
    trusted = lab.trusted(unmatched, {"query": manual["query"]}, pin=True)
    assert trusted["selection"]["id"] != manual_empty["selection"]["id"]
    assert trusted["selection"]["retrieval"]["status"] in {"matched", "partial"}
    assert request(operator, "GET", guidance(unmatched)).json() == manual_empty
    incident = f"/api/incidents/{ids['incident_id']}"
    request(
        operator,
        "POST",
        incident + "/notes",
        expected=201,
        json={"text": "Controlled E05 note.\nHuman quote: giữ nguyên."},
    )
    request(
        operator,
        "POST",
        incident + "/lifecycle",
        json={"action": "resolve", "reason": "Captured E05 acceptance resolution"},
    )
    activity = request(operator, "GET", incident + "/activity").json()["items"]
    receipt["resolution_id"] = next(a["id"] for a in activity if a["kind"] == "resolve")
    receipt["selection_id"] = selected["selection"]["id"]
    receipt["reference"] = retrieval["chunks"][0]["reference"]
    receipt["original_hashes"] = capture(operator, receipt)
    preserved(operator, viewer, receipt)
    receipt["browser_ids"] = create(operator, "memory")
    receipt["manual_diagnosis"] = manual_diagnosis(operator, cases)
    receipt["phases"].append("prepare")


def mutate(lab, operator, viewer, receipt, cases):
    assert receipt["phases"] == ["prepare"]
    preserved(operator, viewer, receipt)
    manifest = json.loads((ROOT / "runbooks/catalog.json").read_text())
    doc = next(d for d in manifest["documents"] if d["document_key"] == "oom-restarts")
    doc["version"] = "acceptance-62"
    source = lab.corpus / "oom-restarts.md"
    source.write_bytes(
        (ROOT / "runbooks/oom-restarts.md").read_bytes()
        + b"\n\nControlled acceptance source update.\n"
    )
    catalog = lab.corpus / "catalog.json"
    catalog.write_text(json.dumps(manifest))
    receipt["updated_index"] = lab.index("indexed")
    preserved(operator, viewer, receipt)
    items = request(operator, "GET", "/api/knowledge/documents", params={"limit": 100}).json()[
        "items"
    ]
    updated = next(d for d in items if d["document_key"] == "oom-restarts")
    assert (
        updated["version"] == "acceptance-62"
        and updated["id"] != receipt["reference"]["document_version_id"]
    )
    manifest["documents"] = [
        d for d in manifest["documents"] if d["document_key"] != "oom-restarts"
    ]
    catalog.write_text(json.dumps(manifest))
    receipt["removed_index"] = lab.index("indexed")
    receipt["negatives"].append(
        negative(
            lab, receipt["ids"], next(c for c in cases["negatives"] if c["id"] == "removed-current")
        )
    )
    manifest["documents"][0]["source_path"] = "runbooks/missing-acceptance.md"
    catalog.write_text(json.dumps(manifest))
    receipt["failed_index"] = lab.index("failed")
    state = request(operator, "GET", "/api/knowledge/index-status").json()
    assert (
        state["state"] == "degraded"
        and state["active_generation"]["id"] == receipt["removed_index"]["generation_id"]
    )
    assert len(request(viewer, "GET", "/api/knowledge/documents").json()["items"]) == 7
    preserved(operator, viewer, receipt)
    receipt["degraded_status"] = state
    receipt["degraded_ids"] = create(operator, "memory")
    saved = request(operator, "POST", guidance(receipt["degraded_ids"]), json={}).json()
    assert saved["selection"]["retrieval"]["degraded"]
    receipt["phases"].append("mutate")


def finish(lab, operator, viewer, receipt, cases):
    assert receipt["phases"] == ["prepare", "mutate"]
    manifest = json.loads((ROOT / "runbooks/catalog.json").read_text())
    manifest["documents"] = [
        d for d in manifest["documents"] if d["document_key"] != "oom-restarts"
    ]
    (lab.corpus / "catalog.json").write_text(json.dumps(manifest))
    receipt["retry_index"] = lab.index("unchanged")
    assert request(operator, "GET", "/api/knowledge/index-status").json()["state"] == "ready"
    checked(lab.compose + ["restart", "db"])
    checked(lab.compose + ["restart", "api", "worker", "web"])
    deadline = time.monotonic() + 45
    while True:
        try:
            if httpx.get(lab.args.base_url + "/api/health/ready").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        assert time.monotonic() < deadline, "Owned stack did not restart."
        time.sleep(0.3)
    preserved(operator, viewer, receipt)
    incident = f"/api/incidents/{receipt['ids']['incident_id']}"
    request(
        operator,
        "POST",
        incident + "/lifecycle",
        json={"action": "reopen", "reason": "Verify later Run preserves saved resolution"},
    )
    workload = request(operator, "GET", incident).json()["workload_id"]
    later = request(
        operator,
        "POST",
        incident + "/runs",
        expected=201,
        headers={"Idempotency-Key": str(uuid4())},
        json={"workload_id": workload, "reason": "memory", "window": {"preset": 15}},
    ).json()
    later_ids = {"incident_id": receipt["ids"]["incident_id"], "run_id": later["run_id"]}
    newer = request(operator, "POST", guidance(later_ids), json={}).json()
    assert newer["selection"]["id"] != receipt["selection_id"]
    preserved(operator, viewer, receipt)
    assert (
        sha(request(operator, "GET", incident + "/handoff").content)
        != receipt["original_hashes"]["resolution_markdown"]
    )
    for file in (ROOT / "runbooks").iterdir():
        (lab.corpus / file.name).write_bytes(file.read_bytes())
    receipt["restored_release_index"] = lab.index("reactivated")
    assert (
        receipt["restored_release_index"]["generation_id"]
        == receipt["initial_index"]["generation_id"]
    )
    preserved(operator, viewer, receipt)
    receipt["restart_and_saved_resolution_preservation"] = True
    receipt["later_run_id"] = later["run_id"]
    receipt["phases"].append("finish")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "mutate", "finish"))
    parser.add_argument("--compose-file", action="append", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--base-url", required=True, type=origin)
    parser.add_argument("--web-origin", required=True, type=origin)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Optimized Python disables acceptance assertions.")
    password = sys.stdin.readline().rstrip("\r\n")
    assert len(password) >= 12
    cases = json.loads(CASES.read_text())
    assert len(cases["positives"]) == 16 and len(cases["negatives"]) == 4
    lab = Lab(args)
    if args.phase == "prepare":
        assert not args.output.exists(), "Use a fresh receipt and database."
        assert all(
            (lab.corpus / p.name).read_bytes() == p.read_bytes()
            for p in (ROOT / "runbooks").iterdir()
        )
        for role in ("operator", "viewer"):
            lab.cli("account", role, "--role", role, "--password-stdin", stdin=password + "\n")
        receipt = {
            "schema": "Knowledge62Acceptance/v1",
            "product_baseline": cases["product_baseline"],
            "candidate_commit": checked(["git", "rev-parse", "HEAD"]),
            "probe_commit": checked(["git", "rev-parse", "HEAD"]),
            "probe_sha256": sha(Path(__file__).read_bytes()),
            "fixture_sha256": sha(CASES.read_bytes()),
            "environment": lab.identity,
            "images": lab.images,
            "phases": [],
        }
    else:
        receipt = json.loads(args.output.read_text())
        assert (
            receipt["fixture_sha256"] == sha(CASES.read_bytes()) and receipt["images"] == lab.images
        )
    with ExitStack() as stack:
        operator = login(stack, args.base_url, args.web_origin, "operator", password)
        viewer = login(stack, args.base_url, args.web_origin, "viewer", password)
        try:
            globals()[args.phase](lab, operator, viewer, receipt, cases)
        except Exception as exc:
            receipt["failure"] = {
                "phase": args.phase,
                "type": type(exc).__name__,
                "message": str(exc)[:500],
            }
            args.output.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
            raise
    positive = receipt.get("positives", [])
    receipt["metrics"] = {
        "positive_cases": len(positive),
        "guide_recall_at_5": sum(r["guide_rank"] is not None for r in positive) / 16,
        "guide_mrr_at_5": sum(1 / r["guide_rank"] for r in positive if r["guide_rank"]) / 16,
        "section_recall_at_5": sum(r["section_rank"] is not None for r in positive) / 16,
        "section_mrr_at_5": sum(1 / r["section_rank"] for r in positive if r["section_rank"]) / 16,
        "negatives_passed": sum(n["passed"] for n in receipt.get("negatives", [])),
    }
    receipt["recorded_at"] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"phases": receipt["phases"], "metrics": receipt["metrics"]}))
    return (
        0
        if receipt["metrics"]["guide_recall_at_5"] == 1
        and receipt.get("manual_diagnosis", {}).get("guide_recall_at_5") == 1
        and all(n["passed"] for n in receipt["negatives"])
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
