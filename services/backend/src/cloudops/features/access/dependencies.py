import secrets

from fastapi import Depends, Header, Request, Security
from fastapi.security import APIKeyCookie

from cloudops.features.access.schemas import SessionView
from cloudops.features.access.service import authenticate
from cloudops.shared.config import settings
from cloudops.shared.database import sessions
from cloudops.shared.errors import DomainError

COOKIE = "cloudops_session"
cookie_auth = APIKeyCookie(name=COOKIE, auto_error=False)


def check_origin(request: Request):
    if request.headers.getlist("origin") != [settings().web_origin]:
        raise DomainError(403, "origin_denied", "Use the configured application origin.")


def current_session(token: str | None = Security(cookie_auth)) -> SessionView:
    with sessions()() as db:
        return authenticate(db, token)


def check_csrf(request: Request, session: SessionView):
    check_origin(request)
    tokens = request.headers.getlist("x-csrf-token")
    if len(tokens) != 1 or not secrets.compare_digest(
        tokens[0].encode("utf-8"), session.csrf_token.encode("utf-8")
    ):
        raise DomainError(403, "csrf_denied", "Refresh your session before submitting this action.")


def mutation_session(
    request: Request,
    session: SessionView = Depends(current_session),
    x_csrf_token: str | None = Header(default=None),
):
    check_csrf(request, session)
    return session


def operator(
    request: Request,
    session: SessionView = Depends(current_session),
    x_csrf_token: str | None = Header(default=None),
):
    if session.principal.role != "operator":
        raise DomainError(
            403,
            "permission_denied",
            "Viewer access is read-only. An Operator must perform this action.",
        )
    check_csrf(request, session)
    return session.principal
