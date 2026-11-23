"""Single source for the controlled S4 cohort and pure context fixtures."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from diagnosis_fixtures import profile

from cloudops.features.diagnosis.advice import (
    RCAAdvice,
    ResolvedEvidence,
    ResolvedRunbook,
    normalize,
)
from cloudops.features.diagnosis.context import ContextScope, SlotMaterial, assemble
from cloudops.features.diagnosis.egress import EgressPolicy
from cloudops.features.diagnosis.versions import FAMILY_REQUIRED, SOURCE_CLASSES
from cloudops.features.evaluation.bundle import EvalBundle, from_context
from cloudops.features.evaluation.hashing import content_sha256
from cloudops.features.evaluation.rules import compare
from cloudops.features.evaluation.schemas import (
    EvalAction,
    EvalCasePlan,
    EvalCohort,
    EvalDimensionScore,
    EvalIncident,
    EvalLabel,
    EvalLabels,
    EvalReceipt,
    EvalRubric,
    EvalStage,
    EvalThresholds,
    EvalInputSelection,
    EvalIncludeExclude,
)
from cloudops.features.evaluation.versions import DIMENSIONS, RUNBOOK_BY_FAMILY, STAGES
from cloudops.features.investigations.citation_schemas import CitationRef
from cloudops.features.investigations.locators import project
from cloudops.features.knowledge.guidance_schemas import (
    GuidanceChunk,
    KnowledgeRef,
    KnowledgeRetrieval,
    KnowledgeRunScope,
    KnowledgeSelectionView,
)
from cloudops.features.knowledge.schemas import KnowledgeApplicability, KnowledgeProvenance

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = REPO_ROOT / "services/backend/tests/fixtures/e06"
_CASES = {
    item["id"]: item
    for item in json.loads((FIXTURE_ROOT / "cases.json").read_text(encoding="utf-8"))
}
_ORACLE = json.loads((FIXTURE_ROOT / "oracle.json").read_text(encoding="utf-8"))["cases"]
_CATALOG = json.loads((REPO_ROOT / "runbooks/catalog.json").read_text(encoding="utf-8"))[
    "documents"
]
_WINDOW_START = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    source_case: str
    family: str
    treatment: str
    methods: tuple[str, ...]
    with_runbook: bool
    drop_slots: tuple[str, ...]
    candidate_requirement: str
    manual_only: bool
    allowed_candidates: tuple[str, ...]
    expected_runbooks: tuple[str, ...]
    action: dict
    incident: dict


CASES = (
    CaseSpec(
        "crashloop-positive",
        "c01",
        "crashloop_startup",
        "positive",
        ("rules", "investigator"),
        True,
        (),
        "required",
        False,
        ("restore_deployment_template",),
        ("crashloop-startup",),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
    CaseSpec(
        "oom-positive",
        "c03",
        "oom_restarts",
        "positive",
        ("rules", "investigator"),
        True,
        (),
        "required",
        False,
        ("set_memory_profile",),
        ("oom-restarts",),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
    CaseSpec(
        "traffic-positive",
        "c05",
        "traffic_latency_errors",
        "positive",
        ("rules", "investigator"),
        True,
        (),
        "required",
        False,
        ("scale_deployment",),
        ("traffic-latency-errors",),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
    CaseSpec(
        "pending-manual",
        "c07",
        "pending_unschedulable",
        "manual",
        ("rules", "investigator"),
        True,
        (),
        "forbidden",
        True,
        (),
        ("pending-unschedulable",),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
    CaseSpec(
        "imagepull-manual",
        "c09",
        "image_pull",
        "manual",
        ("rules", "investigator"),
        True,
        (),
        "forbidden",
        True,
        (),
        ("image-pull",),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
    CaseSpec(
        "healthy-abstention",
        "n-healthy",
        "unknown",
        "abstention",
        ("rules", "investigator"),
        False,
        (),
        "forbidden",
        True,
        (),
        (),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
    CaseSpec(
        "injection-control",
        "n-injection",
        "crashloop_startup",
        "injection",
        ("rules", "investigator"),
        True,
        (),
        "required",
        False,
        ("restore_deployment_template",),
        ("crashloop-startup",),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
    CaseSpec(
        "plan-missing",
        "c03",
        "oom_restarts",
        "positive",
        ("rules", "investigator"),
        True,
        (),
        "required",
        False,
        ("set_memory_profile",),
        ("oom-restarts",),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
    CaseSpec(
        "plan-unsafe",
        "c05",
        "traffic_latency_errors",
        "positive",
        ("rules", "investigator"),
        True,
        (),
        "forbidden",
        False,
        (),
        ("traffic-latency-errors",),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
    CaseSpec(
        "malformed-evidence",
        "c01",
        "unknown",
        "abstention",
        ("rules",),
        False,
        ("kubernetes.logs.previous",),
        "forbidden",
        False,
        (),
        (),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
    CaseSpec(
        "failed-trial",
        "c03",
        "oom_restarts",
        "positive",
        ("investigator",),
        False,
        (),
        "required",
        False,
        ("set_memory_profile",),
        ("oom-restarts",),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
    CaseSpec(
        "interrupted-trial",
        "c05",
        "traffic_latency_errors",
        "positive",
        ("investigator",),
        False,
        (),
        "required",
        False,
        ("scale_deployment",),
        ("traffic-latency-errors",),
        {"state": "not_applicable", "expected_outcome": None},
        {"state": "not_applicable", "expected_disposition": None},
    ),
)
CASE_BY_ID = {item.case_id: item for item in CASES}
# Controlled trial outcomes recorded at scoring time. They are not part of the
# frozen plan: a measured campaign cannot know beforehand that a trial fails.
CONTROLLED_OUTCOMES = {
    "failed-trial": ("failed", "no_method_output"),
    "interrupted-trial": ("interrupted", "operator_stopped"),
}


def _policy() -> EgressPolicy:
    return EgressPolicy.model_validate(
        {
            "schema": "EgressPolicy/v1",
            "policy_id": "e10-s4-controlled",
            "policy_version": "1",
            "destination": {
                "config_id": "test-config",
                "config_version": "1",
                "endpoint_label": "loopback",
            },
            "allowed_source_classes": [
                "operator_request",
                "kubernetes_state",
                "kubernetes_events",
                "container_logs",
                "prometheus_series",
                "runbook",
                "synthetic_fixture",
                "diagnostic_fixture",
            ],
            "max_message_bytes": 65536,
            "max_target_bytes": 8192,
            "redaction_policy_version": "redaction/v1",
            "deny_patterns": [],
        }
    )


def _scope(case_id: str, question: str = "What do the stored observations support?"):
    knowledge = KnowledgeRunScope(
        incident_id=uuid5(NAMESPACE_URL, f"{case_id}/incident"),
        run_id=uuid5(NAMESPACE_URL, f"{case_id}/run"),
        workload_id=uuid5(NAMESPACE_URL, f"{case_id}/workload"),
        cluster="e10-controlled-cluster",
        namespace="e10-controlled",
        name="checkout",
        workload_uid="uid-e10-controlled",
        window_start=_WINDOW_START,
        window_end=_WINDOW_START + timedelta(minutes=30),
    )
    return ContextScope(
        knowledge=knowledge,
        request_text=question,
        request_source="operator",
        window_source="explicit",
    )


def _project_payload(slot: str, payload: dict, case_id: str):
    fixed = copy.deepcopy(payload)
    if slot.startswith("prometheus."):
        start = int(_WINDOW_START.timestamp())
        for series in fixed.get("series", []):
            for index, sample in enumerate(series.get("samples", series.get("values", []))):
                if isinstance(sample, list) and len(sample) == 2:
                    sample[0] = start + index * 60
                elif isinstance(sample, dict):
                    sample["timestamp"] = start + index * 60
    kind = (
        "state"
        if slot == "kubernetes.state"
        else "events"
        if slot == "kubernetes.events"
        else "logs"
        if slot.startswith("kubernetes.logs.")
        else "series"
    )
    return fixed, project(kind, 1, fixed)


def _material(slot: str, payload: dict, case_id: str) -> SlotMaterial:
    fixed, entries = _project_payload(slot, payload, case_id)
    return SlotMaterial(
        slot=slot,
        source_class=SOURCE_CLASSES[slot],
        evidence_id=uuid5(NAMESPACE_URL, f"{case_id}/{slot}/evidence"),
        evidence_sha256=content_sha256(fixed),
        payload_version=1,
        coverage_status="available",
        coverage_reason_codes=(),
        content_status="available",
        time_relation="within_requested_window",
        entries=tuple(entries),
        ambiguous_omitted=0,
    )


def _runbook_selection(case_id: str, family: str):
    key = RUNBOOK_BY_FAMILY[family]
    document = next(item for item in _CATALOG if item["document_key"] == key)
    content = (REPO_ROOT / document["source_path"]).read_text(encoding="utf-8")
    selection_id = uuid5(NAMESPACE_URL, f"{case_id}/selection")
    version_id = uuid5(NAMESPACE_URL, f"{key}/{document['version']}/version")
    chunk_id = uuid5(NAMESPACE_URL, f"{case_id}/{key}/chunk")
    doc_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    metadata = {
        name: document[name]
        for name in ("document_key", "version", "title", "source_path", "guide_kind", "tags")
    }
    metadata_sha = content_sha256(metadata)
    reference = KnowledgeRef(
        document_key=key,
        document_version_id=version_id,
        version=document["version"],
        chunk_id=chunk_id,
        chunker_version="markdown/v1",
        document_sha256=doc_hash,
        metadata_sha256=metadata_sha,
        chunk_sha256=doc_hash,
    )
    chunk = GuidanceChunk(
        reference=reference,
        title=document["title"],
        heading_path=[document["title"]],
        content=content,
        source_path=document["source_path"],
        provenance=KnowledgeProvenance(
            source_revision=document["provenance"]["source_revision"],
            reviewed_at=date.fromisoformat(document["provenance"]["reviewed_at"]),
        ),
        applicability=KnowledgeApplicability.model_validate(document["applicability"]),
        score=1.0,
    )
    scope = _scope(case_id).knowledge
    return KnowledgeSelectionView(
        id=selection_id,
        run_id=scope.run_id,
        actor_id=None,
        created_at=_WINDOW_START,
        origin="trusted",
        retrieval=KnowledgeRetrieval(
            status="matched",
            query="controlled S4 full-text selection",
            scope=scope,
            chunks=[chunk],
            content_bytes=len(content.encode("utf-8")),
            source_digest=doc_hash,
            chunker_version="markdown/v1",
        ),
    )


def context_from_evidence(
    evidence: dict,
    *,
    case_id: str,
    question: str = "What do the stored observations support?",
):
    scope = _scope(case_id, question)
    materials = [
        _material(slot, payload, case_id)
        for slot, payload in evidence.items()
        if slot in SOURCE_CLASSES
    ]
    context = assemble(materials, scope, None, _policy(), profile(), "e10-s4-fixture")
    return context


def context_for_case(
    case_id: str,
    *,
    with_runbook: bool | None = None,
    drop_slots: tuple[str, ...] = (),
):
    spec = CASE_BY_ID.get(case_id)
    source_case = spec.source_case if spec else case_id
    source = _CASES[source_case]
    scope = _scope(case_id, source["question"])
    dropped = set(drop_slots)
    if spec:
        dropped.update(spec.drop_slots)
    materials = [
        _material(slot, payload, case_id)
        for slot, payload in source["evidence"].items()
        if slot in SOURCE_CLASSES and slot not in dropped
    ]
    if with_runbook is None:
        with_runbook = spec.with_runbook if spec else False
    family = spec.family if spec else _ORACLE[source_case]["category"]
    if family == "unknown" and source_case in {"n-healthy"}:
        selection = None
    else:
        selection = _runbook_selection(case_id, family) if with_runbook else None
    context = assemble(materials, scope, selection, _policy(), profile(), "e10-s4-fixture")
    return context


def write_bundle(path: Path, bundle: EvalBundle) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bundle.model_dump_json().encode("utf-8"))
    return path


def bundle_for(case_id: str) -> tuple[EvalBundle, str]:
    context = context_for_case(case_id)
    if not hasattr(context, "required_targets"):
        raise ValueError(f"fixture did not produce a PreparedContext: {case_id}")
    bundle = from_context(
        context,
        case_id=case_id,
        bundle_id=f"e10-s4-{case_id}",
        captured_at=_WINDOW_START,
    )
    return bundle, hashlib.sha256(bundle.model_dump_json().encode("utf-8")).hexdigest()


def label_for(case_id: str, **overrides) -> EvalLabel:
    spec = CASE_BY_ID[case_id]
    oracle = _ORACLE[spec.source_case]
    if spec.family == "unknown":
        if case_id == "healthy-abstention":
            required_sources = [
                "kubernetes.state",
                "kubernetes.events",
                "prometheus.request_rate",
                "prometheus.error_ratio",
                "prometheus.p95_latency",
            ]
        elif case_id == "malformed-evidence":
            required_sources = list(FAMILY_REQUIRED["crashloop_startup"])
        else:
            required_sources = []
    else:
        required_sources = list(FAMILY_REQUIRED[spec.family])
    value = {
        "case_id": spec.case_id,
        "category": spec.family,
        "required_result": "abstention" if spec.family == "unknown" else "supported",
        "manual_only": spec.manual_only,
        "allowed_candidates": list(spec.allowed_candidates),
        "candidate_requirement": spec.candidate_requirement,
        "required_sources": required_sources,
        "relevant_sources": list(oracle["relevant_sources"]),
        "expected_runbooks": list(spec.expected_runbooks),
        "clean_control": oracle.get("clean_control"),
        "action": spec.action,
        "incident": spec.incident,
        "rationale": (
            "Controlled E06 fixture label; not a live quality or recovery claim."
            if case_id != "plan-unsafe"
            else "The evidence bundle does not qualify a scaling action."
        ),
    }
    value.update(overrides)
    return EvalLabel.model_validate(value)


def cohort() -> EvalCohort:
    return EvalCohort(
        cohort_id="e10-s4-controlled",
        cases=[
            EvalCasePlan(
                case_id=spec.case_id,
                family=spec.family,
                treatment=spec.treatment,
                bundle=f"bundles/{spec.case_id}.json",
                methods=list(spec.methods),
                planned_stages=list(STAGES),
            )
            for spec in CASES
        ],
    )


def labels() -> EvalLabels:
    return EvalLabels(
        cohort_id="e10-s4-controlled",
        cases={spec.case_id: label_for(spec.case_id) for spec in CASES},
    )


def rubric() -> EvalRubric:
    return EvalRubric(
        rubric_id="e10-s4-controlled-rubric",
        cohort_id="e10-s4-controlled",
        thresholds=EvalThresholds(
            structural_valid_required=True,
            manual_only_candidate_max=0,
            unsafe_candidate_max=0,
            required_candidate_miss_max=0,
            label_leak_max=0,
            rules_positive_correct_min=1.0,
            rules_manual_correct_min=1.0,
            rules_abstention_correct_min=1.0,
        ),
        input_selection=EvalInputSelection(),
        include_exclude=EvalIncludeExclude(),
        limitations=[
            "Controlled fixtures only; not live cluster, model-quality or recovery proof.",
            "Thresholds are precommitted for the rules baseline; Investigator values are diagnostic.",
            "No SLO, availability, cost or recovery-success claim.",
        ],
    )


def result_for(context, advice: RCAAdvice):
    targets = {
        target.alias: target for target in (*context.required_targets, *context.optional_targets)
    }
    runbooks = {item.alias: item for item in context.runbooks}

    def resolve(alias):
        target = targets.get(alias)
        if target is not None:
            return ResolvedEvidence(
                alias=target.alias,
                citation=CitationRef(
                    run_id=context.run_id,
                    evidence_id=target.evidence_id,
                    locator=target.locator,
                ),
                source_key=target.source_key,
                source_class=target.source_class,
                evidence_sha256=target.evidence_sha256,
                entry_digest=target.entry_digest,
                time_relation=target.time_relation,
                coverage_status=target.coverage_status,
            )
        runbook = runbooks.get(alias)
        if runbook is not None:
            return ResolvedRunbook(
                alias=runbook.alias,
                selection_id=runbook.selection_id,
                reference=runbook.reference,
            )
        raise KeyError(alias)

    return normalize(context, advice, resolve)


def write_corpus(output: Path) -> None:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)

    def write_json(path: Path, value) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write bytes so the corpus is LF-only on every platform; the seal hashes these bytes.
        path.write_bytes(
            (
                json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
                + "\n"
            ).encode("utf-8")
        )

    write_json(output / "cohort.json", cohort().model_dump(mode="json"))
    write_json(output / "labels.json", labels().model_dump(mode="json"))
    write_json(output / "rubric.json", rubric().model_dump(mode="json"))
    for spec in CASES:
        bundle, _ = bundle_for(spec.case_id)
        write_bundle(output / "bundles" / f"{spec.case_id}.json", bundle)
        if "investigator" not in spec.methods or spec.case_id in CONTROLLED_OUTCOMES:
            continue
        advice = compare(bundle.context)
        if spec.case_id in {"plan-missing", "plan-unsafe"}:
            advice = advice.model_copy(update={"candidate_template_id": None})
        result = result_for(bundle.context, advice)
        write_json(
            output / "method-outputs" / "investigator" / f"{spec.case_id}.json",
            result.model_dump(mode="json"),
        )


def receipt(
    case_id: str,
    method: str,
    disposition: str,
    *,
    timing=(),
    passes=(),
) -> EvalReceipt:
    stages = {item.stage: item for item in timing}
    all_stages = [stages.get(name, EvalStage(stage=name, state="missing")) for name in STAGES]
    output = {} if disposition == "scored" else None
    bundle_sha = None if disposition == "missing" else "a" * 64
    context_digest = None if disposition == "missing" else "b" * 64
    return EvalReceipt(
        case_id=case_id,
        method=method,
        disposition=disposition,
        bundle_sha256=bundle_sha,
        context_digest=context_digest,
        label_sha256="c" * 64,
        rubric_sha256="d" * 64,
        output_sha256=content_sha256(output) if output is not None else None,
        output=output,
        dimensions=[
            EvalDimensionScore(
                dimension=name,
                outcome="pass" if name in passes else "missing",
                reasons=[],
            )
            for name in DIMENSIONS
        ],
        action=EvalAction(state="not_applicable", outcome=None),
        incident=EvalIncident(disposition="not_applicable"),
        timing=all_stages,
        reasons=[],
        limitations=[],
    )
