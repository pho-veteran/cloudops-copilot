"""Evidence-bound `RCAAdvice/v1` parsing, structural validation and normalization.

Parsing, structural validation, normalization and feedback construction read the
prepared context only. ``validate`` additionally performs read-only exact-reference
resolution against already retained Evidence and a pinned Knowledge selection: it
never opens a socket, never mutates a row and never rewrites the advice. Semantic
relevance is out of scope; the result carries ``quality="not_established"`` on
purpose (issue #69 D2/D5).
"""

from __future__ import annotations

import json
from typing import Annotated, Callable, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cloudops.features.diagnosis.context import ContextTarget, PreparedContext
from cloudops.features.diagnosis.messages import Message, MessageError, serialize_payload
from cloudops.features.diagnosis.profile import ModelProfile
from cloudops.features.diagnosis.versions import (
    FAMILY_REQUIRED,
    MAX_FEEDBACK_BYTES,
    MAX_FEEDBACK_FAILURES,
    VALIDATOR_VERSION,
)
from cloudops.features.investigations.citation_schemas import CitationRef
from cloudops.features.investigations.locators import (
    UnsupportedProjection,
)
from cloudops.features.investigations.locators import digest as entry_digest
from cloudops.features.investigations.locators import project as project_payload
from cloudops.features.knowledge.guidance_schemas import KnowledgeRef
from cloudops.shared.errors import DomainError

AdviceStatus = Literal["supported", "insufficient_evidence"]
CauseCategory = Literal[
    "crashloop_startup",
    "oom_restarts",
    "traffic_latency_errors",
    "pending_unschedulable",
    "image_pull",
    "unknown",
]
CandidateTemplate = Literal[
    "restore_deployment_template",
    "set_memory_profile",
    "scale_deployment",
]
ValidationCode = Literal[
    "json_invalid",
    "schema_invalid",
    "unknown_alias",
    "duplicate_alias",
    "alias_wrong_class",
    "runbook_only_support",
    "supported_requires_family_cause",
    "supported_requires_family_evidence",
    "insufficient_requires_unknown_cause",
    "candidate_not_allowed",
    "candidate_mismatch",
    "ruled_out_family_unknown",
    "ruled_out_family_ineligible",
    "ruled_out_evidence_outside_family",
    "ruled_out_coverage_incomplete",
    "ruled_out_partial_coverage",
    "empty_next_steps",
    "reference_not_sent",
    "evidence_reference_mismatch",
    "evidence_hash_mismatch",
    "evidence_expired",
    "evidence_unavailable",
    "unsupported_payload_version",
    "runbook_alias_unknown",
    "runbook_reference_mismatch",
]

CANDIDATE_BY_FAMILY: dict[str, str] = {
    "crashloop_startup": "restore_deployment_template",
    "oom_restarts": "set_memory_profile",
    "traffic_latency_errors": "scale_deployment",
}

_FROZEN = ConfigDict(extra="forbid", frozen=True)
Alias = Annotated[str, Field(pattern=r"^[EK][0-9]{3}$")]
GeneralString = Annotated[str, Field(min_length=1, max_length=2000)]
_MAX_PATH = 120
_HEADER = "VALIDATION_ERRORS (structural only):"
_TRAILER = "Return one corrected RCAAdvice/v1 JSON object only."


class ValidationFailure(BaseModel):
    """A closed structural failure: code, bounded path, repairability; no payload text."""

    model_config = _FROZEN

    code: ValidationCode
    path: str = Field(min_length=1, max_length=_MAX_PATH)
    repairable: bool


class Alternative(BaseModel):
    model_config = _FROZEN

    hypothesis: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[Alias] = Field(max_length=20)


class RuledOutHypothesis(BaseModel):
    model_config = _FROZEN

    family: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[Alias] = Field(min_length=1, max_length=20)


