import json
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from cloudops.features.recommendations.models import RecommendationReport
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
                result = json.load(response)
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
    def started(self, db, subject_id):
        db.get(RecommendationReport, subject_id).state = "running"

    def failed(self, db, subject_id, reason, terminal):
        row = db.get(RecommendationReport, subject_id)
        row.state = "unavailable" if terminal else "queued"
        row.reason = "Bounded report worker failed; inspect the configured tool connection."

    def publish(self, db, subject_id, result):
        row = db.get(RecommendationReport, subject_id)
        row.state, row.reason, row.result = result["state"], result["reason"], result["result"]
        row.finished_at = database_now(db)
