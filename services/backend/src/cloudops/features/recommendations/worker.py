import json
import os
import re
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from cloudops.features.recommendations.models import RecommendationReport
from cloudops.features.recommendations import service
from cloudops.features.recommendations.schemas import ReportResult
from cloudops.features.workloads.models import Workload
from cloudops.shared.database import database_now, sessions


def generate(report_id, *, before_call):
    with sessions()() as db:
        report = db.get(RecommendationReport, report_id)
        workload = db.get(Workload, report.workload_id)
        body = {
            "namespace": workload.namespace,
            "name": workload.name,
            "uid": workload.uid,
            "requested_hours": report.requested_hours,
        }
    server = os.environ.get("KRR_RUNNER_URL")
    if server:
        try:
            key = Path(os.environ["KRR_RUNNER_KEY"]).read_text().strip()
        except (OSError, KeyError):
            key = ""
        request = Request(
            server.rstrip("/") + "/report",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        opener = build_opener(ProxyHandler({}))
        try:
            before_call()
            with opener.open(request, timeout=150) as response:
                payload = response.read(262145)
                if len(payload) > 262144:
                    raise ValueError("KRR response exceeds its bound.")
                result = json.loads(payload)
                if result["state"] in {"insufficient_data", "unavailable", "available"}:
                    return result
        except (OSError, URLError, ValueError, KeyError):
            pass
    return {
        "state": "unavailable",
        "reason": "The configured KRR runner is unavailable; no recommendations were fabricated.",
        "result": {"source": "krr-v1.31.1", "coverage": None, "recommendations": None},
    }


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
