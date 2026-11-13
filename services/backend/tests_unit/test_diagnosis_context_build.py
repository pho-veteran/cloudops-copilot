from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from diagnosis_fixtures import profile

from cloudops.features.diagnosis import versions
from cloudops.features.diagnosis.context import (
    ContextScope,
    InsufficientContext,
    PreparedContext,
    SlotMaterial,
    assemble,
    extract_optional,
    extract_required,
    family_matrix,
    optional_targets,
    required_targets,
)
from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.diagnosis.messages import prepare_payload, serialize_payload
from cloudops.features.diagnosis.versions import SOURCE_CLASSES
from cloudops.features.investigations.locators import Entry, canonical
from cloudops.features.knowledge.guidance_schemas import KnowledgeRunScope


def test_versions_are_unique_and_pinned():
    values = {
        versions.CONTEXT_VERSION: "e06.context/v1",
        versions.TARGET_RULES_VERSION: "e06.targets/v1",
        versions.PROMPT_VERSION: "e06.rca_prompt/v1",
        versions.VALIDATOR_VERSION: "e06.validator/v1",
        versions.PROBE_PROMPT_VERSION: "e06.probe_prompt/v1",
    }
    assert list(values.values()) == [
        "e06.context/v1",
        "e06.targets/v1",
        "e06.rca_prompt/v1",
        "e06.validator/v1",
        "e06.probe_prompt/v1",
    ]
    for value in values:
        assert value.startswith("e06.")
        assert value.endswith("/v1")
    assert len(values) == 5


def test_alias_and_reserve_bounds_match_the_advice_contract():
    assert versions.ALIAS_CAPACITY == 999
    assert versions.REPAIR_RESERVE_BYTES == 4096
    assert versions.MAX_FEEDBACK_BYTES == 2048
    assert versions.MAX_FEEDBACK_BYTES <= versions.REPAIR_RESERVE_BYTES
    assert versions.MAX_FEEDBACK_FAILURES == 10


def test_family_matrix_is_the_d2_table_in_fixed_order():
    families = versions.FAMILY_REQUIRED
    assert list(families) == [
        "crashloop_startup",
        "oom_restarts",
        "traffic_latency_errors",
        "pending_unschedulable",
        "image_pull",
    ]
    crashloop = (
        "kubernetes.state",
        "kubernetes.events",
        "kubernetes.logs.current",
        "kubernetes.logs.previous",
    )
    assert families["crashloop_startup"] == crashloop
    assert families["oom_restarts"] == crashloop
    assert families["traffic_latency_errors"] == (
        "prometheus.request_rate",
        "prometheus.error_ratio",
        "prometheus.p95_latency",
    )
    assert families["pending_unschedulable"] == ("kubernetes.state", "kubernetes.events")
    assert families["image_pull"] == ("kubernetes.state", "kubernetes.events")


def test_every_slot_has_a_source_class_and_cap():
    assert set(versions.SOURCE_CLASSES) == set(versions.SLOT_CAPS)
    assert versions.SOURCE_CLASSES["kubernetes.configmap.metadata"] == "kubernetes_state"
    assert versions.SOURCE_CLASSES["kubernetes.logs.previous"] == "container_logs"
    assert versions.SOURCE_CLASSES["prometheus.restarts"] == "prometheus_series"
    assert versions.SOURCE_CLASSES["development_fixture"] == "synthetic_fixture"
    assert versions.SLOT_CAPS["development_fixture"][0] == 0
    for required_cap, optional_cap, order in versions.SLOT_CAPS.values():
        assert isinstance(required_cap, int) and required_cap >= 0
        assert isinstance(optional_cap, int) and optional_cap >= 0
        assert order in {"key_asc", "newest_first", "group_newest"}
    assert versions.SLOT_CAPS["kubernetes.events"] == (50, 20, "newest_first")
    assert versions.SLOT_CAPS["kubernetes.logs.current"] == (3, 1, "group_newest")


@pytest.mark.parametrize("family", list(versions.FAMILY_REQUIRED))
def test_family_required_slots_are_known_slots(family):
    assert set(versions.FAMILY_REQUIRED[family]) <= set(versions.SLOT_CAPS)


def _entry(kind, identity, value, label="Stored item"):
    return Entry(kind=kind, identity=canonical(identity), label=label, value=value)