class UnverifiedCheck(BaseModel):
    model_config = _FROZEN

    check: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class RCAAdvice(BaseModel):
    """Exactly D2: no scope, model, template parameters, endpoint or action fields."""

    model_config = _FROZEN

    schema_version: Literal["RCAAdvice/v1"]
    status: AdviceStatus
    cause_category: CauseCategory
    summary: str = Field(min_length=1, max_length=4000)
    supporting_evidence_ids: list[Alias] = Field(max_length=20)
    contradicting_evidence_ids: list[Alias] = Field(max_length=20)
    alternatives: list[Alternative] = Field(max_length=20)
    ruled_out_hypotheses: list[RuledOutHypothesis] = Field(max_length=20)
    unverified_checks: list[UnverifiedCheck] = Field(max_length=20)
    missing_evidence: list[GeneralString] = Field(max_length=20)
    limitations: list[GeneralString] = Field(max_length=20)
    next_steps: list[GeneralString] = Field(max_length=20)
    runbook_citation_ids: list[Alias] = Field(max_length=20)
    candidate_template_id: CandidateTemplate | None


class ResolvedEvidence(BaseModel):
    model_config = _FROZEN

    alias: Annotated[str, Field(pattern=r"^E[0-9]{3}$")]
    citation: CitationRef
    source_key: str
    source_class: str
    evidence_sha256: str
    entry_digest: str
    time_relation: str
    coverage_status: str


class ResolvedRunbook(BaseModel):
    model_config = _FROZEN

    alias: Annotated[str, Field(pattern=r"^K[0-9]{3}$")]
    selection_id: UUID
    reference: KnowledgeRef


class ResolvedAlternative(BaseModel):
    model_config = _FROZEN

    hypothesis: str
    reason: str
    evidence: list[ResolvedEvidence]


class ResolvedRuledOut(BaseModel):
    model_config = _FROZEN

    family: str
    reason: str
    evidence: list[ResolvedEvidence]


class RCAResult(BaseModel):
    """Normalized, evidence-bound result. It never claims semantic quality."""

    model_config = _FROZEN

    schema_version: Literal["RCAResult/v1"] = "RCAResult/v1"
    run_id: UUID
    incident_id: UUID
    context_digest: str
    validator_version: str
    prompt_version: str
    context_version: str
    policy_version: str
    status: AdviceStatus
    cause_category: CauseCategory
    summary: str
    supporting: list[ResolvedEvidence]
    contradicting: list[ResolvedEvidence]
    alternatives: list[ResolvedAlternative]
    ruled_out: list[ResolvedRuledOut]
    unverified_checks: list[UnverifiedCheck]
    missing_evidence: list[str]
    limitations: list[str]
    next_steps: list[str]
    runbook_citations: list[ResolvedRunbook]
    candidate_template_id: CandidateTemplate | None
    machine_validation: Literal["structural_v1"] = "structural_v1"
    quality: Literal["not_established"] = "not_established"


class ValidationOutcome(BaseModel):
    model_config = _FROZEN

    status: Literal["valid", "invalid"]
    result: RCAResult | None
    failures: list[ValidationFailure]
    repairable: bool


ResolvedRef = ResolvedEvidence | ResolvedRunbook
Resolver = Callable[[str], ResolvedRef | ValidationFailure]


class ResolutionRequired(RuntimeError):
    """A pure resolver could not produce an exact reference; Task 8 collects the failure."""

    def __init__(self, failure: ValidationFailure):
        super().__init__(failure.code)
        self.failure = failure


def _failure(code: ValidationCode, path: str, repairable: bool = True) -> ValidationFailure:
    return ValidationFailure(code=code, path=path[:_MAX_PATH] or "$", repairable=repairable)


def _location(loc: tuple[object, ...]) -> str:
    fields = (
        RCAAdvice.model_fields.keys()
        | Alternative.model_fields.keys()
        | RuledOutHypothesis.model_fields.keys()
        | UnverifiedCheck.model_fields.keys()
    )
    rendered = ""
    for part in loc:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            if part not in fields:
                break
            rendered = f"{rendered}.{part}" if rendered else str(part)
    return rendered[:_MAX_PATH] or "$"


def parse(raw_text: str) -> RCAAdvice | list[ValidationFailure]:
    """Parse strict JSON and the D2 model; failures carry only a bounded path."""
    try:
        value = json.loads(raw_text)
    except (TypeError, ValueError, RecursionError):
        return [_failure("json_invalid", "$")]
    if not isinstance(value, dict):
        return [_failure("json_invalid", "$")]
    try:
        return RCAAdvice.model_validate(value)
    except ValidationError as error:
        return [
            _failure("schema_invalid", _location(tuple(item["loc"])))
            for item in error.errors()[:MAX_FEEDBACK_FAILURES]
        ]


