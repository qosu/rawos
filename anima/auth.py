"""
anima auth layer — bcrypt passwords, JWT access tokens, refresh tokens.
Stateless access tokens (15 min), stateful refresh tokens (7 days, stored in DB).
"""
from __future__ import annotations

import hashlib
import secrets
import time
import uuid

import bcrypt
from jose import JWTError, jwt

from anima.config import settings
import anima.db as db
from anima.models import Event, EventType, User, UserTier


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# Access token (JWT, stateless)
# ---------------------------------------------------------------------------

def create_access_token(user_id: str) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + settings.access_token_expire_minutes * 60,
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> str:
    """Returns user_id or raises JWTError."""
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )
    if payload.get("type") != "access":
        raise JWTError("wrong token type")
    return payload["sub"]


# ---------------------------------------------------------------------------
# Refresh token (opaque, stored in DB as hash)
# ---------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_refresh_token(user_id: str) -> str:
    raw = secrets.token_urlsafe(48)
    expires_at = int(time.time()) + settings.refresh_token_expire_days * 86_400
    db.save_refresh_token(
        token_id=str(uuid.uuid4()),
        user_id=user_id,
        token_hash=_hash_token(raw),
        expires_at=expires_at,
    )
    return raw


def rotate_refresh_token(old_raw: str) -> tuple[str, str] | None:
    """Atomically revoke old refresh token and issue new pair. Returns (access, refresh) or None."""
    row = db.get_refresh_token(_hash_token(old_raw))
    if not row:
        return None
    user_id = row["user_id"]
    db.revoke_refresh_token(_hash_token(old_raw))
    return create_access_token(user_id), create_refresh_token(user_id)


# ---------------------------------------------------------------------------
# Signup / Login
# ---------------------------------------------------------------------------

class AuthError(Exception):
    pass


def signup(email: str, password: str) -> tuple[User, str, str]:
    """
    Create new user. Returns (user, access_token, refresh_token).
    Raises AuthError on duplicate email, weak password, or invalid email format.
    """
    if len(password) < 8:
        raise AuthError("password must be at least 8 characters")
    if db.get_user_by_email(email):
        raise AuthError("email already registered")

    from pydantic import ValidationError
    try:
        user = User(
            email=email,
            password_hash=hash_password(password),
            tier=UserTier.FREE,
            token_budget_daily=settings.free_tier_daily_tokens,
        )
    except (ValidationError, ValueError) as e:
        raise AuthError(str(e)) from e
    # Grant admin if email is in the admin_emails allowlist
    if email.lower() in [e.lower() for e in settings.admin_emails]:
        user = user.model_copy(update={"is_admin": True})
    db.create_user(user)
    db.log_event(Event(user_id=user.id, type=EventType.AUTH_SIGNUP))

    access  = create_access_token(user.id)
    refresh = create_refresh_token(user.id)
    return user, access, refresh


def login(email: str, password: str) -> tuple[User, str, str]:
    """
    Authenticate existing user. Returns (user, access_token, refresh_token).
    Raises AuthError on bad credentials.
    """
    user = db.get_user_by_email(email)
    if not user or not verify_password(password, user.password_hash):
        raise AuthError("invalid email or password")

    db.log_event(Event(user_id=user.id, type=EventType.AUTH_LOGIN))

    access  = create_access_token(user.id)
    refresh = create_refresh_token(user.id)
    return user, access, refresh