def _material(slot, entries=(), *, ambiguous=0, evidence_id=None):
    return SlotMaterial(
        slot=slot,
        source_class=SOURCE_CLASSES[slot],
        evidence_id=evidence_id or uuid4(),
        evidence_sha256="a" * 64,
        payload_version=1,
        coverage_status="available",
        coverage_reason_codes=(),
        content_status="available",
        time_relation="within_requested_window",
        entries=tuple(entries),
        ambiguous_omitted=ambiguous,
    )


def _policy(
    *classes,
    max_message_bytes=65536,
    max_target_bytes=8192,
    policy_version="1",
):
    return EgressPolicy.model_validate(
        {
            "schema": "EgressPolicy/v1",
            "policy_id": "test-policy",
            "policy_version": policy_version,
            "destination": {
                "config_id": "test-config",
                "config_version": "1",
                "endpoint_label": "loopback",
            },
            "allowed_source_classes": list(classes)
            if classes
            else [
                "operator_request",
                "kubernetes_state",
                "kubernetes_events",
                "container_logs",
                "prometheus_series",
                "runbook",
                "synthetic_fixture",
                "diagnostic_fixture",
            ],
            "max_message_bytes": max_message_bytes,
            "max_target_bytes": max_target_bytes,
            "redaction_policy_version": "redaction/v1",
            "deny_patterns": [],
        }
    )


def _families(materials):
    return family_matrix(list(materials), _policy())


def test_state_cap_is_250_in_key_order():
    entries = [
        _entry("field", ["uid", f"item-{index:03d}"], {"uid": f"item-{index:03d}"})
        for index in range(260)
    ]
    extracted = extract_required(_material("kubernetes.state", entries))
    assert len(extracted.entries) == 250
    assert extracted.rule_omitted == 10
    assert [entry.key for entry in extracted.entries] == sorted(entry.key for entry in entries)[
        :250
    ]


def test_events_are_newest_first_and_capped():
    entries = [
        _entry(
            "event",
            ["uid", f"event-{index:02d}"],
            {"uid": f"event-{index:02d}", "lastTimestamp": index},
        )
        for index in range(60)
    ]
    entries.reverse()
    required = extract_required(_material("kubernetes.events", entries))
    assert len(required.entries) == 50
    assert required.rule_omitted == 10
    assert required.entries[0].value["lastTimestamp"] == 59
    optional = extract_optional(_material("kubernetes.events", entries))
    assert len(optional.entries) == 20
    assert optional.entries[0].value["lastTimestamp"] == 59


def test_logs_keep_three_newest_segments_per_group():
    entries = []
    for pod in ("pod-a", "pod-b"):
        for second in range(1, 6):
            identity = [pod, "app", False, str(second), f"digest-{pod}-{second}"]
            entries.append(
                _entry(
                    "log_segment",
                    identity,
                    {"pod_uid": pod, "container": "app", "lines": [f"line-{second}"]},
                )
            )
    extracted = extract_required(_material("kubernetes.logs.current", entries))
    assert len(extracted.entries) == 6
    assert extracted.rule_omitted == 4
    groups = {}
    for entry in extracted.entries:
        groups.setdefault(json.loads(entry.identity)[0], []).append(entry)
    assert len(groups) == 2
    assert all(len(group) == 3 for group in groups.values())
    assert extracted.entries[0].value["lines"] == ["line-5"]
    optional = extract_optional(_material("kubernetes.logs.current", entries))
    assert len(optional.entries) == 2


def test_metrics_include_series_and_samples_with_cap():
    entries = [
        _entry("series", ["metric", f"m-{index:03d}"], {"metric": f"m-{index:03d}"})
        for index in range(260)
    ]
    extracted = extract_required(_material("prometheus.request_rate", entries))
    assert len(extracted.entries) == 250
    assert extracted.rule_omitted == 10
    assert [entry.key for entry in extracted.entries] == sorted(entry.key for entry in entries)[
        :250
    ]


def test_development_fixture_is_never_required_and_optional_capped():
    entries = [_entry("field", ["path", f"k-{index:02d}"], {"k": index}) for index in range(25)]
    material = _material("development_fixture", entries)
    required = extract_required(material)
    assert required.entries == ()
    assert required.rule_omitted == 0
    optional = extract_optional(material)
    assert len(optional.entries) == 20
    assert optional.rule_omitted == 5