def _targets(context: PreparedContext) -> dict[str, ContextTarget]:
    return {
        target.alias: target for target in (*context.required_targets, *context.optional_targets)
    }


def _alias_codes(
    aliases: list[str],
    field: str,
    *,
    runbook: bool,
    targets: dict[str, ContextTarget],
    runbooks: set[str],
) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []
    seen: set[str] = set()
    for index, alias in enumerate(aliases):
        path = f"{field}[{index}]"
        if alias in seen:
            failures.append(_failure("duplicate_alias", path))
            continue
        seen.add(alias)
        wrong = alias.startswith("E") if runbook else alias.startswith("K")
        if wrong:
            failures.append(_failure("alias_wrong_class", path))
        elif runbook:
            if alias not in runbooks:
                failures.append(_failure("runbook_alias_unknown", path))
        elif alias not in targets:
            failures.append(_failure("unknown_alias", path))
    return failures


def validate_structural(context: PreparedContext, advice: RCAAdvice) -> list[ValidationFailure]:
    """Run the §10 order: membership, status, candidate, ruled-out, next steps."""
    targets = _targets(context)
    runbooks = {item.alias for item in context.runbooks}
    failures: list[ValidationFailure] = []

    failures.extend(
        _alias_codes(
            advice.supporting_evidence_ids,
            "supporting_evidence_ids",
            runbook=False,
            targets=targets,
            runbooks=runbooks,
        )
    )
    failures.extend(
        _alias_codes(
            advice.contradicting_evidence_ids,
            "contradicting_evidence_ids",
            runbook=False,
            targets=targets,
            runbooks=runbooks,
        )
    )
    for position, alternative in enumerate(advice.alternatives):
        failures.extend(
            _alias_codes(
                alternative.evidence_ids,
                f"alternatives[{position}].evidence_ids",
                runbook=False,
                targets=targets,
                runbooks=runbooks,
            )
        )
    for position, ruled_out in enumerate(advice.ruled_out_hypotheses):
        failures.extend(
            _alias_codes(
                ruled_out.evidence_ids,
                f"ruled_out_hypotheses[{position}].evidence_ids",
                runbook=False,
                targets=targets,
                runbooks=runbooks,
            )
        )
    failures.extend(
        _alias_codes(
            advice.runbook_citation_ids,
            "runbook_citation_ids",
            runbook=True,
            targets=targets,
            runbooks=runbooks,
        )
    )
    unknown_support = any(alias not in targets for alias in advice.supporting_evidence_ids)

    assessments = {item.family: item for item in context.families}
    if advice.status == "supported":
        assessment = assessments.get(advice.cause_category)
        if (
            advice.cause_category == "unknown"
            or assessment is None
            or not assessment.eligible_for_assessment
        ):
            failures.append(_failure("supported_requires_family_cause", "cause_category"))
        elif not advice.supporting_evidence_ids:
            code: ValidationCode = (
                "runbook_only_support"
                if advice.runbook_citation_ids
                else "supported_requires_family_evidence"
            )
            failures.append(_failure(code, "supporting_evidence_ids"))
        else:
            required = set(FAMILY_REQUIRED[advice.cause_category])
            matched = any(
                alias in targets and targets[alias].source_key in required
                for alias in advice.supporting_evidence_ids
            )
            if not matched and not unknown_support:
                failures.append(
                    _failure("supported_requires_family_evidence", "supporting_evidence_ids")
                )
    elif advice.cause_category != "unknown":
        failures.append(_failure("insufficient_requires_unknown_cause", "cause_category"))

    candidate = advice.candidate_template_id
    if candidate is not None:
        expected = CANDIDATE_BY_FAMILY.get(advice.cause_category)
        if advice.status != "supported" or expected is None:
            failures.append(_failure("candidate_not_allowed", "candidate_template_id"))
        elif candidate != expected:
            failures.append(_failure("candidate_mismatch", "candidate_template_id"))

    coverage = {item.slot: item for item in context.source_coverage}
    sent_coverage = {item.slot: item for item in context.sent_coverage.slots}
    for position, ruled_out in enumerate(advice.ruled_out_hypotheses):
        path = f"ruled_out_hypotheses[{position}]"
        family = ruled_out.family
        required = FAMILY_REQUIRED.get(family)
        if required is None:
            failures.append(_failure("ruled_out_family_unknown", path))
            continue
        assessment = assessments.get(family)
        if assessment is None or not assessment.eligible_for_assessment:
            failures.append(_failure("ruled_out_family_ineligible", path))
            continue
        waived = {item.slot for item in assessment.required_slots if item.waived}
        cited: set[str] = set()
        for index, alias in enumerate(ruled_out.evidence_ids):
            target = targets.get(alias)
            if target is None:
                continue
            if target.source_key not in required:
                failures.append(
                    _failure(
                        "ruled_out_evidence_outside_family",
                        f"{path}.evidence_ids[{index}]",
                    )
                )
                continue
            cited.add(target.source_key)
        incomplete = False
        partial = False
        for slot in required:
            if slot in waived:
                continue
            if slot not in cited:
                incomplete = True
                break
            source = coverage.get(slot)
            sent = sent_coverage.get(slot)
            if (
                source is None
                or source.status != "available"
                or source.content_status != "available"
                or sent is None
                or sent.rule_omitted > 0
                or sent.ambiguous_omitted > 0
                or sent.budget_omitted > 0
            ):
                partial = True
                break
        if incomplete:
            failures.append(_failure("ruled_out_coverage_incomplete", path))
        elif partial:
            failures.append(_failure("ruled_out_partial_coverage", path))

    if not advice.next_steps:
        failures.append(_failure("empty_next_steps", "next_steps"))

    return failures


