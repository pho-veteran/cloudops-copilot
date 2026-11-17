"""Versioned advice is data, independent from execution and current applicability."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cloudops.shared.schemas import View


class SafeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, revalidate_instances="always")


Name = Annotated[str, Field(min_length=1, max_length=100)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Reason = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ResultState = Literal["available", "insufficient_data", "excluded", "unavailable"]


class Quantity(SafeModel):
    kind: Literal["quantity"] = "quantity"
    value: str = Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$", max_length=40)
    unit: Literal["cores", "bytes"]


class Unset(SafeModel):
    kind: Literal["unset"] = "unset"


class Unavailable(SafeModel):
    kind: Literal["unavailable"] = "unavailable"
    reason: Reason


Value = Annotated[Quantity | Unset | Unavailable, Field(discriminator="kind")]


class ReportIdentity(SafeModel):
    workload_id: UUID
    cluster: Name
    cluster_fingerprint: Digest | None = None
    namespace: Name
    name: Name
    uid: Name
    observation_id: UUID | None = None
    observed_at: datetime | None = None
    template_digest: Digest | None = None
    pod_template_digest: Digest | None = None
    resources_digest: Digest | None = None
    containers: list[Name] = Field(default_factory=list, max_length=32)


class ReportProfile(SafeModel):
    tool_version: Literal["1.31.1"] = "1.31.1"
    upstream_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    source_tree_sha256: Digest | None = None
    embedded_version: str | None = Field(default=None, max_length=64)
    strategy: Literal["simple"] = "simple"
    history_hours: int = Field(ge=6, le=24)
    timeframe_minutes: Literal[1.25] = 1.25
    cpu_percentile: Literal[95] = 95
    memory_buffer_percent: Literal[15] = 15
    minimum_cpu_millicores: Literal[10] = 10
    minimum_memory_bytes: Literal[104857600] = 104857600
    points_required: Literal[100] = 100
    max_workers: Literal[2] | None = None
    allow_hpa: Literal[False] = False
    use_oomkill_data: Literal[False] = False
    policy_version: Literal["e08-coverage-v1"] = "e08-coverage-v1"
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")


class ReportWindow(SafeModel):
    kind: Literal["exact", "query_envelope"]
    start: datetime
    end: datetime
    raw_input_start: datetime
    raw_input_end: datetime

    @model_validator(mode="after")
    def ordered(self):
        values = (self.start, self.end, self.raw_input_start, self.raw_input_end)
        if any(x.tzinfo is None for x in values) or not (
            self.raw_input_start <= self.start < self.end <= self.raw_input_end
        ):
            raise ValueError("Use ordered UTC-aware analysis and raw-input bounds.")
        return self


class Coverage(SafeModel):
    expected_bins: int = Field(ge=1, le=100_000)
    covered_bins: int = Field(ge=0, le=100_000)
    ratio: float = Field(ge=0, le=1)
    longest_gap_seconds: float = Field(ge=0)
    first_sample_at: datetime | None = None
    last_sample_at: datetime | None = None
    reasons: list[Reason] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def consistent(self):
        if (
            self.covered_bins > self.expected_bins
            or abs(self.ratio - self.covered_bins / self.expected_bins) > 0.000001
        ):
            raise ValueError("Coverage must match the distinct expected/covered bins.")
        return self


class PodEvidence(SafeModel):
    pod: Name
    uid: Name
    replicaset_uid: Name
    container: Name
    start: datetime
    end: datetime
    lifetime: Literal["current", "completed", "unknown"]
    cpu: Coverage
    memory: Coverage
    restarts: Coverage
    cpu_raw_samples: int = Field(ge=0, le=100_000)
    memory_raw_samples: int = Field(ge=0, le=100_000)
    reasons: list[Reason] = Field(default_factory=list, max_length=16)
    memory_reasons: list[Reason] = Field(default_factory=list, max_length=16)


class ResourceEligibility(SafeModel):
    container: Name
    resource: Literal["cpu", "memory"]
    state: Literal["eligible", "insufficient_data", "excluded", "unavailable"]
    reasons: list[Reason] = Field(max_length=16)


class QualifiedEvidence(SafeModel):
    policy_version: Literal["e08-coverage-v1"] = "e08-coverage-v1"
    identity: ReportIdentity
    window: ReportWindow
    query_digest: Digest
    config_digest: Digest
    history_digest: Digest
    collected_at: datetime
    pods: list[PodEvidence] = Field(max_length=100)
    resources: list[ResourceEligibility] = Field(max_length=64)
    reasons: list[Reason] = Field(default_factory=list, max_length=16)
    limitations: list[str] = Field(
        default_factory=lambda: [
            "Five-second metric scraping and thirty-second ownership polling are sampled evidence; unobserved events remain possible.",
            "Eligibility alone supplies no KRR recommendation and authorizes no cluster write.",
        ],
        max_length=8,
    )


class ResourceAdvice(SafeModel):
    container: Name
    resource: Literal["cpu", "memory"]
    state: ResultState
    current_request: Value
    current_limit: Value
    observed: Quantity | None = None
    statistic: Literal["percentile_95", "maximum"] | None = None
    statistic_definition: str | None = Field(default=None, max_length=240)
    recommended_request: Value
    recommended_limit: Value
    buffer_percent: float | None = Field(default=None, ge=0, le=100)
    minimum: Quantity | None = None
    rounding: str | None = Field(default=None, max_length=100)
    coverage: Coverage | None = None
    reasons: list[Reason] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def meaningful(self):
        for value in (
            self.current_request,
            self.current_limit,
            self.observed,
            self.recommended_request,
            self.recommended_limit,
            self.minimum,
        ):
            if isinstance(value, Quantity):
                expected = "cores" if self.resource == "cpu" else "bytes"
                if value.unit != expected or (expected == "bytes" and "." in value.value):
                    raise ValueError("Resource quantities must use the matching base unit.")
        if self.state == "available":
            if not isinstance(self.recommended_request, Quantity) or not isinstance(
                self.recommended_limit, (Quantity, Unset)
            ):
                raise ValueError("Available advice needs explicit request/limit values.")
            if self.observed is None or self.coverage is None or self.statistic is None:
                raise ValueError("Available advice needs observed usage and coverage.")
        elif not (
            isinstance(self.recommended_request, Unavailable)
            and isinstance(self.recommended_limit, Unavailable)
            and self.reasons
        ):
            raise ValueError("Suppressed advice must have unavailable values and reasons.")
        return self


class ToolInvocation(SafeModel):
    request_sha256: Digest
    started_at: datetime | None = None
    finished_at: datetime | None = None
    timed_out: bool = False
    target_before_sha256: Digest | None = None
    target_after_sha256: Digest | None = None


class ReportResult(SafeModel):
    schema_version: Literal[2] = 2
    state: ResultState
    reason: Reason
    source: Literal["live_krr", "unconfigured"]
    identity: ReportIdentity
    profile: ReportProfile
    window: ReportWindow | None = None
    resources: list[ResourceAdvice] = Field(default_factory=list, max_length=64)
    tool_exit_code: int | None = None
    output_sha256: Digest | None = None
    query_digest: Digest | None = None
    source_evidence: QualifiedEvidence | None = None
    tool_invocation: ToolInvocation | None = None
    limitations: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def complete(self):
        if self.source_evidence and (
            self.source_evidence.identity != self.identity
            or self.source_evidence.window != self.window
            or self.source_evidence.query_digest != self.query_digest
        ):
            raise ValueError(
                "Source evidence must bind the same report identity, interval and queries."
            )
        keys = [(x.container, x.resource) for x in self.resources]
        if len(set(keys)) != len(keys):
            raise ValueError("A container/resource may occur only once.")
        available = any(x.state == "available" for x in self.resources)
        if (self.state == "available") != available:
            raise ValueError("Aggregate availability must reflect at least one usable resource.")
        if self.resources and not available:
            states = {x.state for x in self.resources}
            operational_failure = self.source == "unconfigured" or self.tool_exit_code not in (
                None,
                0,
            )
            expected = (
                "unavailable"
                if operational_failure or "unavailable" in states
                else "insufficient_data"
                if "insufficient_data" in states
                else "excluded"
            )
            if self.state != expected:
                raise ValueError(
                    "Aggregate nonavailable state must preserve failure/evidence precedence."
                )
        if available and (
            self.source != "live_krr"
            or self.window is None
            or self.profile.image_digest is None
            or self.output_sha256 is None
            or self.query_digest is None
            or self.tool_exit_code != 0
            or self.identity.cluster_fingerprint is None
            or self.identity.template_digest is None
            or self.identity.resources_digest is None
            or self.identity.observed_at is None
        ):
            raise ValueError("Available advice requires bound tool/source/window provenance.")
        if self.resources and set(keys) != {
            (c, r) for c in self.identity.containers for r in ("cpu", "memory")
        }:
            raise ValueError("Every scoped container must disclose both resources.")
        return self


class Applicability(SafeModel):
    state: Literal["current", "stale", "unknown"]
    assessed_at: datetime
    reasons: list[Reason] = Field(max_length=16)


class AttemptView(View):
    number: int
    state: str
    started_at: datetime
    finished_at: datetime | None
    error: str | None


class ReportView(View):
    id: UUID
    workload_id: UUID
    state: str
    requested_hours: int
    tool_version: str
    result: dict
    reason: str
    created_by: UUID
    created_at: datetime
    finished_at: datetime | None
    schema_version: int
    input_identity: ReportIdentity | None
    profile: ReportProfile | None
    result_v2: ReportResult | None
    migration_disposition: str | None
    job_phase: Literal["queued", "running", "finished", "failed"]
    result_state: ResultState | None
    attempts: list[AttemptView]
    dispatch_paused: bool
    dispatch_reason: str | None
    applicability: Applicability
    export_ready: bool
    export_sha256: str | None


class ReportPage(SafeModel):
    items: list[ReportView]
    next_cursor: str | None
    observation: "RecommendationObservation"


class RecommendationObservation(SafeModel):
    component: Literal["recommendations"] = "recommendations"
    status: Literal["available", "unavailable", "stale"]
    observed_at: datetime
    last_success_at: datetime | None
    reason: Reason
    source_kind: Literal["live_krr", "unconfigured"]