def test_required_union_dedupes_across_families_and_orders_aliases():
    state = _material(
        "kubernetes.state",
        [
            _entry("field", ["uid", "pod-1"], {"uid": "pod-1", "phase": "Running"}),
            _entry("field", ["uid", "pod-2"], {"uid": "pod-2", "phase": "Pending"}),
        ],
    )
    events = _material(
        "kubernetes.events",
        [_entry("event", ["uid", "event-1"], {"uid": "event-1", "reason": "BackOff"})],
    )
    logs = _material(
        "kubernetes.logs.current",
        [_entry("log_segment", ["pod-1", "app", False, "1", "d"], {"lines": ["x"]})],
    )
    previous = _material(
        "kubernetes.logs.previous",
        [_entry("log_segment", ["pod-1", "app", True, "1", "d"], {"lines": ["y"]})],
    )
    materials = [state, events, logs, previous]
    targets = required_targets(materials, _families(materials))
    assert [target.alias for target in targets] == [
        "E001",
        "E002",
        "E003",
        "E004",
        "E005",
    ]
    shared = [target for target in targets if target.locator.kind == "field"]
    assert len(shared) == 2
    expected_slots = [
        "crashloop_startup",
        "oom_restarts",
        "pending_unschedulable",
        "image_pull",
    ]
    assert shared[0].family_slots == expected_slots
    assert shared[1].family_slots == expected_slots
    assert targets[0].source_key == "kubernetes.state"
    assert targets[2].source_key == "kubernetes.events"


def test_optional_targets_continue_aliases_in_catalog_order():
    state = _material("kubernetes.state", [_entry("field", ["uid", "pod-1"], {"uid": "pod-1"})])
    events = _material("kubernetes.events", [_entry("event", ["uid", "e-1"], {"uid": "e-1"})])
    logs = _material(
        "kubernetes.logs.current",
        [_entry("log_segment", ["pod-1", "app", False, "1", "d"], {"lines": ["x"]})],
    )
    previous = _material(
        "kubernetes.logs.previous",
        [_entry("log_segment", ["pod-1", "app", True, "1", "d"], {"lines": ["y"]})],
    )
    configmap = _material(
        "kubernetes.configmap.metadata",
        [_entry("field", ["uid", "cm-1"], {"uid": "cm-1"})],
    )
    fixture = _material("development_fixture", [_entry("field", ["path", "k"], {"k": "v"})])
    materials = [state, events, logs, previous, configmap, fixture]
    families = _families(materials)
    required = required_targets(materials, families)
    used = {(t.evidence_id, t.locator.kind, t.locator.key) for t in required}
    optional = optional_targets(materials, families, used)
    assert [target.alias for target in required] == ["E001", "E002", "E003", "E004"]
    assert [target.alias for target in optional] == ["E005", "E006"]
    assert [target.source_key for target in optional] == [
        "kubernetes.configmap.metadata",
        "development_fixture",
    ]


def test_full_value_not_preview():
    full = "x" * 2048
    state = _material(
        "kubernetes.state", [_entry("field", ["uid", "pod-1"], {"uid": "pod-1", "blob": full})]
    )
    events = _material("kubernetes.events", [_entry("event", ["uid", "e-1"], {"uid": "e-1"})])
    materials = [state, events]
    families = [
        family for family in _families(materials) if family.family == "pending_unschedulable"
    ]
    targets = required_targets(materials, families)
    target = next(item for item in targets if item.source_key == "kubernetes.state")
    assert target.value["blob"] == full
    assert len(canonical(target.value).encode("utf-8")) == target.value_bytes
    assert "…" not in canonical(target.value)
    assert "preview_truncated" not in target.model_dump()


def test_rule_caps_are_recorded_as_rule_omitted():
    entries = [
        _entry("field", ["uid", f"item-{index:03d}"], {"uid": f"item-{index:03d}"})
        for index in range(260)
    ]
    material = _material("kubernetes.state", entries)
    extracted = extract_required(material)
    assert extracted.rule_omitted == 10
    families = _families([material])
    assert all(
        next(slot for slot in family.required_slots if slot.slot == "kubernetes.state").usable
        for family in families
        if family.family == "pending_unschedulable"
    )