def _evidence(resolver: Resolver, alias: str) -> ResolvedEvidence:
    resolved = resolver(alias)
    if isinstance(resolved, ResolvedEvidence):
        return resolved
    if isinstance(resolved, ValidationFailure):
        raise ResolutionRequired(resolved)
    raise ResolutionRequired(_failure("evidence_reference_mismatch", "evidence_ids"))


def _runbook(resolver: Resolver, alias: str) -> ResolvedRunbook:
    resolved = resolver(alias)
    if isinstance(resolved, ResolvedRunbook):
        return resolved
    if isinstance(resolved, ValidationFailure):
        raise ResolutionRequired(resolved)
    raise ResolutionRequired(_failure("runbook_reference_mismatch", "runbook_citation_ids"))


def normalize(context: PreparedContext, advice: RCAAdvice, resolver: Resolver) -> RCAResult:
    """Copy the advice order with exact resolved references and fixed version identity."""
    supporting = [_evidence(resolver, alias) for alias in advice.supporting_evidence_ids]
    contradicting = [_evidence(resolver, alias) for alias in advice.contradicting_evidence_ids]
    alternatives = [
        ResolvedAlternative(
            hypothesis=item.hypothesis,
            reason=item.reason,
            evidence=[_evidence(resolver, alias) for alias in item.evidence_ids],
        )
        for item in advice.alternatives
    ]
    ruled_out = [
        ResolvedRuledOut(
            family=item.family,
            reason=item.reason,
            evidence=[_evidence(resolver, alias) for alias in item.evidence_ids],
        )
        for item in advice.ruled_out_hypotheses
    ]
    limitations = list(advice.limitations)
    assessments = {item.family: item for item in context.families}
    for item in advice.ruled_out_hypotheses:
        assessment = assessments.get(item.family)
        if assessment is None:
            continue
        for slot in assessment.required_slots:
            if not slot.waived:
                continue
            limitation = f"ruled_out_slot_waived:{slot.slot}"
            if limitation not in limitations:
                limitations.append(limitation)
    return RCAResult(
        run_id=context.run_id,
        incident_id=context.incident_id,
        context_digest=context.digest,
        validator_version=VALIDATOR_VERSION,
        prompt_version=context.prompt_version,
        context_version=context.context_version,
        policy_version=context.policy_version,
        status=advice.status,
        cause_category=advice.cause_category,
        summary=advice.summary,
        supporting=supporting,
        contradicting=contradicting,
        alternatives=alternatives,
        ruled_out=ruled_out,
        unverified_checks=[
            UnverifiedCheck(check=item.check, reason=item.reason)
            for item in advice.unverified_checks
        ],
        missing_evidence=list(advice.missing_evidence),
        limitations=limitations,
        next_steps=list(advice.next_steps),
        runbook_citations=[_runbook(resolver, alias) for alias in advice.runbook_citation_ids],
        candidate_template_id=advice.candidate_template_id,
    )


