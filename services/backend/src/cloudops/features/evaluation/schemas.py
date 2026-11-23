"""Strict, immutable schemas for the S4 evaluation contract."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cloudops.features.diagnosis.advice import CandidateTemplate, CauseCategory
from cloudops.features.evaluation.versions import (
    DIMENSIONS,
    IDENTITY_FIELDS,
    STAGES,
    VERSIONS,
)

Hash = str


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvalCasePlan(FrozenModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    family: CauseCategory
    # Treatment is the planned scenario class. Trial outcomes such as failed or
    # interrupted are recorded only as receipt dispositions, never planned here.
    treatment: Literal["positive", "manual", "abstention", "injection"]
    bundle: str
    methods: list[Literal["rules", "investigator"]] = Field(min_length=1)
    planned_stages: list[
        Literal["inject", "detect", "collect", "analyze", "approval", "apply", "verify"]
    ]

    @model_validator(mode="after")
    def safe_unique_plan(self):
        path = PurePosixPath(self.bundle)
        if (
            path.is_absolute()
            or "\\" in self.bundle
            or str(path) != self.bundle
            or path.parts != ("bundles", f"{self.case_id}.json")
        ):
            raise ValueError("bundle must be bundles/<case_id>.json")
        if len(set(self.methods)) != len(self.methods):
            raise ValueError("methods must be unique")
        if len(set(self.planned_stages)) != len(self.planned_stages):
            raise ValueError("planned_stages must be unique")
        return self


class EvalCohort(FrozenModel):
    schema_version: Literal["EvalCohort/v1"] = VERSIONS["cohort"]
    cohort_id: str = Field(min_length=1, max_length=100)
    cases: list[EvalCasePlan] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self):
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case_id values must be unique")
        return self


class EvalActionExpectation(FrozenModel):
    state: Literal["not_applicable", "expected", "forbidden"]
    expected_outcome: Literal["succeeded", "failed", "blocked", "not_executed"] | None

    @model_validator(mode="after")
    def expected_outcome_matches_state(self):
        if (self.state == "expected") != (self.expected_outcome is not None):
            raise ValueError("expected_outcome is required exactly for expected actions")
        if self.state != "expected" and self.expected_outcome is not None:
            raise ValueError("unexpected action outcome")
        return self


class EvalIncidentExpectation(FrozenModel):
    state: Literal["not_applicable", "expected"]
    expected_disposition: Literal["resolved", "unresolved", "unknown"] | None

    @model_validator(mode="after")
    def expected_disposition_matches_state(self):
        if (self.state == "expected") != (self.expected_disposition is not None):
            raise ValueError("expected_disposition is required exactly for expected incidents")
        return self


class EvalLabel(FrozenModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    category: CauseCategory
    required_result: Literal["supported", "abstention"]
    manual_only: bool
    allowed_candidates: list[CandidateTemplate]
    candidate_requirement: Literal["required", "optional", "forbidden"]
    required_sources: list[str]
    relevant_sources: list[str]
    expected_runbooks: list[str]
    clean_control: str | None
    action: EvalActionExpectation
    incident: EvalIncidentExpectation
    rationale: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def candidate_contract(self):
        if len(set(self.allowed_candidates)) != len(self.allowed_candidates):
            raise ValueError("allowed_candidates must be unique")
        if self.candidate_requirement == "required" and not self.allowed_candidates:
            raise ValueError("required candidates need an allowlist")
        if self.candidate_requirement == "forbidden" and self.allowed_candidates:
            raise ValueError("forbidden candidates cannot have an allowlist")
        if self.manual_only and self.candidate_requirement != "forbidden":
            raise ValueError("manual-only labels must forbid candidates")
        if len(set(self.required_sources)) != len(self.required_sources):
            raise ValueError("required_sources must be unique")
        if len(set(self.relevant_sources)) != len(self.relevant_sources):
            raise ValueError("relevant_sources must be unique")
        if not set(self.required_sources) <= set(self.relevant_sources):
            raise ValueError("required_sources must be relevant")
        return self


class EvalLabels(FrozenModel):
    schema_version: Literal["EvalLabels/v1"] = VERSIONS["label"]
    cohort_id: str
    cases: dict[str, EvalLabel] = Field(min_length=1)

    @model_validator(mode="after")
    def keys_match_labels(self):
        if any(key != label.case_id for key, label in self.cases.items()):
            raise ValueError("label keys must match case_id")
        return self


class EvalThresholds(FrozenModel):
    structural_valid_required: bool
    manual_only_candidate_max: int = Field(ge=0)
    unsafe_candidate_max: int = Field(ge=0)
    required_candidate_miss_max: int = Field(ge=0)
    label_leak_max: int = Field(ge=0)
    rules_positive_correct_min: float = Field(ge=0.0, le=1.0)
    rules_manual_correct_min: float = Field(ge=0.0, le=1.0)
    rules_abstention_correct_min: float = Field(ge=0.0, le=1.0)


class EvalInputSelection(FrozenModel):
    version: Literal["e10.input_selection/v1"] = VERSIONS["input_selection"]
    bundle: Literal["exact_scope_and_window_prepared_context"] = (
        "exact_scope_and_window_prepared_context"
    )
    labels_visible_to_methods: Literal[False] = False
    evidence_sources: Literal["e04_allowlisted_slots_only"] = "e04_allowlisted_slots_only"
    runbooks: Literal["e05_pinned_full_text_selection_only"] = "e05_pinned_full_text_selection_only"
    redaction: Literal["existing_egress_policy"] = "existing_egress_policy"


class EvalIncludeExclude(FrozenModel):
    version: Literal["e10.include_exclude/v1"] = "e10.include_exclude/v1"
    post_measurement_exclusions: Literal[False] = False
    retry_replaces_measurement: Literal[False] = False
    failed_cases_remain: Literal[True] = True
    calibration_pooled: Literal[False] = False
    exclusion_requires: Literal["pre_measurement_operator_error_or_label_leak"] = (
        "pre_measurement_operator_error_or_label_leak"
    )


class EvalRubric(FrozenModel):
    schema_version: Literal["EvalRubric/v1"] = VERSIONS["rubric"]
    rubric_id: str
    cohort_id: str
    label_schema_version: Literal["EvalLabels/v1"] = VERSIONS["label"]
    cohort_version: Literal["EvalCohort/v1"] = VERSIONS["cohort"]
    rules_version: Literal["e10.rules/v1"] = VERSIONS["rules"]
    scoring_version: Literal["e10.scoring/v1"] = VERSIONS["scoring"]
    thresholds: EvalThresholds
    input_selection: EvalInputSelection
    include_exclude: EvalIncludeExclude
    limitations: list[str] = Field(min_length=1)


class EvalStage(FrozenModel):
    stage: Literal["inject", "detect", "collect", "analyze", "approval", "apply", "verify"]
    state: Literal["observed", "missing", "not_applicable"]
    duration_ms: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def observed_duration(self):
        if (self.state == "observed") != (self.duration_ms is not None):
            raise ValueError("duration_ms is present exactly when state is observed")
        return self


class EvalAction(FrozenModel):
    state: Literal["not_applicable", "executed", "blocked", "not_executed"]
    outcome: Literal["succeeded", "failed", "blocked", "not_executed"] | None

    @model_validator(mode="after")
    def outcome_matches_state(self):
        if (self.state == "executed") != (self.outcome is not None):
            raise ValueError("outcome is required exactly when action state is executed")
        return self


class EvalIncident(FrozenModel):
    disposition: Literal["not_applicable", "resolved", "unresolved", "unknown"]


class EvalDimensionScore(FrozenModel):
    dimension: Literal[
        "cause_category",
        "evidence_references",
        "abstention_manual",
        "eligible_plan",
        "action_outcome",
        "incident_resolution",
    ]
    outcome: Literal["pass", "fail", "not_applicable", "missing", "blocked"]
    reasons: list[str] = Field(default_factory=list, max_length=20)


class EvalReceipt(FrozenModel):
    schema_version: Literal["EvalReceipt/v1"] = VERSIONS["receipt"]
    case_id: str
    method: Literal["rules", "investigator"]
    disposition: Literal["scored", "failed", "missing", "blocked", "interrupted"]
    bundle_sha256: str | None
    context_digest: str | None
    label_sha256: str
    rubric_sha256: str
    output_sha256: str | None
    output: dict | None
    dimensions: list[EvalDimensionScore]
    action: EvalAction
    incident: EvalIncident
    timing: list[EvalStage]
    reasons: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def complete_identity_and_measurements(self):
        for name in (
            "bundle_sha256",
            "context_digest",
            "label_sha256",
            "rubric_sha256",
            "output_sha256",
        ):
            digest = getattr(self, name)
            if digest is not None and (
                len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        if self.disposition == "scored" and (
            self.bundle_sha256 is None or self.context_digest is None or self.output is None
        ):
            raise ValueError("scored receipts require a bundle, context and output")
        if self.disposition != "missing" and self.bundle_sha256 is None:
            raise ValueError("non-missing receipts require a bundle")
        invalid_bundle = self.disposition == "blocked" and self.reasons == ["bundle_invalid"]
        if self.disposition != "missing" and (self.context_digest is None) != invalid_bundle:
            raise ValueError("context_digest is absent exactly for an invalid blocked bundle")
        if (self.output is None) != (self.output_sha256 is None):
            raise ValueError("output and output_sha256 must be present together")
        if (
            len(self.dimensions) != len(DIMENSIONS)
            or tuple(item.dimension for item in self.dimensions) != DIMENSIONS
        ):
            raise ValueError("dimensions must follow the pinned order exactly")
        if len(self.timing) != len(STAGES) or tuple(item.stage for item in self.timing) != STAGES:
            raise ValueError("timing must contain one marker for every stage in pinned order")
        return self


class EvalCaseLedger(FrozenModel):
    case_id: str
    methods: list[Literal["rules", "investigator"]]
    compare_bundle_sha256: str | None = None
    compare_context_digest: str | None = None
    compare_output_sha256: str | None = None
    compare_duration_ms: int | None = Field(default=None, ge=0)
    receipt_sha256: dict[str, str | None]

    @model_validator(mode="after")
    def receipt_methods_match(self):
        if set(self.receipt_sha256) != set(self.methods):
            raise ValueError("receipt_sha256 keys must match planned methods")
        return self


class EvalLedger(FrozenModel):
    schema_version: Literal["EvalLedger/v1"] = VERSIONS["ledger"]
    cohort_id: str
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    started: bool
    cases: list[EvalCaseLedger]

    @model_validator(mode="after")
    def unique_cases(self):
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("ledger case IDs must be unique")
        return self


class EvalStageSummary(FrozenModel):
    stage: Literal["inject", "detect", "collect", "analyze", "approval", "apply", "verify"]
    observed: int = Field(ge=0)
    missing: int = Field(ge=0)
    not_applicable: int = Field(ge=0)
    median_ms: int | None = Field(default=None, ge=0)
    min_ms: int | None = Field(default=None, ge=0)
    max_ms: int | None = Field(default=None, ge=0)


class EvalFamilySummary(FrozenModel):
    family: CauseCategory
    planned: int = Field(ge=0)
    scored: int = Field(ge=0)
    failed: int = Field(ge=0)
    missing: int = Field(ge=0)
    blocked: int = Field(ge=0)
    interrupted: int = Field(ge=0)
    per_dimension_passes: dict[str, int]


class EvalSummary(FrozenModel):
    schema_version: Literal["EvalSummary/v1"] = VERSIONS["summary"]
    cohort_id: str
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    seal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    method: Literal["rules", "investigator"]
    planned: int = Field(ge=0)
    attempted: int = Field(ge=0)
    dispositions: dict[str, int]
    families: list[EvalFamilySummary]
    stages: list[EvalStageSummary]
    thresholds: dict[str, bool | int | float]
    thresholds_met: bool
    threshold_reasons: list[str]
    limitations: list[str]


class EvalSeal(FrozenModel):
    schema_version: Literal["EvalSeal/v1"] = VERSIONS["seal"]
    cohort_id: str
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    frozen_at: datetime
    files: dict[str, str]
    versions: dict[str, str]
    label_schema_version: Literal["EvalLabels/v1"] = VERSIONS["label"]
    cohort_schema_version: Literal["EvalCohort/v1"] = VERSIONS["cohort"]
    rubric_schema_version: Literal["EvalRubric/v1"] = VERSIONS["rubric"]
    rules_version: Literal["e10.rules/v1"] = VERSIONS["rules"]
    scoring_version: Literal["e10.scoring/v1"] = VERSIONS["scoring"]
    identity_fields: tuple[str, ...]

    @model_validator(mode="after")
    def identity_and_files(self):
        if self.identity_fields != IDENTITY_FIELDS:
            raise ValueError("identity_fields must match the pinned tuple")
        for name, digest in self.files.items():
            if not name or name.startswith("/") or ".." in PurePosixPath(name).parts:
                raise ValueError("file names must be relative and contained")
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("file digests must be lowercase SHA-256")
        return self