def _scope(request_text="Why did the workload restart?"):
    now = datetime.now(timezone.utc)
    return ContextScope(
        knowledge=KnowledgeRunScope(
            incident_id=uuid4(),
            run_id=uuid4(),
            workload_id=uuid4(),
            cluster="cluster-a",
            namespace="namespace-a",
            name="app",
            workload_uid="uid-1",
            window_start=now - timedelta(minutes=30),
            window_end=now,
        ),
        request_text=request_text,
        request_source="operator",
        window_source="explicit",
    )


def _build(
    materials,
    *,
    budget=65536,
    max_target_bytes=8192,
    request_text=None,
    policy_version="1",
    scope=None,
):
    scope = scope or (_scope(request_text) if request_text is not None else _scope())
    execution_policy = _policy(
        max_message_bytes=budget,
        max_target_bytes=min(max_target_bytes, budget),
        policy_version=policy_version,
    )
    return assemble(materials, scope, None, execution_policy, profile(), "analysis-1")


def _minimal_materials():
    state = _material("kubernetes.state", [_entry("field", ["uid", "pod-1"], {"uid": "pod-1"})])
    events = _material("kubernetes.events", [_entry("event", ["uid", "e-1"], {"uid": "e-1"})])
    return [state, events]


def test_effective_budget_uses_the_lower_limit():
    scope = _scope()
    execution_policy = _policy()
    materials = _minimal_materials()
    wide = assemble(
        materials,
        scope,
        None,
        execution_policy,
        profile(limits={"max_input_bytes": 10000}),
        "analysis-1",
    )
    assert isinstance(wide, PreparedContext)
    assert wide.effective_budget_bytes == 10000
    narrow_policy = execution_policy.model_copy(update={"max_message_bytes": 20000})
    narrow = assemble(materials, scope, None, narrow_policy, profile(), "analysis-1")
    assert isinstance(narrow, PreparedContext)
    assert narrow.effective_budget_bytes == 20000


def test_message_bytes_equals_transport_and_prepare_payload_accepts():
    context = _build(_minimal_materials())
    assert isinstance(context, PreparedContext)
    assert context.message_bytes == len(serialize_payload(profile(), context.messages))
    assert prepare_payload(profile(), context.messages)
    assert len(context.messages) == 2
    assert [message.role for message in context.messages] == ["system", "user"]


def test_optional_is_omitted_greedily_under_the_repair_reserve():
    state = _material("kubernetes.state", [_entry("field", ["uid", "pod-1"], {"uid": "pod-1"})])
    events = _material("kubernetes.events", [_entry("event", ["uid", "e-1"], {"uid": "e-1"})])
    configmap = _material(
        "kubernetes.configmap.metadata",
        [_entry("field", ["uid", "cm-1"], {"uid": "cm-1", "blob": "y" * 7000})],
    )
    fixture = _material("development_fixture", [_entry("field", ["path", "k"], {"k": "small"})])
    context = _build([state, events, configmap, fixture], budget=12000)
    assert isinstance(context, PreparedContext)
    assert context.message_bytes <= 12000 - 4096
    assert context.repair_available is True
    assert [target.source_key for target in context.optional_targets] == ["development_fixture"]
    assert any(
        omission.code == "budget_omitted" and omission.slot == "kubernetes.configmap.metadata"
        for omission in context.omissions
    )
    envelope = json.loads(context.messages[1].content)
    assert "synthetic_fixture_not_production_evidence" in envelope["limitations"]
    assert envelope["limitations"] == context.limitations


def test_required_filling_the_budget_sets_repair_unavailable():
    materials = _minimal_materials()
    measured = _build(materials)
    assert isinstance(measured, PreparedContext)
    context = _build(materials, budget=measured.message_bytes)
    assert isinstance(context, PreparedContext)
    assert context.optional_targets == []
    assert context.repair_available is False


