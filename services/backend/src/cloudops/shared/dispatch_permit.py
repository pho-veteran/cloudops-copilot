"""DispatchPermit/v1: trusted job-dispatch authority stored outside PostgreSQL.

The permit lives in ``JOB_DISPATCH_ROOT`` (``permit.json`` plus ``permit.lock``). Only a trusted
host actor writes it; workers and the API mount it read-only. Database rows record which
authority/generation admitted an attempt, but no database value ever grants permission.

These mechanics are feature-neutral: entrypoints inject the job-kind to permit-class mapping.
"""

import json
import os
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal, Mapping
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator

from cloudops.shared.file_lock import LockUnavailable, append_durably, file_lock, replace_durably

SCHEMA = "DispatchPermit/v1"
PERMIT = "permit.json"
LOCK = "permit.lock"
AUDIT = "audit.jsonl"
MAX_PERMIT_BYTES = 16384
DEFAULT_DAYS = 30
MAX_DAYS = 365
DenyReason = Literal[
    "permit_missing", "permit_invalid", "permit_stale", "recovery_mode", "class_denied"
]
Operation = Literal["bootstrap", "enter_recovery", "resume", "exit_recovery", "renew"]


class Permit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    schema_: Literal["DispatchPermit/v1"] = Field(alias="schema")
    authority_id: UUID
    generation: int = Field(ge=1)
    mode: Literal["normal", "recovery"]
    # Permit classes (for example ``collection``), mapped from concrete job kinds by entrypoints.
    allowed_job_kinds: tuple[str, ...] = Field(max_length=16)
    valid_until: AwareDatetime
    issued_at: AwareDatetime
    operation: Operation

    @field_validator("allowed_job_kinds")
    @classmethod
    def _classes(cls, value):
        if len(set(value)) != len(value) or any(
            not 1 <= len(item) <= 32 or not item.replace("_", "").isalpha() or not item.islower()
            for item in value
        ):
            raise ValueError("Permit classes must be unique lowercase identifiers.")
        return tuple(sorted(value))

    def document(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)


@dataclass(frozen=True)
class Admission:
    """One permit read under the shared host lock; valid only inside ``DispatchGate.admitted``."""

    permit: Permit | None
    kinds: frozenset[str]
    reason: DenyReason | None

    def permits(self, kind: str, authority_id: UUID | None, generation: int | None) -> bool:
        return (
            self.permit is not None
            and kind in self.kinds
            and authority_id == self.permit.authority_id
            and generation == self.permit.generation
        )


def dispatch_root() -> Path | None:
    value = os.environ.get("JOB_DISPATCH_ROOT", "").strip()
    return Path(value) if value else None


def read_permit(root: Path) -> tuple[Permit | None, DenyReason | None]:
    path = root / PERMIT
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_PERMIT_BYTES + 1)
    except FileNotFoundError:
        return None, "permit_missing"
    except OSError:
        return None, "permit_invalid"
    if len(raw) > MAX_PERMIT_BYTES:
        return None, "permit_invalid"
    try:
        return Permit.model_validate(json.loads(raw)), None
    except (ValueError, ValidationError):
        return None, "permit_invalid"


class DispatchGate:
    """Fail-closed reader. ``classes`` maps each concrete job kind to its permit class."""

    def __init__(self, classes: Mapping[str, str], root: Path | None = None, *, timeout=15.0):
        self.classes = dict(classes)
        self._root = root
        self.timeout = timeout
        self._highest: dict[UUID, int] = {}
        self._guard = threading.Lock()

    @property
    def root(self) -> Path | None:
        return self._root or dispatch_root()

    def _evaluate(self, permit, reason, now=None):
        """Return (kinds, reason); a regressed generation or past validity is stale."""
        if permit is None:
            return frozenset(), reason
        now = now or datetime.now(timezone.utc)
        with self._guard:
            highest = self._highest.get(permit.authority_id, 0)
            if permit.generation < highest:
                return frozenset(), "permit_stale"
            self._highest[permit.authority_id] = permit.generation
        if permit.valid_until <= now:
            return frozenset(), "permit_stale"
        kinds = frozenset(k for k, c in self.classes.items() if c in permit.allowed_job_kinds)
        if not kinds:
            return kinds, "recovery_mode" if permit.mode == "recovery" else "class_denied"
        return kinds, None

    @contextmanager
    def admitted(self) -> Iterator[Admission]:
        """Hold the shared host lock across one short claim/checkpoint/publication transaction."""
        root = self.root
        if root is None or not (root / LOCK).exists():
            yield Admission(None, frozenset(), "permit_missing")
            return
        stack = ExitStack()
        try:
            stack.enter_context(file_lock(root / LOCK, exclusive=False, timeout=self.timeout))
        except (LockUnavailable, OSError):
            stack.close()
            yield Admission(None, frozenset(), "permit_invalid")
            return
        with stack:
            permit, reason = read_permit(root)
            kinds, reason = self._evaluate(permit, reason)
            yield Admission(permit if kinds else None, kinds, reason)

    def current(self) -> tuple[Permit | None, frozenset[str], DenyReason | None]:
        """Lock-free read for external-call boundaries and status; never grants publication."""
        root = self.root
        if root is None:
            return None, frozenset(), "permit_missing"
        permit, reason = read_permit(root)
        kinds, reason = self._evaluate(permit, reason)
        return permit, kinds, reason

    def allows(self, kind: str, authority_id: UUID | None, generation: int | None) -> bool:
        permit, kinds, _ = self.current()
        return (
            permit is not None
            and kind in kinds
            and permit.authority_id == authority_id
            and permit.generation == generation
        )

    def status(self, kind: str) -> tuple[bool, DenyReason | None]:
        """Display-only dispatch status from a lock-free read; it never grants publication."""
        permit, kinds, reason = self.current()
        if kind in kinds:
            return True, None
        if reason is None:
            reason = "recovery_mode" if permit and permit.mode == "recovery" else "class_denied"
        return False, reason


