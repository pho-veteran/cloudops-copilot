import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cloudops.features.access.models import LoginSession, User
from cloudops.features.access.schemas import Principal, SessionView
from cloudops.shared.config import settings
from cloudops.shared.database import database_now
from cloudops.shared.errors import DomainError


def password_hash(password: str) -> str:
    if not 12 <= len(password) <= 256:
        raise ValueError("Use a password of 12..256 characters.")
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"scrypt${salt}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    _, salt, expected = encoded.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return hmac.compare_digest(actual.hex(), expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def revoke_all_sessions(db: Session):
    """Trusted deployment restore step; never exposed through the application API."""
    with db.begin():
        db.execute(delete(LoginSession))


def view(user: User, session: LoginSession) -> SessionView:
    return SessionView(
        principal=Principal(id=user.id, username=user.username, role=user.role),
        csrf_token=session.csrf_token,
        expires_at=session.expires_at,
    )


def login(db: Session, username: str, password: str, old_token: str | None):
    with db.begin():
        user = db.scalar(select(User).where(User.username == username).with_for_update())
        # A fixed valid hash avoids skipping expensive password work for unknown users.
        dummy = "scrypt$00000000000000000000000000000000$" + "00" * 64
        valid = password_matches(password, user.password_hash if user else dummy)
        if not user or not valid:
            raise DomainError(401, "invalid_credentials", "Username or password is incorrect.")
        now = database_now(db)
        db.execute(delete(LoginSession).where(LoginSession.expires_at <= now))
        if old_token:
            db.execute(delete(LoginSession).where(LoginSession.token_hash == token_hash(old_token)))
        token = secrets.token_urlsafe(32)
        session = LoginSession(
            token_hash=token_hash(token),
            user_id=user.id,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + timedelta(seconds=settings().session_seconds),
        )
        db.add(session)
        return token, view(user, session)


def authenticate(db: Session, token: str | None) -> SessionView:
    session = db.get(LoginSession, token_hash(token)) if token else None
    if not session or session.expires_at <= database_now(db):
        raise DomainError(
            401, "session_expired", "Sign in to continue. Your saved work is still available."
        )
    user = db.get(User, session.user_id)
    if not user:
        raise DomainError(401, "session_expired", "Sign in to continue.")
    return view(user, session)


def provision(db: Session, username: str, password: str, role: str):
    from cloudops.features.access.schemas import LoginRequest

    LoginRequest(username=username, password=password)
    if role not in {"viewer", "operator"}:
        raise ValueError("Role must be viewer or operator.")
    encoded = password_hash(password)
    with db.begin():
        user = db.scalar(select(User).where(User.username == username).with_for_update())
        if user:
            db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
            user.password_hash, user.role = encoded, role
        else:
            db.add(User(username=username, password_hash=encoded, role=role))


def set_role(db: Session, username: str, role: str) -> None:
    if role not in {"viewer", "operator"}:
        raise DomainError(422, "invalid_role", "Role must be viewer or operator.")
    with db.begin():
        user = db.scalar(select(User).where(User.username == username).with_for_update())
        if not user:
            raise DomainError(404, "account_not_found", "Account does not exist.")
        user.role = role
        db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))


def revoke_sessions(db: Session, username: str) -> None:
    with db.begin():
        user = db.scalar(select(User).where(User.username == username).with_for_update())
        if not user:
            raise DomainError(404, "account_not_found", "Account does not exist.")
        db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