def test_required_overflow_returns_insufficiency_without_messages():
    state_entries = [
        _entry(
            "field",
            ["uid", f"pod-{index:02d}"],
            {"uid": f"pod-{index:02d}", "blob": "z" * 200},
        )
        for index in range(40)
    ]
    state = _material("kubernetes.state", state_entries)
    events = _material("kubernetes.events", [_entry("event", ["uid", "e-1"], {"uid": "e-1"})])
    context = _build([state, events], budget=6000)
    assert isinstance(context, InsufficientContext)
    assert context.reason == "required_targets_exceed_budget"
    assert not hasattr(context, "messages")
    families = {item.family: item for item in context.families}
    assert families["pending_unschedulable"].eligible_for_assessment is True
    assert families["image_pull"].eligible_for_assessment is True


def test_request_exceeds_budget_is_terminal():
    context = _build(_minimal_materials(), budget=1000, request_text="Q" * 5000)
    assert isinstance(context, InsufficientContext)
    assert context.reason == "request_exceeds_budget"


def test_alias_capacity_is_999_required():
    # 250 + 50 + 3 + 3 + 3 * 250 = 1056 required targets; the 1000th alias fails
    # before any budget check, so a 999-target build cannot be reached here.
    materials = [
        _material(
            "kubernetes.state",
            [
                _entry("field", ["uid", f"s-{index:03d}"], {"uid": f"s-{index:03d}"})
                for index in range(250)
            ],
        ),
        _material(
            "kubernetes.events",
            [
                _entry("event", ["uid", f"e-{index:03d}"], {"uid": f"e-{index:03d}"})
                for index in range(50)
            ],
        ),
        _material(
            "kubernetes.logs.current",
            [
                _entry("log_segment", ["pod-1", "app", False, str(index), "d"], {"lines": ["x"]})
                for index in range(3)
            ],
        ),
        _material(
            "kubernetes.logs.previous",
            [
                _entry("log_segment", ["pod-1", "app", True, str(index), "d"], {"lines": ["y"]})
                for index in range(3)
            ],
        ),
    ]
    for slot in (
        "prometheus.request_rate",
        "prometheus.error_ratio",
        "prometheus.p95_latency",
    ):
        materials.append(
            _material(
                slot,
                [
                    _entry("series", ["metric", f"{slot}-{index:03d}"], {"m": index})
                    for index in range(250)
                ],
            )
        )
    context = _build(materials)
    assert isinstance(context, InsufficientContext)
    assert context.reason == "required_alias_space_exceeded"


def test_request_text_is_envelope_only_and_never_citable():
    scope = _scope("Distinctive operator request text")
    materials = _minimal_materials()
    context = assemble(materials, scope, None, _policy(), profile(), "analysis-1")
    assert isinstance(context, PreparedContext)
    user = json.loads(context.messages[1].content)
    assert user["request"] == {
        "text": "Distinctive operator request text",
        "source": "operator",
    }
    assert context.messages[1].content.count("Distinctive operator request text") == 1
    for target in [*context.required_targets, *context.optional_targets]:
        assert target.source_class != "operator_request"
        assert "Distinctive operator request text" not in canonical(target.value)


def test_insufficiency_has_digest_and_record_shape():
    context = _build(_minimal_materials(), budget=1000, request_text="SECRET-REQUEST-MARKER" * 200)
    assert isinstance(context, InsufficientContext)
    record = context.record()
    assert record["outcome"] == "insufficient"
    assert record["message_bytes"] is None
    assert record["digest"] == context.digest
    assert record["policy_digest"]
    assert record["versions"]
    assert record["scope"] is not None
    assert "SECRET-REQUEST-MARKER" not in json.dumps(record)
    prepared = _build(_minimal_materials())
    assert isinstance(prepared, PreparedContext)
    prepared_record = prepared.record()
    assert prepared_record["outcome"] == "prepared"
    assert isinstance(prepared_record["message_bytes"], int)
    assert prepared_record["digest"] == prepared.digest
    assert "Why did the workload restart?" not in json.dumps(prepared_record)


def test_rebuild_is_byte_identical_and_digest_stable():
    materials = _minimal_materials()
    scope = _scope()
    first = _build(materials, scope=scope)
    second = _build(materials, scope=scope)
    assert isinstance(first, PreparedContext) and isinstance(second, PreparedContext)
    assert [message.content for message in first.messages] == [
        message.content for message in second.messages
    ]
    assert first.message_bytes == second.message_bytes
    assert first.digest == second.digest
    third = _build(materials, scope=scope, policy_version="2")
    assert isinstance(third, PreparedContext)
    assert third.digest != first.digest