# Trusted writer operations. Callers (the dispatch_admin entrypoint and tests) establish the
# OS actor; nothing here reads PostgreSQL.


class PermitError(RuntimeError):
    pass


def _audit_entry(permit: Permit, reason: str, evidence: list[str], actor: dict) -> bytes:
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "operation": permit.operation,
        "authority_id": str(permit.authority_id),
        "generation": permit.generation,
        "mode": permit.mode,
        "allowed_job_kinds": list(permit.allowed_job_kinds),
        "valid_until": permit.valid_until.isoformat(),
        "reason": reason,
        "evidence": evidence,
        "actor": actor,
    }
    return json.dumps(entry, sort_keys=True).encode("utf-8") + b"\n"


def _validate_justification(reason: str, evidence: list[str]) -> None:
    if (
        not reason.strip()
        or len(reason) > 2000
        or not evidence
        or len(evidence) > 20
        or any(not item.strip() or len(item) > 512 for item in evidence)
    ):
        raise PermitError(
            "A reason and 1-20 evidence references (each at most 512 chars) are required."
        )


def _days(days: int) -> timedelta:
    if not 1 <= days <= MAX_DAYS:
        raise PermitError(f"Validity must be 1 to {MAX_DAYS} days.")
    return timedelta(days=days)


def bootstrap(
    root: Path,
    classes: list[str],
    *,
    reason: str,
    evidence: list[str],
    actor: dict,
    days: int = DEFAULT_DAYS,
) -> Permit:
    """Issue the first normal permit. Refuses when any earlier authority exists."""
    _validate_justification(reason, evidence)
    validity = _days(days)
    root.mkdir(parents=True, exist_ok=True)
    if (root / PERMIT).exists() or (root / AUDIT).exists():
        raise PermitError(
            "A dispatch authority already exists; use status or a recovery operation instead."
        )
    with file_lock(root / LOCK, exclusive=True, create=True):
        if (root / PERMIT).exists() or (root / AUDIT).exists():
            raise PermitError("A dispatch authority already exists.")
        if os.name != "nt":
            os.chmod(root / LOCK, 0o644)
        now = datetime.now(timezone.utc)
        permit = Permit(
            schema=SCHEMA,
            authority_id=uuid4(),
            generation=1,
            mode="normal",
            allowed_job_kinds=tuple(classes),
            valid_until=now + validity,
            issued_at=now,
            operation="bootstrap",
        )
        _commit(root, permit, reason, evidence, actor)
        return permit


def _commit(root: Path, permit: Permit, reason: str, evidence: list[str], actor: dict) -> None:
    data = json.dumps(permit.document(), sort_keys=True).encode("utf-8")
    replace_durably(root / PERMIT, data)
    append_durably(root / AUDIT, _audit_entry(permit, reason, evidence, actor))


def transition(
    root: Path,
    operation: Operation,
    classes: list[str],
    *,
    reason: str,
    evidence: list[str],
    actor: dict,
    resume: str | None = None,
    days: int | None = None,
) -> Permit:
    """Apply one trusted mode/class change (generation +1) or a renewal (generation kept)."""
    _validate_justification(reason, evidence)
    if not (root / LOCK).exists():
        raise PermitError("No dispatch authority exists; run the fresh-install bootstrap first.")
    with file_lock(root / LOCK, exclusive=True):
        current, problem = read_permit(root)
        if current is None:
            raise PermitError(f"The current permit is unusable ({problem}); restore it from audit.")
        now = datetime.now(timezone.utc)
        changes: dict = {"issued_at": now, "operation": operation}
        if operation == "renew":
            changes["valid_until"] = now + _days(days or DEFAULT_DAYS)
        else:
            changes["generation"] = current.generation + 1
            changes["valid_until"] = max(current.valid_until, now + _days(DEFAULT_DAYS))
            if operation == "enter_recovery":
                changes.update(mode="recovery", allowed_job_kinds=())
            elif operation == "resume":
                if current.mode != "recovery":
                    raise PermitError("Resume applies only while dispatch is in recovery mode.")
                if resume not in classes:
                    raise PermitError("Choose a known permit class to resume.")
                if resume in current.allowed_job_kinds:
                    raise PermitError("That class is already resumed.")
                changes["allowed_job_kinds"] = (*current.allowed_job_kinds, resume)
            elif operation == "exit_recovery":
                if current.mode != "recovery":
                    raise PermitError("Dispatch is not in recovery mode.")
                changes.update(mode="normal", allowed_job_kinds=tuple(classes))
            else:
                raise PermitError("Unsupported dispatch operation.")
        permit = Permit.model_validate({**current.document(), **_json(changes)})
        _commit(root, permit, reason, evidence, actor)
        return permit


def _json(changes: dict) -> dict:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in changes.items()
    }


def audit_tail(root: Path, limit: int = 25) -> list[dict]:
    try:
        lines = (root / AUDIT).read_bytes().splitlines()[-limit:]
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except ValueError:
            result.append({"invalid": True})
    return result