def _feedback_block(lines: list[str]) -> str:
    return "\n".join([_HEADER, *lines, _TRAILER])


def feedback_message(
    profile: ModelProfile,
    context: PreparedContext,
    failures: list[ValidationFailure],
) -> str | None:
    """Build the bounded repair block or return None when it cannot be sent."""
    if not context.repair_available:
        return None
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for failure in failures:
        if not failure.repairable:
            continue
        key = (failure.path, failure.code)
        if key in seen:
            continue
        seen.add(key)
        if len(lines) >= MAX_FEEDBACK_FAILURES:
            break
        line = f"- {failure.path}: {failure.code}"
        if len(_feedback_block([*lines, line]).encode("utf-8")) > MAX_FEEDBACK_BYTES:
            break
        lines.append(line)
    if not lines:
        return None
    feedback = _feedback_block(lines)
    repaired = (
        context.messages[0],
        Message("user", f"{context.messages[1].content}\n{feedback}"),
    )
    try:
        encoded = serialize_payload(profile, list(repaired))
    except MessageError:
        return None
    if len(encoded) > context.effective_budget_bytes:
        return None
    return feedback


_EVIDENCE_DOMAIN_CODES: dict[str, ValidationCode] = {
    "evidence_expired": "evidence_expired",
    "evidence_unavailable": "evidence_unavailable",
    "locator_not_supported": "unsupported_payload_version",
}


def _domain_failure(error: DomainError, *, runbook: bool) -> ValidationFailure:
    """Map a scoped-read DomainError to a closed terminal code, never its message."""
    if runbook:
        return _failure("runbook_reference_mismatch", "$", repairable=False)
    code = _EVIDENCE_DOMAIN_CODES.get(error.code, "evidence_reference_mismatch")
    return _failure(code, "$", repairable=False)


def _exact_entry(content, target: ContextTarget):
    try:
        entries = project_payload(content.payload_kind, content.payload_version, content.payload)
    except UnsupportedProjection:
        return None
    return next(
        (
            entry
            for entry in entries
            if entry.kind == target.locator.kind and entry.key == target.locator.key
        ),
        None,
    )


def _resolve_evidence(db, context: PreparedContext, alias: str, target: ContextTarget):
    from cloudops.features.investigations import comparison_service
    from cloudops.features.investigations.service import evidence_content

    resolution = comparison_service.citation(
        db,
        context.incident_id,
        context.run_id,
        target.evidence_id,
        target.locator.kind,
        target.locator.key,
    )
    evidence = resolution.evidence
    if evidence.payload_version != 1:
        return _failure("unsupported_payload_version", "$", repairable=False)
    if evidence.source_key != target.source_key:
        return _failure("evidence_reference_mismatch", "$", repairable=False)
    if evidence.sha256 != target.evidence_sha256:
        return _failure("evidence_hash_mismatch", "$", repairable=False)
    content = evidence_content(db, context.incident_id, context.run_id, target.evidence_id)
    entry = _exact_entry(content, target)
    if entry is None or entry.ambiguous:
        return _failure("evidence_reference_mismatch", "$", repairable=False)
    if entry.identity != target.identity:
        return _failure("evidence_reference_mismatch", "$", repairable=False)
    if entry_digest(entry.value) != target.entry_digest:
        return _failure("evidence_reference_mismatch", "$", repairable=False)
    return ResolvedEvidence(
        alias=alias,
        citation=resolution.citation,
        source_key=target.source_key,
        source_class=target.source_class,
        evidence_sha256=target.evidence_sha256,
        entry_digest=target.entry_digest,
        time_relation=target.time_relation,
        coverage_status=target.coverage_status,
    )


