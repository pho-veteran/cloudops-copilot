"""Pinned evaluation contract identifiers and finite vocabularies."""

VERSIONS: dict[str, str] = {
    "bundle": "EvalBundle/v1",
    "cohort": "EvalCohort/v1",
    "label": "EvalLabels/v1",
    "rubric": "EvalRubric/v1",
    "receipt": "EvalReceipt/v1",
    "ledger": "EvalLedger/v1",
    "seal": "EvalSeal/v1",
    "summary": "EvalSummary/v1",
    "comparison": "EvalComparison/v1",
    "rules": "e10.rules/v1",
    "scoring": "e10.scoring/v1",
    "input_selection": "e10.input_selection/v1",
}

METHODS = ("rules", "investigator")
DISPOSITIONS = ("scored", "failed", "missing", "blocked", "interrupted")
STAGES = ("inject", "detect", "collect", "analyze", "approval", "apply", "verify")
DIMENSIONS = (
    "cause_category",
    "evidence_references",
    "abstention_manual",
    "eligible_plan",
    "action_outcome",
    "incident_resolution",
)
MANUAL_FAMILIES = ("pending_unschedulable", "image_pull")
RUNBOOK_BY_FAMILY = {
    "crashloop_startup": "crashloop-startup",
    "oom_restarts": "oom-restarts",
    "traffic_latency_errors": "traffic-latency-errors",
    "pending_unschedulable": "pending-unschedulable",
    "image_pull": "image-pull",
}
IDENTITY_FIELDS = (
    "case_id",
    "method",
    "bundle_sha256",
    "context_digest",
    "label_sha256",
    "rubric_sha256",
    "output_sha256",
    "disposition",
)
