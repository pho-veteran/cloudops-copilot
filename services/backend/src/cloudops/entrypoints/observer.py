"""Dedicated read-only workload observer; it never claims or delays shared Jobs."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from cloudops.entrypoints import metadata  # noqa: F401
from cloudops.features.workloads.kubernetes import Kubernetes
from cloudops.features.workloads.observations import (
    OBSERVATION_SCOPE_LIMIT,
    claim_due,
    latest_payload,
    publish_failure,
    publish_success,
)
from cloudops.shared.config import settings
from cloudops.shared.database import database_now, require_schema, sessions
from cloudops.shared.errors import DomainError

logger = logging.getLogger(__name__)


def observe_claim(claim, kube):
    """Read one workload outside a transaction and publish only with the live lease."""
    workload = SimpleNamespace(
        uid=claim.workload_uid,
        cluster=claim.cluster,
        namespace=claim.namespace,
        name=claim.name,
    )
    try:
        with sessions()() as db:
            previous = latest_payload(db, claim.workload_id)
        payload = kube.observe(workload, previous_payload=previous)
    except DomainError as error:
        return publish_attempt(claim, reason=error.code)
    except Exception:
        # Never log provider bodies or environment values; record a safe code.
        logger.warning(
            "Observation read failed for workload %s; sanitized reason recorded.",
            claim.workload_id,
        )
        return publish_attempt(claim, reason="observation_failed")
    with sessions()() as db:
        return publish_success(db, claim, payload)


def publish_attempt(claim, reason):
    with sessions()() as db:
        return publish_failure(db, claim, reason)


def run_once(kube=None) -> int:
    with sessions()() as db:
        now = database_now(db)
    with sessions()() as db:
        # Claim the whole configured scope; concurrency bounds parallel reads,
        # not how many due workloads one sweep accepts.
        claims = claim_due(db, now, OBSERVATION_SCOPE_LIMIT)
    if not claims:
        return 0
    reader = kube or Kubernetes()
    with ThreadPoolExecutor(max_workers=settings().observation_concurrency) as pool:
        list(pool.map(lambda claim: observe_claim(claim, reader), claims))
    return len(claims)


def main():
    require_schema()
    logging.basicConfig(level=logging.INFO)
    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception as exc:
            logger.error(
                "Observer cycle failed (%s). Check database/migrations and reader configuration; "
                "retrying in 5 seconds.",
                type(exc).__name__,
            )
            time.sleep(5)
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, settings().observation_poll_seconds - elapsed))


if __name__ == "__main__":
    main()
