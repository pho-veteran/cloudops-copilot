"""Exercise E02 against fixed disposable local fixtures; never mutate demo/checkout.

Use the documented lab setup and temporary CA lifecycle. Database records and all
named Docker volumes are retained. This is an integration probe, not a release SLO.
"""

import argparse
import base64
from datetime import datetime, timezone
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time
from urllib.parse import quote
from uuid import uuid4

import httpx
from local_lab import ROOT as root, COMPOSE
from cloudops.features.workloads.scope_config import cluster_fingerprint

folder = root / "tmp/e02-baselines"
reader_folder = folder / "reader"
origin = "https://localhost:8443"
compose = [*COMPOSE, "-f", str(folder / "compose.json")]
FIXTURES = {
    "Deployment": {"e02-ready", "e02-recreate", "e02-config", "e02-unlisted"}
    | {f"e02-budget-{i:03}" for i in range(96)},
    "ConfigMap": {"e02-settings", "e02-settings-new"},
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def lf_hash(value):
    return hashlib.sha256(value.replace(b"\r\n", b"\n")).hexdigest()


def git_bytes(*args):
    return subprocess.check_output(["git", *args], cwd=root, stdin=subprocess.DEVNULL)


def receipt_metadata(*extra_probes):
    status = git_bytes("status", "--porcelain=v1", "--untracked-files=all").decode("utf-8")
    require(
        not status, "Commit the candidate and keep the checkout clean before recording evidence."
    )
    head = git_bytes("rev-parse", "HEAD").decode().strip()
    require(
        app(
            "from urllib.parse import urlsplit; from cloudops.shared.config import settings; print(urlsplit(settings().database_url).path == '/cloudops28_lab')"
        )
        == "True",
        "Use only the separate cloudops28_lab application database.",
    )
    package = root / "services/backend/src/cloudops"
    actual = json.loads(
        app(
            "import hashlib,json; from pathlib import Path; p=Path('/workspace/services/backend/src/cloudops'); print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes().replace(b'\\r\\n',b'\\n')).hexdigest() for f in p.rglob('*.py')}))"
        )
    )
    checkout = {
        str(path.relative_to(package)).replace("\\", "/"): lf_hash(path.read_bytes())
        for path in package.rglob("*.py")
    }
    committed = {
        name: lf_hash(git_bytes("show", f"{head}:services/backend/src/cloudops/{name}"))
        for name in checkout
    }
    require(
        actual == checkout == committed,
        "Runtime, checkout and committed backend sources differ after LF normalization.",
    )
    probes = {}
    for path in {
        Path(__file__).resolve(),
        root / "scripts/local_lab.py",
        *(Path(p).resolve() for p in extra_probes),
    }:
        name = str(path.relative_to(root)).replace("\\", "/")
        value = lf_hash(path.read_bytes())
        require(
            value == lf_hash(git_bytes("show", f"{head}:{name}")),
            "Probe source differs from its committed Git blob.",
        )
        probes[name] = value
    policy_hash = hashlib.sha256((reader_folder / "workloads.json").read_bytes()).hexdigest()
    actual_policy = app(
        "import hashlib; from cloudops.shared.config import settings; print(hashlib.sha256(settings().workloads_file.read_bytes()).hexdigest())"
    )
    require(policy_hash == actual_policy, "Running API uses a different scope policy file.")
    return {
        "runtime_commit": head,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "git_worktree_status": [],
        "source_hash_normalization": "SHA-256 after CRLF to LF normalization; no other whitespace changes",
        "runtime_checkout_git_sources_match": True,
        "source_sha256_lf": committed,
        "probe_sources_sha256_lf": probes,
        "scope_policy_sha256": policy_hash,
        "browser_acceptance": "Pending trusted HTTPS browser execution; API evidence does not close E02.",
    }


def call(*args, input=None):
    return command(list(args), input)


def passwords_for(args):
    return {
        role: sys.stdin.readline().rstrip("\r\n")
        if args.password_stdin
        else getpass(f"E02 {role} password: ")
        for role in ("operator", "viewer")
    }


def command(args, body=None, check=True):
    return subprocess.run(
        args,
        input=body,
        stdin=subprocess.DEVNULL if body is None else None,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=check,
        env=os.environ,
        cwd=root,
    ).stdout.strip()


def kube(*args, body=None):
    return command(
        ["docker", "exec", "-i", "cloudops-e01-https-k3s-1", "/bin/kubectl", *args],
        json.dumps(body) if body else None,
    )


def fixture_metadata(value):
    kind, metadata = value["kind"], value["metadata"]
    name = metadata["name"]
    require(
        name in FIXTURES.get(kind, set()) and metadata.get("namespace") == "demo",
        "Only fixed disposable fixtures in demo may be mutated.",
    )
    require(
        (metadata.get("labels") or {}).get("cloudops-probe") == "e02-baselines",
        "Refusing a resource outside this probe's ownership.",
    )
    return kind, name


def read_fixture(kind, name):
    require(name in FIXTURES.get(kind, set()), "Only fixed disposable fixtures may be mutated.")
    raw = kube("get", kind, name, "-n", "demo", "-o", "json", "--ignore-not-found")
    if not raw:
        return None
    value = json.loads(raw)
    require(fixture_metadata(value) == (kind, name), "Fixture identity changed.")
    return value


def create_fixture(value):
    fixture_metadata(value)
    # Create fails if a concurrent actor acquired the name; never overwrite it.
    return kube("create", "-f", "-", body=value)


def ensure_fixture(value):
    kind, name = fixture_metadata(value)
    if read_fixture(kind, name) is None:
        create_fixture(value)


def apply_fixture(value):
    kind, name = fixture_metadata(value)
    current = read_fixture(kind, name)
    require(current is not None, "Prepared fixture disappeared; inspect before continuing.")
    body = json.loads(json.dumps(value))
    for key in ("uid", "resourceVersion"):
        body["metadata"][key] = current["metadata"][key]
    return kube("apply", "-f", "-", body=body)


def delete_fixture(kind, name):
    current = read_fixture(kind, name)
    require(current is not None, "Prepared fixture disappeared; inspect before continuing.")
    prefix = "/apis/apps/v1" if kind == "Deployment" else "/api/v1"
    resource = "deployments" if kind == "Deployment" else "configmaps"
    kube(
        "delete",
        "--raw",
        f"{prefix}/namespaces/demo/{resource}/{name}",
        "-f",
        "-",
        body={
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {key: current["metadata"][key] for key in ("uid", "resourceVersion")},
        },
    )
    kube("wait", "--for=delete", f"{kind}/{name}", "-n", "demo", "--timeout=45s")


def app(code):
    return command([*compose, "exec", "-T", "api", "python", "-c", code])


def sync():
    return command(
        [
            *compose,
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "cloudops.entrypoints.cli",
            "sync-workloads",
        ]
    )


def get(name):
    return json.loads(kube("get", "deployment", name, "-n", "demo", "-o", "json"))


def hash_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def protected():
    checkout = get("checkout")
    return {
        "uid": checkout["metadata"]["uid"],
        "spec_sha256": hash_value(checkout["spec"]),
        "writer_sha256": hashlib.sha256(
            (root / "tmp/e01-integration/writer/workloads.json").read_bytes()
        ).hexdigest(),
    }


def domain():
    return json.loads(
        app(
            "import json; from sqlalchemy import select; from cloudops.shared.database import sessions; from cloudops.features.recovery.models import ActionPlan,Execution,PlanReview; from cloudops.shared.jobs import Job; from cloudops.features.incidents.models import Incident;\nwith sessions()() as db: print(json.dumps({m.__tablename__:sorted([repr(dict(r)) for r in db.execute(select(m.__table__)).mappings()]) for m in [ActionPlan,Execution,PlanReview,Job,Incident]}))"
        )
    )


def live_read(wid):
    for _ in range(8):
        result = json.loads(
            app(
                f"import asyncio; from uuid import UUID; from cloudops.features.workloads.observation_service import observe_direct; r=asyncio.run(observe_direct(UUID('{wid}'),'recovery_context')); print(r.model_dump_json())"
            )
        )
        if result["outcome"] != "busy":
            return result
        time.sleep(0.5)
    raise RuntimeError("Direct reader remained busy")


def stable(value):
    return {k: v for k, v in value.items() if k != "age_seconds"}


def error_response(response, status, code):
    require(
        response.status_code == status and response.json().get("error", {}).get("code") == code,
        f"Expected {status}/{code}, received {response.status_code}: {response.text}",
    )


def target_snapshot(name):
    value = read_fixture("Deployment", name)
    require(value is not None, "Prepared fixture disappeared.")
    return {key: value["metadata"][key] for key in ("uid", "generation", "resourceVersion")} | {
        "spec_sha256": hash_value(value["spec"])
    }


def seed_legacy(_args):
    print(
        app("""import hashlib,json
from sqlalchemy import select
from urllib.parse import urlsplit
from cloudops.shared.config import settings
from cloudops.shared.database import sessions,require_schema
from cloudops.features.access.models import User
from cloudops.features.workloads.models import Workload,BaselineCandidate,Baseline
from cloudops.features.incidents.models import Incident
if urlsplit(settings().database_url).path != '/cloudops28_lab': raise RuntimeError('Use only the separate cloudops28_lab database.')
require_schema()
with sessions()() as db,db.begin():
    actor=db.scalar(select(User).where(User.username=='e02-operator'))
    if not actor or actor.role!='operator': raise RuntimeError('Provision the named e02-operator test account first.')
    row=db.scalar(select(Workload).where(Workload.cluster=='legacy-fixture',Workload.namespace=='demo',Workload.name=='legacy-e02-fixture',Workload.uid=='legacy-fixture-uid'))
    if row and (row.source!='development_fixture' or row.active): raise RuntimeError('Existing fixture identity has unexpected ownership/state.')
    if row is None:
        row=Workload(cluster='legacy-fixture',namespace='demo',name='legacy-e02-fixture',uid='legacy-fixture-uid',source='development_fixture',active=False)
        db.add(row);db.flush()
    saved=db.scalar(select(Baseline).where(Baseline.workload_id==row.id))
    if saved is None:
        value={'uid':'legacy-fixture-uid','replicas':1,'containers':[{'name':'legacy','image':'fixture:legacy','resources':{}}]}
        digest=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
        candidate=BaselineCandidate(workload_id=row.id,digest=digest,snapshot=value);db.add(candidate);db.flush()
        db.add(Baseline(workload_id=row.id,candidate_id=candidate.id,digest=digest,snapshot=value,confirmed_by=actor.id))
        db.add(Incident(workload_id=row.id,title='Legacy continuity fixture',description='Controlled unverified v0 history; not healthy-rollout or populated-upgrade proof.',created_by=actor.id))
    elif saved.snapshot.get('profile') is not None: raise RuntimeError('Expected legacy v0 fixture; existing records were not modified.')
print('Nonempty legacy continuity fixture retained; no populated-upgrade claim.')""")
    )


def legacy_state():
    return json.loads(
        app(
            "import json; from sqlalchemy import text; from cloudops.shared.database import sessions;\nwith sessions()() as db: print(json.dumps([dict(r) for r in db.execute(text(\"SELECT w.id::text AS workload_id,b.id::text,b.digest,b.snapshot,b.confirmed_by::text,b.confirmed_at::text FROM baselines b JOIN workloads w ON w.id=b.workload_id WHERE w.cluster='legacy-fixture' AND w.name='legacy-e02-fixture'\")).mappings()]))"
        )
    )


def prepare(args):
    folder.mkdir(parents=True, exist_ok=True)
    reader_folder.mkdir(parents=True, exist_ok=True)
    checkout = json.loads(kube("get", "deployment", "checkout", "-n", "demo", "-o", "json"))
    protected = {
        "uid": checkout["metadata"]["uid"],
        "spec_sha256": hashlib.sha256(
            json.dumps(checkout["spec"], sort_keys=True).encode()
        ).hexdigest(),
        "writer_sha256": hashlib.sha256(
            (root / "tmp/e01-integration/writer/workloads.json").read_bytes()
        ).hexdigest(),
    }
    prior = folder / "protected.json"
    if prior.exists():
        require(
            json.loads(prior.read_text()) == protected,
            "Protected checkout/writer policy changed; inspect before preparation.",
        )
    else:
        prior.write_text(json.dumps(protected, indent=2))
    for name in ["e02-ready", "e02-recreate", "e02-config", "e02-unlisted"]:
        if read_fixture("Deployment", name) is not None:
            continue
        container = {
            "name": "app",
            "image": checkout["spec"]["template"]["spec"]["containers"][0]["image"],
            "command": ["python", "-m", "http.server", "8080"],
            "resources": {
                "requests": {"cpu": "10m", "memory": "32Mi"},
                "limits": {"cpu": "100m", "memory": "64Mi"},
            },
            "readinessProbe": {
                "tcpSocket": {"port": 8080},
                "periodSeconds": 1,
                "timeoutSeconds": 1,
            },
            "env": [{"name": "LITERAL_SENTINEL", "value": "private-literal-e02"}],
        }
        if name == "e02-config":
            ensure_fixture(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "namespace": "demo",
                        "name": "e02-settings",
                        "labels": {"cloudops-probe": "e02-baselines"},
                    },
                    "data": {"MESSAGE": "private-config-e02"},
                }
            )
            container["envFrom"] = [{"configMapRef": {"name": "e02-settings"}}]
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "namespace": "demo",
                "name": name,
                "labels": {"cloudops-probe": "e02-baselines"},
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {"containers": [container]},
                },
            },
        }
        (folder / f"{name}.json").write_text(json.dumps(manifest, indent=2))
        create_fixture(manifest)
        kube("rollout", "status", f"deployment/{name}", "-n", "demo", "--timeout=90s")
    ca = call(
        "docker",
        "exec",
        "cloudops-e01-https-k3s-1",
        "cat",
        "/var/lib/rancher/k3s/server/tls/server-ca.crt",
    )
    reader = {
        "server": "https://k3s:6443",
        "token": kube("create", "token", "cloudops-reader", "-n", "demo", "--duration=8h"),
        "ca_data": base64.b64encode((ca + "\n").encode()).decode(),
    }
    (reader_folder / "client.json").write_text(json.dumps(reader))
    denied = {**reader, "token": kube("create", "token", "default", "-n", "demo", "--duration=8h")}
    (folder / "denied.json").write_text(json.dumps(denied))
    scope = {
        "schema_version": 2,
        "cluster": {
            "key": "e02-local-k3s",
            "fingerprint_algorithm": "server-ca-sha256/v1",
            "expected_fingerprint": cluster_fingerprint(reader),
        },
        "deployments": [
            {"namespace": "demo", "name": n}
            for n in ["checkout", "e02-ready", "e02-recreate", "e02-config"]
        ],
    }
    (reader_folder / "workloads.json").write_text(json.dumps(scope, indent=2))
    dbs = call(
        "docker",
        "exec",
        "cloudops-e01-https-db-1",
        "psql",
        "-U",
        "cloudops",
        "-d",
        "postgres",
        "-tAc",
        "SELECT datname FROM pg_database WHERE datname='cloudops28_lab'",
    )
    if not dbs:
        call(
            "docker",
            "exec",
            "cloudops-e01-https-db-1",
            "psql",
            "-U",
            "cloudops",
            "-d",
            "postgres",
            "-c",
            "CREATE DATABASE cloudops28_lab",
        )
    backend = {
        "image": args.backend_image,
        "environment": {
            "DATABASE_URL": f"postgresql+psycopg://cloudops:{quote(os.environ['POSTGRES_PASSWORD'], safe='')}@db:5432/cloudops28_lab"
        },
        "volumes": [f"{reader_folder.as_posix()}:/run/cloudops-reader:ro"],
    }
    override = {"services": {name: backend for name in ["api", "migrate", "worker", "observer"]}}
    override["services"]["https"] = {"image": args.https_image}
    (folder / "compose.json").write_text(json.dumps(override, indent=2))
    print(
        json.dumps(
            {
                "prepared": 4,
                "scoped_disposable": 3,
                "unlisted": 1,
                "protected_checkout": protected["uid"],
                "writer_policy_unchanged": True,
            }
        )
    )


