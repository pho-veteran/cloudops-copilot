from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from cloudops.features.access import service
from cloudops.features.access.dependencies import (
    COOKIE,
    check_origin,
    current_session,
    mutation_session,
)
from cloudops.features.access.models import LoginSession
from cloudops.features.access.schemas import LoginRequest, SessionView
from cloudops.shared.config import settings
from cloudops.shared.database import get_db
from cloudops.shared.errors import ERROR_RESPONSES

router = APIRouter(prefix="/api", tags=["access"], responses=ERROR_RESPONSES)


@router.post("/login", operation_id="login", response_model=SessionView)
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    check_origin(request)
    token, result = service.login(db, body.username, body.password, request.cookies.get(COOKIE))
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=settings().cookie_secure,
        samesite="lax",
        max_age=settings().session_seconds,
        path="/",
    )
    return result


@router.get("/session", operation_id="get_session", response_model=SessionView)
def session(result: SessionView = Depends(current_session)):
    return result


@router.post("/logout", operation_id="logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    session: SessionView = Depends(mutation_session),
    db: Session = Depends(get_db),
):
    with db.begin():
        row = db.get(LoginSession, service.token_hash(request.cookies[COOKIE]))
        if row:
            db.delete(row)
    response.delete_cookie(
        COOKIE, path="/", secure=settings().cookie_secure, httponly=True, samesite="lax"
    )
