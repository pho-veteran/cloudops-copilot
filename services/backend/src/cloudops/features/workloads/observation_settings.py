"""Finite feature bounds; polling never uses the product job queue."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ObservationPolicy:
    poll_seconds: int = 30
    stale_seconds: int = 60
    deadline_seconds: int = 12
    lease_seconds: int = 15
    global_slots: int = 4
    scheduled_slots: int = 3
    page_size: int = 100
    max_replicasets: int = 200
    max_pods: int = 500
    max_owned_pods: int = 100
    max_requests: int = 9
    response_bytes: int = 2_000_000
    observation_bytes: int = 65_536
    history_count: int = 100
    history_days: int = 7


POLICY = ObservationPolicy()
