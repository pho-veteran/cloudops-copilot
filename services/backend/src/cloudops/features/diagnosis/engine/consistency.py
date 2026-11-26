"""Stage E: consistency between the detector and a structurally valid model answer.

The product validator has already accepted the answer; this stage can only make it more
conservative. When the model supports a family that is outside the detector's top two
and that family scored below the detector threshold, the answer is downgraded to an
abstention (policy ``downgrade``) or kept with a recorded flag (policy ``flag``).
"""

from __future__ import annotations

from cloudops.features.diagnosis.advice import RCAAdvice, validate_structural
from cloudops.features.diagnosis.context import PreparedContext
from cloudops.features.diagnosis.engine.detector import THRESHOLD, Detection

CONSISTENCY_VERSION = "engine.consistency/v1"
POLICIES = ("downgrade", "flag", "off")


def check(
    advice: RCAAdvice, detection: Detection, context: PreparedContext, *, policy: str
) -> tuple[RCAAdvice, dict]:
    if policy not in POLICIES:
        raise ValueError(f"unknown consistency policy {policy}")
    note = {"version": CONSISTENCY_VERSION, "policy": policy, "action": "none"}
    if policy == "off" or advice.status != "supported":
        return advice, note
    scores = {item.family: item.score for item in detection.hypotheses}
    top_two = [item.family for item in detection.hypotheses[:2]]
    family = advice.cause_category
    if family in top_two or scores.get(family, 0.0) >= THRESHOLD:
        return advice, note
    note.update(
        {
            "action": "downgrade" if policy == "downgrade" else "flag",
            "model_family": family,
            "detector_top": top_two,
            "model_family_score": scores.get(family, 0.0),
        }
    )
    if policy == "flag":
        return advice, note
    downgraded = advice.model_copy(
        update={
            "status": "insufficient_evidence",
            "cause_category": "unknown",
            "candidate_template_id": None,
            "ruled_out_hypotheses": [],
            "limitations": [
                *advice.limitations[:19],
                f"{CONSISTENCY_VERSION}: {family} is not supported by the deterministic signals",
            ],
        }
    )
    failures = validate_structural(context, downgraded)
    if failures:
        # Never return a structurally invalid answer; keep the validated one, flagged.
        note["action"] = "flag"
        note["downgrade_failures"] = sorted({item.code for item in failures})
        return advice, note
    return downgraded, note