def verify(args):
    passwords = passwords_for(args)
    report = {
        **receipt_metadata(),
        "environment": "real K3s + PostgreSQL + HTTPS; disposable e02-* Deployments",
        "checks": {},
    }
    before_protected = protected()
    require(
        before_protected == json.loads((folder / "protected.json").read_text()),
        "Protected original target/writer policy changed.",
    )
    reader = (reader_folder / "client.json").read_bytes()
    policy = (reader_folder / "workloads.json").read_bytes()
    tls = ssl.create_default_context(cafile=str(args.ca))
    with (
        httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=30) as operator,
        httpx.Client(base_url=origin, verify=tls, trust_env=False, timeout=30) as viewer,
    ):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                if operator.get("/api/health/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        headers, principals = {}, {}
        for role, client in [("operator", operator), ("viewer", viewer)]:
            response = client.post(
                "/api/login",
                json={"username": "e02-" + role, "password": passwords[role]},
                headers={"Origin": origin},
            )
            assert response.status_code == 200, (response.status_code, response.text)
            headers[role] = {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}
            principals[role] = response.json()["principal"]["id"]
        legacy = legacy_state()
        require(
            bool(legacy),
            "Run seed-legacy after provisioning the test accounts; an empty continuity check is invalid.",
        )
        for row in legacy:
            require(
                row["snapshot"].get("profile") is None, "Legacy continuity fixture must remain v0."
            )
            page = viewer.get(f"/api/workloads/{row['workload_id']}/baselines").json()
            assert any(
                item["id"] == row["id"] and item["profile"] == "legacy/v0" for item in page["items"]
            )

        def inventory():
            return operator.get("/api/workloads").json()["items"]

        workloads = {r["name"]: r for r in inventory()}
        assert "e02-unlisted" not in workloads
        for name in ("e02-ready", "e02-recreate", "e02-config"):
            current = read_fixture("Deployment", name)
            assert current is not None and current["metadata"]["uid"] == workloads[name]["uid"]

        def path(name):
            return "/api/workloads/" + workloads[name]["id"]

        def candidate(name):
            response = operator.get(path(name) + "/baseline-candidate")
            assert response.status_code == 200, response.text
            value = response.json()
            assert (
                "private-literal-e02" not in response.text
                and "private-config-e02" not in response.text
            )
            return {"candidate_id": value["id"], "digest": value["digest"]}

        def confirm(name, body):
            return operator.post(path(name) + "/baseline", json=body, headers=headers["operator"])

        def context(name, baseline=None, client=operator):
            return client.get(
                path(name) + "/recovery-context",
                params={"baseline_id": baseline} if baseline else {},
            ).json()

        baseline_ids = {}
        initial_domain = domain()
        before_targets = {
            name: target_snapshot(name) for name in ("e02-ready", "e02-recreate", "e02-config")
        }
        for name in ["e02-ready", "e02-recreate", "e02-config"]:
            body = candidate(name)
            history_before = [
                stable(row) for row in viewer.get(path(name) + "/baselines").json()["items"]
            ]
            error_response(
                viewer.post(path(name) + "/baseline", json=body, headers=headers["viewer"]),
                403,
                "permission_denied",
            )
            assert [
                stable(row) for row in viewer.get(path(name) + "/baselines").json()["items"]
            ] == history_before
            time.sleep(5.3)
            response = confirm(name, body)
            assert response.status_code == 201, response.text
            saved = response.json()
            assert (
                saved["confirmed_by"] == principals["operator"]
                and saved["profile"] == "deployment-images-resources/v1"
                and saved["digest"] == body["digest"]
            )
            assert (
                saved["snapshot"]["provenance"]["first"]["health"]
                == saved["snapshot"]["provenance"]["second"]["health"]
                == "healthy"
            )
            baseline_ids[name] = saved["id"]
            selected = context(name, saved["id"])
            assert (
                stable(selected["baseline"]) == stable(saved) and not selected["execution_eligible"]
            )
            assert stable(context(name, saved["id"], viewer)["baseline"]) == stable(saved)
        assert domain() == initial_domain
        assert {name: target_snapshot(name) for name in before_targets} == before_targets
        report["checks"][
            "preparation_and_both_role_contexts_preserve_all_target_versions_and_domain"
        ] = True
        report["checks"]["viewer_confirmation_denied_with_unchanged_history"] = True
        report["checks"]["healthy_baselines_before_faults_and_exact_actual_consumer"] = True
        # Generation change with exactly the same baseline envelope must invalidate pending confirmation.
        body = candidate("e02-ready")
        manifest = json.loads((folder / "e02-ready.json").read_text())
        changed = json.loads(json.dumps(manifest))
        changed["spec"]["template"]["metadata"]["annotations"] = {"probe": "generation-change"}
        apply_fixture(changed)
        kube("rollout", "status", "deployment/e02-ready", "-n", "demo", "--timeout=90s")
        time.sleep(5.3)
        error_response(confirm("e02-ready", body), 409, "candidate_changed")
        report["checks"]["generation_change_rejects_pending"] = True
        apply_fixture(manifest)
        kube("rollout", "status", "deployment/e02-ready", "-n", "demo", "--timeout=90s")
        # Real fault: use a valid cached image with a crashing command, not registry/network failure.
        broken = json.loads(json.dumps(manifest))
        broken["spec"]["template"]["spec"]["containers"][0]["command"] = [
            "python",
            "-c",
            "raise SystemExit(1)",
        ]
        apply_fixture(broken)
        deadline = time.monotonic() + 100
        fault = None
        while time.monotonic() < deadline:
            read = live_read(workloads["e02-ready"]["id"])
            if read.get("observation") and read["observation"]["health"] == "degraded":
                fault = read
                break
            time.sleep(1)
        assert fault is not None
        error_response(
            operator.get(path("e02-ready") + "/baseline-candidate"), 409, "baseline_unavailable"
        )
        assert context("e02-ready")["reason"] == "missing"
        result = context("e02-ready", baseline_ids["e02-ready"])
        assert result["reason"] == "no_supported_diff" and not result["execution_eligible"]
        report["checks"]["actual_crash_refuses_baseline_and_has_manual_guidance"] = True
        report["fault"] = {
            "health": fault["observation"]["health"],
            "coverage": fault["observation"]["coverage"],
            "reasons": fault["observation"]["reasons"],
        }
        apply_fixture(manifest)
        kube("rollout", "status", "deployment/e02-ready", "-n", "demo", "--timeout=90s")
        assert live_read(workloads["e02-ready"]["id"])["observation"]["health"] == "healthy"
        # envFrom changes both reference identity and generation; content stays unverified.
        config_manifest = json.loads((folder / "e02-config.json").read_text())
        ensure_fixture(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "e02-settings-new",
                    "namespace": "demo",
                    "labels": {"cloudops-probe": "e02-baselines"},
                },
                "data": {"MESSAGE": "private-config-e02"},
            }
        )
        body = candidate("e02-config")
        changed = json.loads(json.dumps(config_manifest))
        changed["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
            {"configMapRef": {"name": "e02-settings-new"}}
        ]
        apply_fixture(changed)
        kube("rollout", "status", "deployment/e02-config", "-n", "demo", "--timeout=90s")
        time.sleep(5.3)
        error_response(confirm("e02-config", body), 409, "candidate_changed")
        result = context("e02-config", baseline_ids["e02-config"])
        assert result["reason"] == "config_unverified" and not result["execution_eligible"]
        report["checks"]["reference_and_generation_change_rejects_confirm"] = True
        report["checks"]["changed_reference_context_is_config_unverified"] = True
        apply_fixture(config_manifest)
        kube("rollout", "status", "deployment/e02-config", "-n", "demo", "--timeout=90s")
        # Scoped name replacement must never transfer historical identity or authority.
        old = workloads["e02-recreate"]
        old_path = path("e02-recreate")
        old_history = operator.get(old_path + "/baselines").json()["items"]
        body = candidate("e02-recreate")
        delete_fixture("Deployment", "e02-recreate")
        sync()
        assert not operator.get(old_path).json()["active"]
        create_fixture(json.loads((folder / "e02-recreate.json").read_text()))
        kube("rollout", "status", "deployment/e02-recreate", "-n", "demo", "--timeout=90s")
        sync()
        new = next(r for r in inventory() if r["name"] == "e02-recreate" and r["active"])
        assert old["id"] != new["id"] and old["uid"] != new["uid"]
        assert operator.get("/api/workloads/" + new["id"] + "/baselines").json()["items"] == []
        error_response(
            operator.post(
                "/api/workloads/" + new["id"] + "/baseline", json=body, headers=headers["operator"]
            ),
            409,
            "candidate_changed",
        )
        assert [stable(r) for r in operator.get(old_path + "/baselines").json()["items"]] == [
            stable(r) for r in old_history
        ]
        assert (
            operator.get(
                old_path + "/recovery-context", params={"baseline_id": baseline_ids["e02-recreate"]}
            ).json()["state"]
            == "manual_only"
        )
        report["replacement"] = {
            "old_workload_id": old["id"],
            "new_workload_id": new["id"],
            "old_uid": old["uid"],
            "new_uid": new["uid"],
            "old_baseline_id": baseline_ids["e02-recreate"],
        }
        report["checks"][
            "replacement_no_baseline_or_candidate_inheritance_old_history_retained"
        ] = True
        workloads["e02-recreate"] = new
        # Denied read and wrong trusted binding remain explicit, without retiring prior admission.
        try:
            (reader_folder / "client.json").write_bytes((folder / "denied.json").read_bytes())
            read = live_read(workloads["e02-ready"]["id"])
            assert read["outcome"] == "failed" and read["reason"] == "permission_denied"
            observation = operator.get(path("e02-ready") + "/observation").json()
            assert (
                observation["summary"]["effective_health"] == "unknown"
                and observation["historical"]
            )
            assert context("e02-ready", baseline_ids["e02-ready"])["reason"] == "unavailable"
            before_inventory = {(r["id"], r["active"]) for r in inventory()}
            sync()
            assert {(r["id"], r["active"]) for r in inventory()} == before_inventory
            wrong = json.loads(policy)
            wrong["cluster"]["expected_fingerprint"] = "0" * 64
            (reader_folder / "workloads.json").write_text(json.dumps(wrong))
            result = subprocess.run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "-m",
                    "cloudops.entrypoints.cli",
                    "sync-workloads",
                ],
                capture_output=True,
                text=True,
                env=os.environ,
                cwd=root,
                stdin=subprocess.DEVNULL,
            )
            assert (
                result.returncode == 1
                and "Reader server/CA does not match the pinned scope policy." in result.stderr
            )
            assert {(r["id"], r["active"]) for r in inventory()} == before_inventory
            report["checks"]["denied_and_wrong_pin_unknown_without_retirement"] = True
        finally:
            (reader_folder / "client.json").write_bytes(reader)
            (reader_folder / "workloads.json").write_bytes(policy)
        sync()
        assert live_read(workloads["e02-ready"]["id"])["observation"]["health"] == "healthy"
        # Stop observer and age naturally; stale is not a database timestamp edit.
        command([*compose, "stop", "observer"])
        live_read(workloads["e02-ready"]["id"])
        time.sleep(61)
        stale = operator.get(path("e02-ready") + "/observation").json()
        assert (
            stale["summary"]["freshness"] == "stale"
            and stale["summary"]["effective_health"] == "unknown"
        )
        report["checks"]["natural_age_stale_without_fake_zero"] = True
        live_read(workloads["e02-ready"]["id"])
        assert (
            operator.get(path("e02-ready") + "/observation").json()["summary"]["effective_health"]
            == "healthy"
        )
        command([*compose, "start", "observer"])
        # Viewer denial is measured on complete protected application rows.
        before = domain()
        response = viewer.post(
            "/api/incidents",
            json={
                "workload_id": workloads["e02-ready"]["id"],
                "title": "Denied E02 Viewer",
                "description": "Controlled permission request",
            },
            headers={**headers["viewer"], "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 403 and domain() == before
        report["checks"]["viewer_investigation_denied_domain_unchanged"] = True
        # Repeat-at-head is not populated migration proof; the _test suite owns that check.
        app(
            "from alembic import command; from alembic.config import Config; command.upgrade(Config('services/backend/alembic.ini'),'head'); command.upgrade(Config('services/backend/alembic.ini'),'head')"
        )
        before_selected = stable(context("e02-ready", baseline_ids["e02-ready"])["baseline"])
        command([*compose, "stop", "api", "worker", "observer"])
        command([*compose, "start", "api", "worker", "observer"])
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                if operator.get("/api/health/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        assert (
            stable(context("e02-ready", baseline_ids["e02-ready"])["baseline"]) == before_selected
        )
        assert legacy_state() == legacy
        report["checks"][
            "repeat_head_upgrade_restart_nonempty_legacy_and_exact_selected_v1_preserved"
        ] = True
        report["legacy_continuity_count"] = len(legacy)
        report["populated_upgrade_scope"] = (
            "Separate PostgreSQL _test suite; this live case checks repeat-at-head and restart only."
        )
    require(
        protected() == before_protected,
        "Protected original target/writer policy changed during verification.",
    )
    report["checks"]["original_checkout_uid_spec_and_writer_allowlist_unchanged"] = True
    report["baselines"] = baseline_ids
    require(
        git_bytes("rev-parse", "HEAD").decode().strip() == report["runtime_commit"],
        "Candidate commit changed during verification.",
    )
    require(
        not git_bytes("status", "--porcelain=v1", "--untracked-files=all").strip(),
        "Checkout changed during verification.",
    )
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "receipt": str(args.output),
                "runtime_commit": report["runtime_commit"],
                "result": "passed",
                "browser_gate": "open",
            }
        )
    )


