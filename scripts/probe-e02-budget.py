"""Measure 100-target polling in the fixed lab; create/delete only owned zero-replica fixtures."""

from datetime import datetime, timezone
import json
from pathlib import Path
import time

from e02_probe_support import arguments, lab, metadata, save


def main():
    args = arguments(__doc__, "budget.json")
    report = metadata(Path(__file__))
    policy = (lab.reader_folder / "workloads.json").read_bytes()
    names = [f"e02-budget-{i:03}" for i in range(96)]
    for name in names:
        lab.require(
            lab.read_fixture("Deployment", name) is None,
            "A budget fixture already exists; inspect or cleanup before rerunning.",
        )
    template = json.loads((lab.folder / "e02-ready.json").read_text())
    created = []
    try:
        for name in names:
            value = json.loads(json.dumps(template))
            value["metadata"]["name"] = name
            value["spec"]["replicas"] = 0
            value["spec"]["selector"]["matchLabels"]["app"] = name
            value["spec"]["template"]["metadata"]["labels"]["app"] = name
            lab.create_fixture(value)
            created.append(name)
        scope = json.loads(policy)
        scope["deployments"] += [{"namespace": "demo", "name": name} for name in names]
        lab.require(
            len(scope["deployments"]) == 100,
            "Budget measurement requires exactly 100 configured targets.",
        )
        (lab.reader_folder / "workloads.json").write_text(json.dumps(scope), encoding="utf-8")
        lab.sync()
        begin = datetime.now(timezone.utc)
        started = time.monotonic()
        count = 0
        while time.monotonic() - started < 180:
            value = json.loads(
                lab.app(
                    "import json; from sqlalchemy import select; from cloudops.shared.database import sessions,database_now; from cloudops.features.workloads.models import Workload; from cloudops.features.workloads.observation_models import ObservationState;\nwith sessions()() as db: print(json.dumps({'now':database_now(db).isoformat(),'rows':[{'name':w.name,'success':s.last_success_at.isoformat() if s.last_success_at else None,'outcome':s.latest_outcome,'due':s.next_due_at.isoformat()} for w,s in db.execute(select(Workload,ObservationState).join(ObservationState,ObservationState.workload_id==Workload.id).where(Workload.active,Workload.cluster=='e02-local-k3s'))]}))"
                )
            )
            count = sum(
                row["success"] is not None and datetime.fromisoformat(row["success"]) >= begin
                for row in value["rows"]
            )
            if count == 100:
                break
            time.sleep(2)
        lab.require(count == 100, f"Only {count}/100 received a new complete read within 180s.")
        now = datetime.fromisoformat(value["now"])
        ages = [
            (now - datetime.fromisoformat(row["success"])).total_seconds() for row in value["rows"]
        ]
        lags = [
            max(0, (now - datetime.fromisoformat(row["due"])).total_seconds())
            for row in value["rows"]
        ]
        windows = json.loads(
            lab.app(
                "import json; from sqlalchemy import select; from cloudops.shared.database import sessions; from cloudops.features.workloads.observation_models import WorkloadObservation; from cloudops.features.workloads.models import Workload;\nwith sessions()() as db: print(json.dumps([r.payload for r in db.scalars(select(WorkloadObservation).join(Workload,Workload.id==WorkloadObservation.workload_id).where(Workload.active,Workload.cluster=='e02-local-k3s'))]))"
            )
        )
        durations = [
            (
                datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
                - datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
            ).total_seconds()
            for row in windows
            if datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")) >= begin
        ]
        assert durations and max(durations) <= 12 and max(ages) <= 60
        report.update(
            scope=100,
            mix={"original_checkout": 1, "disposable_running": 3, "disposable_scaled_to_zero": 96},
            all_100_complete_within_seconds=round(time.monotonic() - started, 3),
            max_complete_read_seconds=max(durations),
            retained_complete_windows=len(durations),
            max_success_age_at_all_complete_seconds=max(ages),
            max_poll_lag_seconds=max(lags),
            deadline_seconds=12,
            freshness_seconds=60,
            limits="Local measurement, not a universal availability SLO or 100 healthy applications.",
        )
    finally:
        (lab.reader_folder / "workloads.json").write_bytes(policy)
        lab.sync()
        for name in created:
            lab.delete_fixture("Deployment", name)
    save(report, args.output)


if __name__ == "__main__":
    main()
