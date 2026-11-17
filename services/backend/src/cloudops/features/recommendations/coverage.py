"""Coverage uses real source timestamps, independently for every expected lifetime."""

import math
from datetime import datetime, timezone

from cloudops.features.recommendations.schemas import Coverage

GRID_SECONDS = 60
RATE_SECONDS = 75


def assess(start: float, end: float, timestamps: list[float]) -> Coverage:
    if not math.isfinite(start) or not math.isfinite(end) or start >= end:
        raise ValueError("A finite nonempty lifetime is required.")
    expected = math.ceil((end - start) / GRID_SECONDS)
    points = sorted({t for t in timestamps if math.isfinite(t) and start <= t <= end})
    bins = {min(expected - 1, int((t - start) // GRID_SECONDS)) for t in points}
    gap = max(b - a for a, b in zip([start, *points], [*points, end]))
    reasons = []
    if len(bins) / expected < 0.95:
        reasons.append("coverage_below_threshold")
    if gap > 300:
        reasons.append("coverage_gap")
    if not points or end - points[-1] > 120:
        reasons.append("samples_stale")
    return Coverage(
        expected_bins=expected,
        covered_bins=len(bins),
        ratio=len(bins) / expected,
        longest_gap_seconds=gap,
        first_sample_at=datetime.fromtimestamp(points[0], timezone.utc) if points else None,
        last_sample_at=datetime.fromtimestamp(points[-1], timezone.utc) if points else None,
        reasons=reasons,
    )


def cpu_points(samples: list[tuple[float, float]], start: float, end: float):
    """Only count rate evaluation times supported by two real counter samples."""
    rows = sorted(samples)
    points, reset = [], False
    for index, (moment, value) in enumerate(rows):
        if index and value < rows[index - 1][1]:
            reset = True
        if moment < start or moment > end:
            continue
        support = [
            (t, v) for t, v in rows[max(0, index - 32) : index + 1] if moment - RATE_SECONDS <= t
        ]
        if len(support) >= 2 and support[0][0] <= moment - 60:
            points.append(moment)
    boundary = [t for t, _ in rows if start - RATE_SECONDS <= t <= start]
    supported = len(boundary) >= 2 and boundary[-1] - boundary[0] >= 60
    return points, supported, reset
