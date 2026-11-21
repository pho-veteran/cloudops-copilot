from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from cloudops.features.access.dependencies import current_session
from cloudops.features.connections.operations import read_operations
from cloudops.features.connections.operations_schemas import OperationsView
from cloudops.shared.database import get_db
from cloudops.shared.dispatch_permit import DispatchGate
from cloudops.shared.errors import ERROR_RESPONSES


def operations_router(gate: DispatchGate) -> APIRouter:
    router = APIRouter(tags=["connections"], responses=ERROR_RESPONSES)

    @router.get(
        "/api/connections/operations",
        operation_id="read_connection_operations",
        response_model=OperationsView,
        dependencies=[Depends(current_session)],
    )
    def operations(db: Session = Depends(get_db)):
        # A promotion can add a job and clear capacity in one commit. Observe both from one snapshot.
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return read_operations(db, gate)

    return router
