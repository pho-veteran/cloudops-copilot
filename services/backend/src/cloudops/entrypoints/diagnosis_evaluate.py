"""Trusted, explicitly owned test-lab admission; never an HTTP capability."""

import argparse
import json
from uuid import UUID

from sqlalchemy import select

from cloudops.entrypoints import metadata  # noqa: F401
from cloudops.entrypoints.dispatch import GATE
from cloudops.features.diagnosis.lifecycle import admit, configure_dispatch
from cloudops.features.diagnosis.settings import load
from cloudops.features.investigations.models import InvestigationRun
from cloudops.shared.config import settings
from cloudops.shared.database import engine, require_schema, sessions


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Admit one owned-lab evaluation through the normal bounded worker."
    )
    parser.add_argument("--owned-test-lab", action="store_true", required=True)
    parser.add_argument("--run-id", type=UUID, required=True)
    args = parser.parse_args(argv)
    settings()
    database = engine().url.database or ""
    if not database.endswith("_test"):
        parser.error("Evaluation requires a dedicated owned database ending in _test.")
    if load().denied("evaluation"):
        parser.error("Install an exact evaluation_only model and egress profile first.")
    require_schema()
    configure_dispatch(GATE)
    with GATE.admitted(), sessions()() as db, db.begin():
        run = db.scalar(
            select(InvestigationRun).where(InvestigationRun.id == args.run_id).with_for_update()
        )
        if run is None or run.pack_coverage is None:
            parser.error("Select an existing collected Run in the owned test lab.")
        analysis = admit(db, run, purpose="evaluation")
        print(
            json.dumps(
                {
                    "analysis_id": str(analysis.id),
                    "purpose": "evaluation",
                    "operational_findings": False,
                }
            )
        )


if __name__ == "__main__":
    main()
