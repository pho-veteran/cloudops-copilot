"""Dependency-free allowlist shared with the separately pinned KRR image."""

import base64
import hashlib
import hmac
import json
import math
import re
import ssl
from urllib.parse import urlsplit
from uuid import UUID

VERSION = "1.31.1"
UPSTREAM_COMMIT = "f21d482d6974699ccd5bb07ef9e208430fbcafeb"
SOURCE_TREE_SHA256 = "cb1a4ba977fe49accee4b7a2bab1e3ef25fbf8501b3f739ac49653b61a61540b"
BASE_IMAGE = "sha256:835bf3b6fec4d8f27d96d236a36542bd87bfcb4f12f3537f8dce7f886916ed9c"
PROFILE = {
    "tool_version": VERSION,
    "upstream_commit": UPSTREAM_COMMIT,
    "source_tree_sha256": SOURCE_TREE_SHA256,
    "embedded_version": "dev",
    "strategy": "simple",
    "history_hours": 6,
    "timeframe_minutes": 1.25,
    "cpu_percentile": 95,
    "memory_buffer_percent": 15,
    "minimum_cpu_millicores": 10,
    "minimum_memory_bytes": 104857600,
    "points_required": 100,
    "max_workers": 2,
    "allow_hpa": False,
    "use_oomkill_data": False,
    "policy_version": "e08-coverage-v1",
}
IDENTITY_FIELDS = {
    "workload_id",
    "cluster",
    "cluster_fingerprint",
    "namespace",
    "name",
    "uid",
    "observation_id",
    "observed_at",
    "template_digest",
    "resources_digest",
    "pod_template_digest",
    "containers",
}
DNS = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,98}[a-z0-9])?$")