def _resolve_runbook(db, context: PreparedContext, alias: str, runbook):
    from cloudops.features.knowledge import guidance

    resolved = guidance.resolve_selection_chunk(
        db,
        context.incident_id,
        context.run_id,
        runbook.selection_id,
        runbook.reference.chunk_id,
        runbook.reference.document_version_id,
        allow_evaluation=True,
    )
    if resolved.reference != runbook.reference or resolved.selection_id != runbook.selection_id:
        return _failure("runbook_reference_mismatch", "$", repairable=False)
    return ResolvedRunbook(
        alias=alias,
        selection_id=runbook.selection_id,
        reference=resolved.reference,
    )


def _database_resolver(db, context: PreparedContext) -> Resolver:
    """Memoized exact resolution over the sent manifest; failures stay path-free."""
    targets = _targets(context)
    runbooks = {item.alias: item for item in context.runbooks}
    cache: dict[str, ResolvedRef | ValidationFailure] = {}

    def resolve(alias: str) -> ResolvedRef | ValidationFailure:
        if alias in cache:
            return cache[alias]
        try:
            if alias.startswith("E"):
                target = targets.get(alias)
                result: ResolvedRef | ValidationFailure = (
                    _resolve_evidence(db, context, alias, target)
                    if target is not None
                    else _failure("unknown_alias", "$")
                )
            else:
                runbook = runbooks.get(alias)
                result = (
                    _resolve_runbook(db, context, alias, runbook)
                    if runbook is not None
                    else _failure("runbook_alias_unknown", "$")
                )
        except DomainError as error:
            result = _domain_failure(error, runbook=alias.startswith("K"))
        cache[alias] = result
        return result

    return resolve


def _occurrences(advice: RCAAdvice) -> list[tuple[str, str, bool]]:
    """Every cited alias with its advice path, in normalization order."""
    found: list[tuple[str, str, bool]] = []
    for index, alias in enumerate(advice.supporting_evidence_ids):
        found.append((alias, f"supporting_evidence_ids[{index}]", False))
    for index, alias in enumerate(advice.contradicting_evidence_ids):
        found.append((alias, f"contradicting_evidence_ids[{index}]", False))
    for position, item in enumerate(advice.alternatives):
        for index, alias in enumerate(item.evidence_ids):
            found.append((alias, f"alternatives[{position}].evidence_ids[{index}]", False))
    for position, item in enumerate(advice.ruled_out_hypotheses):
        for index, alias in enumerate(item.evidence_ids):
            found.append((alias, f"ruled_out_hypotheses[{position}].evidence_ids[{index}]", False))
    for index, alias in enumerate(advice.runbook_citation_ids):
        found.append((alias, f"runbook_citation_ids[{index}]", True))
    return found


def _invalid(failures: list[ValidationFailure]) -> ValidationOutcome:
    return ValidationOutcome(
        status="invalid",
        result=None,
        failures=failures,
        repairable=any(failure.repairable for failure in failures),
    )


def validate(db, context: PreparedContext, raw_text: str, refine=None) -> ValidationOutcome:
    """Parse, structurally check and resolve every citation in advice order.

    ``validate`` is read-only. Structural failures are repairable; any reference that
    no longer resolves to the exact sent identity is terminal (§10.2). ``refine`` (the
    engine profile's stage E) may only narrow a structurally valid answer; the result is
    checked again before resolution.
    """
    parsed = parse(raw_text)
    if isinstance(parsed, list):
        return _invalid(parsed)
    structural = validate_structural(context, parsed)
    if structural:
        return _invalid(structural)
    if refine is not None:
        parsed = refine(parsed, context)
        structural = validate_structural(context, parsed)
        if structural:
            return _invalid(structural)
    resolver = _database_resolver(db, context)
    for alias, path, _runbook in _occurrences(parsed):
        resolved = resolver(alias)
        if isinstance(resolved, ValidationFailure):
            terminal = resolved.model_copy(update={"path": path, "repairable": False})
            return _invalid([terminal])
    try:
        result = normalize(context, parsed, resolver)
    except ResolutionRequired as error:
        return _invalid([error.failure])
    return ValidationOutcome(status="valid", result=result, failures=[], repairable=False)
