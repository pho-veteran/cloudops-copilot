import re

from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.recommendations.producer import generate as generate
from cloudops.features.recommendations import service
from cloudops.features.recommendations.schemas import ReportResult
from cloudops.shared.database import database_now


class ReportHandler:
    def eligible(self, db, subject_id):
        row = db.get(RecommendationReport, subject_id)
        return row is not None and row.schema_version == 2 and row.export_text is None

    def started(self, db, subject_id):
        db.get(RecommendationReport, subject_id).state = "running"

    def failed(self, db, subject_id, reason, terminal):
        row = db.get(RecommendationReport, subject_id)
        row.state = "unavailable" if terminal else "queued"
        row.reason = "Bounded report worker failed; inspect the configured tool connection."
        if terminal and row.schema_version == 2:
            service.publish(db, row, service.unavailable(row, "worker_failed"))

    def publish(self, db, subject_id, result):
        row = db.get(RecommendationReport, subject_id)
        if row.schema_version == 2:
            value = result if isinstance(result, ReportResult) else service.unavailable(row)
            if isinstance(result, dict):
                receipt = result.get("result", {})
                if (
                    isinstance(receipt, dict)
                    and result.get("state") in {"insufficient_data", "unavailable"}
                    and receipt.get("source") == "live_krr"
                    and type(receipt.get("tool_exit_code")) is int
                    and isinstance(receipt.get("output_sha256"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", receipt["output_sha256"])
                ):
                    value = value.model_copy(
                        update={
                            "source": "live_krr",
                            "state": "insufficient_data"
                            if receipt["tool_exit_code"] == 0
                            else "unavailable",
                            "reason": "representative_history_unqualified"
                            if receipt["tool_exit_code"] == 0
                            else "runner_failed",
                            "tool_exit_code": receipt["tool_exit_code"],
                            "output_sha256": receipt["output_sha256"],
                        }
                    )
            service.publish(db, row, value)
            return
        row.state, row.reason, row.result = result["state"], result["reason"], result["result"]
        row.finished_at = database_now(db)