class ContractError(Exception):
    def __init__(self, reason="tool_output_invalid"):
        self.reason = reason
        super().__init__(reason)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def metrics_binding(config):
    try:
        server = config["server"].rstrip("/")
        url = urlsplit(server)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or any(c in server for c in "\\?#@%")
            or not server.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in server)
            or (url.path and not re.fullmatch(r"/[A-Za-z0-9._~/-]+", url.path))
            or ".." in url.path.split("/")
            or (url.port is not None and not 1 <= url.port <= 65535)
            or config["schema"] != "PrometheusReadConfig/v1"
        ):
            raise ValueError
        pem = base64.b64decode(config["ca_data"], validate=True).decode("ascii")
        if not ssl.create_default_context(cadata=pem).get_ca_certs():
            raise ValueError
        token = config.get("token")
        if token is not None and (
            not isinstance(token, str)
            or len(token) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in token)
        ):
            raise ValueError
        return digest(
            {
                "server": server,
                "ca_data": config["ca_data"],
                "endpoint_id": config["endpoint_id"],
                "producer_version": config["producer_version"],
                "credential_sha256": digest(token),
                "step_seconds": config.get("step_seconds", 30),
                "binding": {
                    "deployment_uid_label": "deployment_uid",
                    "namespace_label": "namespace",
                    **config.get("binding", {}),
                },
            }
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ContractError("runner_metrics_configuration_invalid") from None


def metrics_attestation(binding, key):
    if not isinstance(binding, str) or not re.fullmatch(r"[a-f0-9]{64}", binding) or not key:
        raise ContractError("runner_metrics_configuration_invalid")
    return hmac.new(
        key.encode(), ("CloudOpsKRRMetricsSource/v1\n" + binding).encode(), hashlib.sha256
    ).hexdigest()


def parse_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError()
            result[key] = value
        return result

    def constant(_value):
        raise ContractError()

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ContractError() from None


def validate_request(body, image_digest):
    try:
        if (
            set(body)
            != {"protocol_version", "request_id", "identity", "profile", "metrics_source_sha256"}
            or type(body["protocol_version"]) is not int
            or body["protocol_version"] != 1
        ):
            raise ValueError
        UUID(body["request_id"])
        if not isinstance(body["metrics_source_sha256"], str) or not re.fullmatch(
            r"[a-f0-9]{64}", body["metrics_source_sha256"]
        ):
            raise ValueError
        identity, profile = body["identity"], body["profile"]
        if (
            set(identity) != IDENTITY_FIELDS
            or profile != {**PROFILE, "image_digest": image_digest}
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest)
        ):
            raise ValueError
        if profile["allow_hpa"] is not False or profile["use_oomkill_data"] is not False:
            raise ValueError
        if any(
            not isinstance(identity[k], str) or not DNS.fullmatch(identity[k])
            for k in ("namespace", "name")
        ):
            raise ValueError
        if not isinstance(identity["uid"], str) or not 1 <= len(identity["uid"]) <= 100:
            raise ValueError
        if any(
            not isinstance(identity[k], str) or not re.fullmatch(r"[a-f0-9]{64}", identity[k])
            for k in (
                "cluster_fingerprint",
                "template_digest",
                "pod_template_digest",
                "resources_digest",
            )
        ):
            raise ValueError
        containers = identity["containers"]
        if (
            not isinstance(containers, list)
            or not 1 <= len(containers) <= 32
            or len(set(containers)) != len(containers)
            or any(not isinstance(c, str) or not DNS.fullmatch(c) for c in containers)
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ContractError("runner_request_invalid") from None
    return body


def snapshot(deployment):
    try:
        if deployment["kind"] != "Deployment" or deployment["apiVersion"] != "apps/v1":
            raise ValueError
        meta, spec = deployment["metadata"], deployment["spec"]
        fields = [
            {
                "name": c["name"],
                "image": c["image"],
                **{
                    kind: {
                        r: c.get("resources", {}).get(kind, {})[r]
                        for r in ("cpu", "memory")
                        if c.get("resources", {}).get(kind, {}).get(r) is not None
                    }
                    for kind in ("requests", "limits")
                },
            }
            for c in spec["template"]["spec"]["containers"]
        ]
        fields.sort(key=lambda item: item["name"])
        if (
            type(meta["generation"]) is not int
            or meta["generation"] < 1
            or not 1 <= len(fields) <= 32
            or len({c["name"] for c in fields}) != len(fields)
        ):
            raise ValueError
        return {
            "namespace": meta["namespace"],
            "name": meta["name"],
            "uid": meta["uid"],
            "generation": meta["generation"],
            "template_digest": digest(fields),
            "resources_digest": digest(
                [{k: c[k] for k in ("name", "requests", "limits")} for c in fields]
            ),
            "pod_template_digest": digest(spec["template"]),
            "selector_digest": digest(spec["selector"]),
            "containers": fields,
        }
    except (KeyError, TypeError, ValueError):
        raise ContractError("runner_target_unproven") from None


def bound_snapshot(value, identity):
    if any(
        value[k] != identity[k]
        for k in (
            "namespace",
            "name",
            "uid",
            "template_digest",
            "resources_digest",
            "pod_template_digest",
        )
    ) or {c["name"] for c in value["containers"]} != set(identity["containers"]):
        raise ContractError("runner_target_changed")


def number(value, *, unknown=False):
    if unknown and (value is None or value == "?"):
        return value
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 2**53:
        raise ContractError()
    return value


def normalize(raw, observed, body):
    try:
        identity = body["identity"]
        strategy = raw["strategy"]
        if strategy["name"].lower() != "simple" or observed["request_sha256"] != digest(body):
            raise ContractError()
        if observed["parameters"] != {"cpu_min": 10, "mem_min": 100, "max_workers": 2}:
            raise ContractError("tool_profile_mismatch")
        expected = {
            "history_duration": 6,
            "timeframe_duration": 1.25,
            "cpu_percentile": 95,
            "memory_buffer_percentage": 15,
            "points_required": 100,
            "allow_hpa": False,
            "use_oomkill_data": False,
        }
        if any(strategy["settings"].get(k) != v for k, v in expected.items()):
            raise ContractError("tool_profile_mismatch")
        scans, measured = raw["scans"], observed["observations"]
        if (
            not isinstance(scans, list)
            or not isinstance(measured, list)
            or len(scans) != len(identity["containers"])
            or len(measured) != len(scans)
        ):
            raise ContractError("tool_scope_mismatch")
        measurements = {m["container"]: m for m in measured}
        if len(measurements) != len(measured):
            raise ContractError("tool_scope_mismatch")
        result, seen = [], set()
        for scan in scans:
            obj = scan["object"]
            name = obj["container"]
            if (
                (obj["namespace"], obj["name"], obj["kind"])
                != (identity["namespace"], identity["name"], "Deployment")
                or name not in identity["containers"]
                or name in seen
            ):
                raise ContractError("tool_scope_mismatch")
            seen.add(name)
            pods = obj["pods"]
            if (
                not isinstance(pods, list)
                or not 1 <= len(pods) <= 100
                or any(
                    not isinstance(p["name"], str)
                    or not DNS.fullmatch(p["name"])
                    or type(p["deleted"]) is not bool
                    for p in pods
                )
                or len({p["name"] for p in pods}) != len(pods)
            ):
                raise ContractError("tool_scope_mismatch")
            measured = measurements[name]
            if (measured["namespace"], measured["name"], measured["kind"]) != (
                obj["namespace"],
                obj["name"],
                obj["kind"],
            ):
                raise ContractError("tool_scope_mismatch")
            resources = {}
            for resource in ("cpu", "memory"):
                current = {
                    kind: number(obj["allocations"][kind][resource], unknown=True)
                    for kind in ("requests", "limits")
                }
                if "?" in current.values():
                    raise ContractError()
                advice = {
                    kind: number(scan["recommended"][kind][resource]["value"], unknown=True)
                    for kind in ("requests", "limits")
                }
                metric = measured[resource]
                value = number(metric["observed"], unknown=True)
                metric_pods = metric["pods"]
                if (
                    value == "?"
                    or not isinstance(metric_pods, list)
                    or len(metric_pods) > 100
                    or len(set(metric_pods)) != len(metric_pods)
                    or not set(metric_pods) <= {p["name"] for p in pods}
                ):
                    raise ContractError("tool_scope_mismatch")
                if any(type(v) in (int, float) for v in advice.values()) and value is None:
                    raise ContractError()
                if resource == "memory" and any(
                    type(v) in (int, float) and v != int(v)
                    for v in [*current.values(), *advice.values(), value]
                ):
                    raise ContractError()
                resources[resource] = {
                    "current": current,
                    "recommended": advice,
                    "observed": value,
                    "pods": metric_pods,
                }
            result.append(
                {
                    "container": name,
                    "pods": [{"name": p["name"], "deleted": p["deleted"]} for p in pods],
                    "resources": resources,
                }
            )
        return result
    except ContractError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ContractError() from None
