"""Feature-owned Alertmanager intake configuration; shared config carries no intake policy."""

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
MAX_BODY_BYTES = 1_048_576
MAX_ALERTS = 100
RECEIPT_RETENTION = 5000
FIELDS = {"schema_version", "source_id", "cluster", "token_sha256"}


@dataclass(frozen=True)
class IntakeConfig:
    source_id: str
    cluster: str
    token_sha256: str


@lru_cache
def intake_config() -> IntakeConfig | None:
    """Read ``ALERT_INTAKE_CONFIG`` once; unset disables intake, invalid fails startup."""
    path = os.environ.get("ALERT_INTAKE_CONFIG", "")
    if not path:
        return None
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 10_000:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise ValueError
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError
        source, cluster, digest = value["source_id"], value["cluster"], value["token_sha256"]
        if (
            not all(isinstance(item, str) for item in (source, cluster, digest))
            or not SOURCE_ID.fullmatch(source)
            or not 1 <= len(cluster) <= 100
            or not DIGEST.fullmatch(digest)
        ):
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        raise RuntimeError(
            "ALERT_INTAKE_CONFIG must name a JSON file with schema_version 1, a lowercase "
            "source_id, the pinned scope cluster key and the token_sha256 of the sender token."
        ) from None
    return IntakeConfig(source, cluster, digest)
