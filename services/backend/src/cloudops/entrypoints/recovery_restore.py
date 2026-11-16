"""Trusted post-restore cleanup with app processes stopped and both dispatch authorities closed."""

import json

from cloudops.entrypoints import metadata  # noqa: F401
from cloudops.entrypoints.dispatch import GATE
from cloudops.entrypoints.dispatch_admin import trusted_actor
from cloudops.features.recovery.restore import closed_gateway_targets, retire
from cloudops.shared.database import require_schema, sessions
from cloudops.shared.dispatch_permit import PermitError, dispatch_root


def main():
    root = dispatch_root()
    if root is None:
        raise PermitError("Mount the original trusted host dispatch authority.")
    actor = trusted_actor(root, create=False)
    require_schema()
    with GATE.admitted() as admission:
        if admission.reason != "recovery_mode" or admission.kinds:
            raise PermitError("A valid locked host recovery permit is required.")
        permit, kinds, reason = GATE.current()
        if (
            permit is None
            or permit.mode != "recovery"
            or kinds
            or permit.allowed_job_kinds
            or reason not in (None, "recovery_mode", "class_denied")
        ):
            raise PermitError(
                "Keep every host dispatch class denied in recovery during restored-work cleanup."
            )
        targets = closed_gateway_targets()
        with sessions()() as db, db.begin():
            result = retire(db)
    print(
        json.dumps(
            {
                **result,
                "configured_gateway_targets_closed": len(targets),
                "permit_generation": permit.generation,
                "actor": actor,
            }
        )
    )


if __name__ == "__main__":
    main()