def cleanup(_args):
    expected = json.loads((folder / "protected.json").read_text())
    require(
        protected() == expected,
        "Protected checkout or writer policy changed; inspect before cleanup.",
    )
    rows = json.loads(
        kube("get", "deployments", "-n", "demo", "-l", "cloudops-probe=e02-baselines", "-o", "json")
    )["items"]
    require(
        all(row["metadata"]["name"] in FIXTURES["Deployment"] for row in rows),
        "Unexpected owned Deployment; inspect before cleanup.",
    )
    for row in rows:
        delete_fixture("Deployment", row["metadata"]["name"])
    refs = json.loads(
        kube("get", "configmaps", "-n", "demo", "-l", "cloudops-probe=e02-baselines", "-o", "json")
    )["items"]
    require(
        all(row["metadata"]["name"] in FIXTURES["ConfigMap"] for row in refs),
        "Unexpected owned ConfigMap; inspect before cleanup.",
    )
    for row in refs:
        delete_fixture("ConfigMap", row["metadata"]["name"])
    sync()
    require(protected() == expected, "Protected checkout/writer policy changed during cleanup.")
    print("Only owned E02 fixture resources removed; database and authority volumes retained.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser(
        "prepare", help="Create labeled fixtures and a separate application database/overlay."
    )
    prep.add_argument("--backend-image", default="cloudops-local-backend")
    prep.add_argument("--https-image", default="cloudops-local-https")
    probe = sub.add_parser(
        "verify", help="Exercise healthy baselines before faults and verify exact history/context."
    )
    probe.add_argument("--ca", type=Path, default=folder / "root.crt")
    probe.add_argument("--output", type=Path, default=folder / "integrated-api.json")
    probe.add_argument("--password-stdin", action="store_true")
    sub.add_parser(
        "cleanup", help="Remove only the fixed labeled fixtures; retain all records/volumes."
    )
    sub.add_parser(
        "seed-legacy", help="Create or retain the explicitly synthetic v0 continuity fixture."
    )
    args = parser.parse_args()
    if not __debug__:
        parser.error(
            "Run verification without -O/PYTHONOPTIMIZE; acceptance assertions must remain active."
        )
    if not os.environ.get("POSTGRES_PASSWORD"):
        parser.error("Set POSTGRES_PASSWORD for the existing disposable local lab.")
    {"prepare": prepare, "verify": verify, "cleanup": cleanup, "seed-legacy": seed_legacy}[
        args.command
    ](args)


if __name__ == "__main__":
    main()
