import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, password_digest: str) -> bool:
    return password_hash.verify(password, password_digest)


def create_access_token(subject: str, claims: dict[str, Any] | None = None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        "type": "access",
    }
    payload.update(claims or {})
    return jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def create_refresh_token(subject: str) -> tuple[str, datetime]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=settings.refresh_token_expire_days)
    payload: dict[str, Any] = {
        "sub": subject,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": expires_at,
        "type": "refresh",
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, expires_at


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(
        token,
        settings.jwt_secret_key.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    payload = decode_access_token(token)
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("unexpected token type")
    return payload


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
