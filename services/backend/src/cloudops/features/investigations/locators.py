"""Stable semantic identities, never array offsets, for stored redacted content.

This module has no database/provider dependency and never mutates its input.
Keys are local to an evidence snapshot. A semantic identity is kept separately
for comparison; a log content hash cannot imply event identity across Runs.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from cloudops.features.investigations.citation_schemas import CitationTarget, Locator


def canonical(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def timestamp(value) -> str | None:
    """Normalize supported stored timestamps without inventing an absent time."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
        return str(number.normalize()) if number.is_finite() else None
    except InvalidOperation:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return str(Decimal(str(parsed.astimezone(timezone.utc).timestamp())).normalize())
        except (ValueError, OverflowError):
            return None


def sample_number(value) -> Decimal | None:
    """Compare numeric sample meaning without changing the retained representation."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def equivalent(kind, before, after) -> bool:
    if kind == "sample":
        left, right = sample_number(before), sample_number(after)
        return left is not None and right is not None and left == right
    return canonical(before) == canonical(after)


@dataclass(frozen=True)
class Entry:
    kind: str
    identity: str
    label: str
    value: object
    multiplicity: int = 1
    ambiguous: bool = False

    @property
    def key(self):
        return "v1_" + digest(["locator_v1", self.kind, self.identity])

    def view(self):
        encoded = canonical(self.value)
        truncated = len(encoded.encode("utf-8")) > 1024
        return CitationTarget(
            locator=Locator(kind=self.kind, key=self.key),
            label=self.label[:240],
            value=encoded.encode("utf-8")[:1000].decode("utf-8", errors="ignore") + "…"
            if truncated
            else self.value,
            preview_truncated=truncated,
            multiplicity=self.multiplicity,
            ambiguous=self.ambiguous,
        )


class UnsupportedProjection(ValueError):
    pass


def project(kind: str, version: int, payload: dict) -> list[Entry]:
    if version != 1 or kind not in {"state", "events", "logs", "series", "fixture"}:
        raise UnsupportedProjection("The stored payload version has no locator projection.")
    entries: dict[tuple[str, str], Entry] = {}

    def emit(target_kind, identity, label, value):
        identity = canonical(identity)
        key = (target_kind, identity)
        before = entries.get(key)
        if before:
            # A repeated logical identity with different content is not safe to
            # align. Retain a deterministic representative only for disclosure.
            entries[key] = Entry(
                target_kind,
                identity,
                min(before.label, label),
                min((before.value, value), key=canonical),
                before.multiplicity + 1,
                before.ambiguous or not equivalent(target_kind, before.value, value),
            )
        else:
            entries[key] = Entry(target_kind, identity, label, value)

    def fields(value, path):
        if isinstance(value, dict):
            for key in sorted(value):
                fields(value[key], [*path, key])
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    # Sets of scalar observations are viewable without an array
                    # index; the whole value remains available at source level.
                    emit("field", [*path, "set", digest(item)], "/".join(path), item)
                    continue
                if item.get("uid"):
                    identity = ["uid", item["uid"]]
                elif item.get("pod_uid") and item.get("container", item.get("name")):
                    identity = [
                        "container",
                        item["pod_uid"],
                        item.get("container", item.get("name")),
                    ]
                elif item.get("type") and path and path[-1] == "conditions":
                    identity = ["condition", item["type"]]
                elif item.get("name") and "pods" in path:
                    identity = ["container", item["name"]]
                else:
                    # A content identity is useful for a citation, but cannot
                    # establish that a changed anonymous item is the same item.
                    identity = ["anonymous", digest(item)]
                fields(item, [*path, canonical(identity)])
        else:
            emit("field", path, "/".join(path), value)

    def logs(value, pod=None, container=None, previous=None, observed_at=None, key=""):
        if isinstance(value, dict):
            pod = value.get("pod_uid", value.get("uid", pod) if key == "pods" else pod)
            container = value.get(
                "container", value.get("name", container) if key == "containers" else container
            )
            previous = value.get("previous", previous)
            observed_at = value.get("timestamp", observed_at)
            if "lines" in value or "text" in value:
                if not pod or not container:
                    return
                # A stored segment, rather than a raw line offset, is the stable
                # unit even if several retained lines share a timestamp.
                emit(
                    "log_segment",
                    [
                        pod,
                        container,
                        previous,
                        timestamp(observed_at),
                        digest(value),
                    ],
                    f"{pod}/{container} · retained log segment",
                    value,
                )
                return
            for child_key in sorted(value):
                logs(value[child_key], pod, container, previous, observed_at, child_key)
        elif isinstance(value, list):
            for child in value:
                logs(child, pod, container, previous, observed_at, key)

    if kind in {"state", "fixture"}:
        fields(payload, [])
    elif kind == "events":
        for event in payload.get("events", []):
            if isinstance(event, dict):
                identity = ["uid", event["uid"]] if event.get("uid") else ["content", digest(event)]
                emit("event", identity, str(event.get("reason", "Stored event")), event)
    elif kind == "logs":
        logs(payload)
    else:
        for series in payload.get("series", []):
            if not isinstance(series, dict):
                continue
            identity = {
                key: value for key, value in series.items() if key not in {"samples", "values"}
            }
            emit("series", identity, "Stored metric series", identity)
            for sample in series.get("samples", series.get("values", [])):
                if isinstance(sample, list) and len(sample) == 2:
                    time, value = sample
                elif isinstance(sample, dict):
                    time, value = sample.get("timestamp"), sample.get("value")
                else:
                    continue
                time = timestamp(time)
                if time is not None:
                    emit("sample", [identity, time], f"Sample at {time}", value)
    return [entries[key] for key in sorted(entries)]
