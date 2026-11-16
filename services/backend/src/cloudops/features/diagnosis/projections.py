"""Stored operational findings for handoff consumers; no inference or telemetry reads."""

from sqlalchemy import select

from cloudops.features.diagnosis.advice import RCAResult
from cloudops.features.diagnosis.models import Analysis, AnalysisResult


def handoff_sections(db, run_ids):
    rows = db.execute(
        select(Analysis, AnalysisResult)
        .outerjoin(AnalysisResult, AnalysisResult.analysis_id == Analysis.id)
        .where(Analysis.run_id.in_(run_ids), Analysis.purpose == "operational")
        .order_by(Analysis.created_at, Analysis.id)
    ).all()
    sections = {
        name: {"status": "complete", "count": 0, "items": [], "detail": "No stored items."}
        for name in (
            "findings",
            "citations",
            "runbook_citations",
            "ruled_out_checks",
            "unverified_checks",
            "open_questions",
        )
    }
    for analysis, stored in rows:
        if stored is None:
            sections["unverified_checks"]["items"].append(
                {
                    "run_id": str(analysis.run_id),
                    "status": analysis.state,
                    "summary": analysis.reason or "Analysis has no validated result.",
                    "evidence_ids": [],
                }
            )
            continue
        result = RCAResult.model_validate(stored.payload)
        run_id = str(analysis.run_id)
        sections["findings"]["items"].append(
            {
                "run_id": run_id,
                "analysis_id": str(analysis.id),
                "status": result.status,
                "summary": result.summary,
                "cause_category": result.cause_category,
                "identity": analysis.identity,
                "result": result.model_dump(mode="json"),
                "evidence_ids": [str(item.citation.evidence_id) for item in result.supporting],
            }
        )
        references = [*result.supporting, *result.contradicting]
        for item in result.alternatives:
            references.extend(item.evidence)
        for item in result.ruled_out:
            references.extend(item.evidence)
            sections["ruled_out_checks"]["items"].append(
                {
                    "run_id": run_id,
                    "status": "ruled_out",
                    "summary": item.reason,
                    "family": item.family,
                    "evidence_ids": [str(ref.citation.evidence_id) for ref in item.evidence],
                }
            )
        for item in references:
            reference = item.citation.model_dump(mode="json")
            if reference not in sections["citations"]["items"]:
                sections["citations"]["items"].append(reference)
        for item in result.runbook_citations:
            sections["runbook_citations"]["items"].append(
                {
                    "run_id": run_id,
                    **item.model_dump(mode="json"),
                }
            )
        for item in result.unverified_checks:
            sections["unverified_checks"]["items"].append(
                {
                    "run_id": run_id,
                    "status": "unverified",
                    "summary": item.reason,
                    "check": item.check,
                    "evidence_ids": [],
                }
            )
        for item in result.missing_evidence:
            sections["open_questions"]["items"].append(
                {
                    "run_id": run_id,
                    "status": "missing_evidence",
                    "summary": item,
                    "evidence_ids": [],
                }
            )
    for section in sections.values():
        section["count"] = len(section["items"])
    if not rows:
        for section in sections.values():
            section.update(status="unavailable", detail="No operational analysis recorded.")
    return sections
