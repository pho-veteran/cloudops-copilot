"""Alert intake fixtures: sender credential, pinned v2 scope and a controlled cluster API."""

import base64
import hashlib
import json
from pathlib import Path

import httpx

from cloudops.features.workloads.scope_config import cluster_fingerprint

TOKEN = "intake-test-token-0123456789abcdefghijklmnop"
CLUSTER = "lab"
SOURCE = "lab-alertmanager"
CERT = Path(__file__).resolve().parent / "fixtures/scope-ca.crt"
READER_SECRET = "reader-secret-sentinel"
ANNOTATION = "annotation-sentinel-must-not-persist"


def configure_intake(tmp_path, monkeypatch, *, cluster=CLUSTER, source=SOURCE):
    from cloudops.features.incidents.intake_settings import intake_config

    path = tmp_path / "intake.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_id": source,
                "cluster": cluster,
                "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
            }
        )
    )
    monkeypatch.setenv("ALERT_INTAKE_CONFIG", str(path))
    intake_config.cache_clear()
    return path


def configure_reader(tmp_path, monkeypatch) -> str:
    config = {
        "server": "https://cluster.example:6443",
        "ca_data": base64.b64encode(CERT.read_bytes()).decode(),
        "token": READER_SECRET,
    }
    path = tmp_path / "reader.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("KUBE_READ_CONFIG", str(path))
    return cluster_fingerprint(config)


def pin_scope(fingerprint: str, targets: dict[str, str], *, namespace="demo") -> dict[str, str]:
    """Publish an active v2 live scope directly; returns name -> workload id."""
    from cloudops.features.workloads.models import ScopeState, Workload
    from cloudops.shared.database import sessions

    ids = {}
    with sessions()() as db, db.begin():
        state = db.get(ScopeState, 1)
        if state is None:
            state = ScopeState(id=1, latest_sequence=0, applied_sequence=0)
            db.add(state)
        state.mode, state.cluster_key, state.fingerprint = "v2", CLUSTER, fingerprint
        for name, uid in targets.items():
            row = Workload(
                cluster=CLUSTER, namespace=namespace, name=name, uid=uid, source="live_kubernetes"
            )
            db.add(row)
            db.flush()
            ids[name] = str(row.id)
    return ids


class Cluster:
    """Fixed-path Kubernetes reads for one namespace; records every request."""

    def __init__(self, namespace="demo"):
        self.namespace = namespace
        self.deployments: dict[str, str] = {}
        self.replicasets: dict[str, tuple[str, list[dict]]] = {}
        self.pods: dict[str, list[dict]] = {}
        self.status: dict[str, int] = {}
        self.calls: list[httpx.Request] = []

    def deployment(self, name, uid):
        self.deployments[name] = uid

    def pod(self, name, deployment, deployment_uid, *, rs_uid="rs-uid", owners=None):
        rs = f"{deployment}-5d9f"
        self.replicasets[rs] = (
            rs_uid,
            [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": deployment,
                    "uid": deployment_uid,
                    "controller": True,
                }
            ],
        )
        self.pods[name] = owners or [
            {
                "apiVersion": "apps/v1",
                "kind": "ReplicaSet",
                "name": rs,
                "uid": rs_uid,
                "controller": True,
            }
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        assert request.method == "GET" and request.url.host == "cluster.example"
        assert request.headers["authorization"] == "Bearer " + READER_SECRET
        if path in self.status:
            return httpx.Response(self.status[path], json={"kind": "Status"})
        base = f"/namespaces/{self.namespace}/"
        kind, _, name = path.rpartition("/")
        meta = {"name": name, "namespace": self.namespace}
        if kind.endswith(base + "deployments") and name in self.deployments:
            body = {"kind": "Deployment", "metadata": {**meta, "uid": self.deployments[name]}}
        elif kind.endswith(base + "replicasets") and name in self.replicasets:
            uid, owners = self.replicasets[name]
            body = {
                "kind": "ReplicaSet",
                "metadata": {**meta, "uid": uid, "ownerReferences": owners},
            }
        elif kind.endswith(base + "pods") and name in self.pods:
            body = {
                "kind": "Pod",
                "metadata": {**meta, "uid": "pod", "ownerReferences": self.pods[name]},
            }
        else:
            return httpx.Response(404, json={"kind": "Status", "reason": "NotFound"})
        return httpx.Response(200, json=body)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


def alert(fingerprint="a1", *, status="firing", starts="2026-11-01T10:00:00.123456789Z", **labels):
    merged = {
        "alertname": "CheckoutDown",
        "namespace": "demo",
        "deployment": "checkout",
        "severity": "critical",
        **labels,
    }
    return {
        "status": status,
        "labels": {k: v for k, v in merged.items() if v is not None},
        "annotations": {"summary": ANNOTATION},
        "startsAt": starts,
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus.invalid/graph",
        "fingerprint": fingerprint,
    }


def envelope(*alerts, status="firing", **extra):
    return {
        "version": "4",
        "groupKey": '{}:{alertname="CheckoutDown"}',
        "truncatedAlerts": 0,
        "status": status,
        "receiver": "cloudops",
        "groupLabels": {"alertname": "CheckoutDown"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager.invalid:9093",
        "alerts": list(alerts) or [alert()],
        **extra,
    }


def deliver(client, body, *, token=TOKEN, headers=None):
    return client.post(
        "/api/intake/alertmanager",
        content=json.dumps(body) if not isinstance(body, (bytes, str)) else body,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
            **(headers or {}),
        },
    )
