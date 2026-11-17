"""Bind one actual KRR invocation to independently qualified source evidence."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
import os
from pathlib import Path
import re
from uuid import uuid4
from urllib.parse import urlsplit

import httpx

from cloudops.features.recommendations import service
from cloudops.features.recommendations.krr_protocol import (
    ContractError,
    PROFILE,
    bound_snapshot,
    digest,
    number,
    parse_json,
    snapshot,
    validate_request,
)
from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.recommendations.qualification import load_history, qualify
from cloudops.features.recommendations.schemas import (
    Coverage,
    Quantity,
    ReportIdentity,
    ReportProfile,
    ReportResult,
    ReportWindow,
    ResourceAdvice,
    ToolInvocation,
    Unavailable,
    Unset,
)
from cloudops.features.recommendations.sources import SourceFailure, SourceReader
from cloudops.features.workloads.kubernetes import quantity
from cloudops.shared.database import sessions
from cloudops.shared.errors import DomainError


def call_runner(body, *, before_call):
    url = os.environ.get("KRR_RUNNER_URL", "").rstrip("/")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "\\" in url
    ):
        raise ContractError("runner_configuration_invalid")
    try:
        key = Path(os.environ["KRR_RUNNER_KEY"]).read_text().strip()
    except (OSError, KeyError):
        raise ContractError("runner_configuration_invalid") from None
    if not key or len(key) > 4096:
        raise ContractError("runner_configuration_invalid")
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=150) as client:
        before_call()
        with client.stream(
            "POST",
            url + "/report",
            json=body,
            headers={"Authorization": "Bearer " + key, "Accept-Encoding": "identity"},
        ) as response:
            if response.status_code != 200:
                raise ContractError("runner_unavailable")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ContractError("runner_payload_limit")
            raw = bytearray()
            for chunk in response.iter_raw():
                raw.extend(chunk)
                if len(raw) > 262_144:
                    raise ContractError("runner_payload_limit")
    return parse_json(raw)


def envelope(value, body, sent_at):
    keys = {
        "protocol_version",
        "request_sha256",
        "state",
        "reason",
        "tool_version",
        "image_digest",
        "invocation_started_at",
        "invocation_finished_at",
        "tool_exit_code",
        "timed_out",
        "output_sha256",
        "scope_before",
        "scope_after",
        "containers",
    }
    try:
        if (
            set(value) != keys
            or value["protocol_version"] != 1
            or type(value["protocol_version"]) is not int
            or value["request_sha256"] != digest(body)
            or value["tool_version"] != PROFILE["tool_version"]
            or value["image_digest"] != body["profile"]["image_digest"]
            or value["state"] not in {"completed", "unavailable"}
            or type(value["timed_out"]) is not bool
        ):
            raise ContractError()
        code = value["tool_exit_code"]
        if code is not None and (type(code) is not int or not -255 <= code <= 255):
            raise ContractError()
        output = value["output_sha256"]
        if output is not None and (
            not isinstance(output, str) or not re.fullmatch(r"[a-f0-9]{64}", output)
        ):
            raise ContractError()
        started = (
            datetime.fromisoformat(value["invocation_started_at"])
            if value["invocation_started_at"]
            else None
        )
        finished = (
            datetime.fromisoformat(value["invocation_finished_at"])
            if value["invocation_finished_at"]
            else None
        )
        if (
            bool(started) != bool(finished)
            or started
            and (
                started.tzinfo is None
                or finished.tzinfo is None
                or not sent_at - timedelta(seconds=5)
                <= started
                <= finished
                <= datetime.now(timezone.utc) + timedelta(seconds=5)
                or finished - started > timedelta(seconds=150)
            )
        ):
            raise ContractError("tool_window_invalid")
        for current in (value["scope_before"], value["scope_after"]):
            if current is not None:
                bound_snapshot(current, body["identity"])
        if value["state"] == "completed" and (
            not started
            or code != 0
            or output is None
            or value["timed_out"]
            or value["scope_before"] is None
            or value["scope_before"] != value["scope_after"]
        ):
            raise ContractError()
        invocation = ToolInvocation(
            request_sha256=digest(body),
            started_at=started,
            finished_at=finished,
            timed_out=value["timed_out"],
            target_before_sha256=digest(value["scope_before"]) if value["scope_before"] else None,
            target_after_sha256=digest(value["scope_after"]) if value["scope_after"] else None,
        )
        window = (
            ReportWindow(
                kind="query_envelope",
                start=started.replace(second=0, microsecond=0) - timedelta(hours=6),
                end=finished,
                raw_input_start=started.replace(second=0, microsecond=0)
                - timedelta(hours=6, seconds=75),
                raw_input_end=finished,
            )
            if started
            else None
        )
        return invocation, window
    except ContractError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ContractError() from None


def value(number_, resource):
    if number_ is None:
        return Unset()
    number(number_)
    if resource == "memory":
        if number_ != int(number_):
            raise ContractError()
        return Quantity(value=str(int(number_)), unit="bytes")
    return Quantity(value=format(Decimal(str(number_)), "f"), unit="cores")


def advice(identity, source, evidence, containers):
    if not isinstance(containers, list) or len(containers) != len(identity.containers):
        raise ContractError("tool_scope_mismatch")
    scans = {c["container"]: c for c in containers}
    if len(scans) != len(containers) or set(scans) != set(identity.containers):
        raise ContractError("tool_scope_mismatch")
    templates = {c["name"]: c for c in snapshot(source.before)["containers"]}
    resources = []
    for container in identity.containers:
        scan = scans[container]
        pods = [p for p in evidence.pods if p.container == container]
        expected_pods = {p.pod for p in pods}
        tool_pods = {p["name"] for p in scan["pods"]}
        if len(tool_pods) != len(scan["pods"]) or tool_pods - expected_pods:
            raise ContractError("tool_scope_mismatch")
        for resource in ("cpu", "memory"):
            metric = scan["resources"][resource]
            selected = next(
                r for r in evidence.resources if r.container == container and r.resource == resource
            )
            reasons = list(selected.reasons)
            state = selected.state
            current = {}
            for kind in ("requests", "limits"):
                configured = templates[container][kind].get(resource)
                actual = metric["current"][kind]
                try:
                    configured_value = quantity(configured) if configured is not None else None
                except DomainError:
                    raise ContractError("tool_current_mismatch") from None
                if (
                    actual == "?"
                    or (configured is None) != (actual is None)
                    or configured is not None
                    and configured_value != Decimal(str(number(actual)))
                ):
                    raise ContractError("tool_current_mismatch")
                current[kind] = value(actual, resource)
            observed = number(metric["observed"], unknown=True)
            recommended = metric["recommended"]
            request, limit = (
                number(recommended["requests"], unknown=True),
                number(recommended["limits"], unknown=True),
            )
            unknown = request == "?" and limit == "?"
            if not unknown:
                if observed is None or observed == "?" or request is None or request == "?":
                    raise ContractError()
                expected = (
                    max(math.ceil(observed * 1000) / 1000, 0.01)
                    if resource == "cpu"
                    else max(math.ceil(observed * 1.15 / (1024**2)) * (1024**2), 104857600)
                )
                if request != expected or (
                    limit is not None if resource == "cpu" else limit != expected
                ):
                    raise ContractError("tool_numeric_mismatch")
            if unknown or set(metric["pods"]) != expected_pods or tool_pods != expected_pods:
                reasons.append("tool_metric_insufficient")
                if state == "eligible":
                    state = "insufficient_data"
            state = "available" if state == "eligible" else state
            coverage_rows = [getattr(p, resource) for p in pods]
            coverage = None
            if coverage_rows:
                expected = sum(c.expected_bins for c in coverage_rows)
                covered = sum(c.covered_bins for c in coverage_rows)
                first = [c.first_sample_at for c in coverage_rows if c.first_sample_at]
                last = [c.last_sample_at for c in coverage_rows if c.last_sample_at]
                coverage = Coverage(
                    expected_bins=expected,
                    covered_bins=covered,
                    ratio=covered / expected,
                    longest_gap_seconds=max(c.longest_gap_seconds for c in coverage_rows),
                    first_sample_at=min(first) if first else None,
                    last_sample_at=max(last) if last else None,
                    reasons=sorted({r for c in coverage_rows for r in c.reasons})[:16],
                )
            reasons = sorted(set(reasons))[:16]
            suppressed = Unavailable(reason=reasons[0] if reasons else "source_evidence_incomplete")
            resources.append(
                ResourceAdvice(
                    container=container,
                    resource=resource,
                    state=state,
                    current_request=current["requests"],
                    current_limit=current["limits"],
                    observed=value(observed, resource)
                    if observed is not None and observed != "?" and not evidence.reasons
                    else None,
                    statistic="percentile_95" if resource == "cpu" else "maximum",
                    statistic_definition="Maximum of per-Pod 95th percentiles of 75-second CPU rates"
                    if resource == "cpu"
                    else "Maximum working set on the pinned tool’s 75-second subquery grid",
                    recommended_request=value(request, resource)
                    if state == "available"
                    else suppressed,
                    recommended_limit=value(limit, resource)
                    if state == "available"
                    else suppressed,
                    buffer_percent=0 if resource == "cpu" else 15,
                    minimum=Quantity(value="0.01", unit="cores")
                    if resource == "cpu"
                    else Quantity(value="104857600", unit="bytes"),
                    rounding="Upward to 1 millicore" if resource == "cpu" else "Upward to 1 MiB",
                    coverage=coverage,
                    reasons=reasons,
                )
            )
    return resources


def generate(report_id, *, before_call):
    with sessions()() as db:
        report = db.get(RecommendationReport, report_id)
        identity = ReportIdentity.model_validate(report.input_identity)
        profile = ReportProfile.model_validate(report.profile)
        base = service.unavailable(report, "runner_unavailable")
    if profile.history_hours != 6:
        return base.model_copy(
            update={"state": "insufficient_data", "reason": "history_window_unqualified"}
        )
    body = {
        "protocol_version": 1,
        "request_id": str(uuid4()),
        "identity": identity.model_dump(mode="json"),
        "profile": profile.model_dump(mode="json"),
    }
    try:
        validate_request(body, os.environ.get("KRR_RUNNER_IMAGE_DIGEST", ""))
        sent_at = datetime.now(timezone.utc)
        result = call_runner(body, before_call=before_call)
        invocation, window = envelope(result, body, sent_at)
        base = base.model_copy(
            update={
                "source": "live_krr",
                "tool_invocation": invocation,
                "window": window,
                "tool_exit_code": result["tool_exit_code"],
                "output_sha256": result["output_sha256"],
            }
        )
        if result["state"] != "completed":
            return base.model_copy(
                update={"reason": "runner_timeout" if invocation.timed_out else "runner_failed"}
            )
        with sessions()() as db:
            history = load_history(db, identity, window)
        before_call()
        source = asyncio.run(SourceReader().read(identity, window))
        if snapshot(source.before) != result["scope_before"]:
            raise ContractError("runner_target_changed")
        evidence = qualify(identity, window, history, source)
        resources = advice(identity, source, evidence, result["containers"])
        states = {r.state for r in resources}
        state = (
            "available"
            if "available" in states
            else "unavailable"
            if "unavailable" in states
            else "insufficient_data"
            if "insufficient_data" in states
            else "excluded"
        )
        return ReportResult(
            state=state,
            reason="qualified_advice"
            if state == "available"
            else "resource_evidence_incomplete"
            if state == "insufficient_data"
            else "resource_policy_excluded",
            source="live_krr",
            identity=identity,
            profile=profile,
            window=window,
            resources=resources,
            tool_exit_code=result["tool_exit_code"],
            output_sha256=result["output_sha256"],
            query_digest=evidence.query_digest,
            source_evidence=evidence,
            tool_invocation=invocation,
            limitations=[
                "Advisory only; no model call or Kubernetes write.",
                "Tool version names the pinned source release; embedded metadata is dev and the full source tree is verified.",
                "The query envelope conservatively contains the tool's rolling six-hour queries; covered gaps remain visible.",
                "Read-only invocation can repeat after worker loss; publication remains lease/permit fenced.",
                *evidence.limitations,
            ],
        )
    except (ContractError, SourceFailure) as error:
        return base.model_copy(update={"reason": error.reason})
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        return base.model_copy(update={"reason": "runner_contract_unavailable"})
