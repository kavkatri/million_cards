"""Session auth.

This service holds credentials that can rewrite an entire storefront, so it is
never exposed unauthenticated. Sessions are signed cookies; passwords are hashed
with Argon2id.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.core.config import get_settings

SESSION_COOKIE = "mc_session"
SESSION_MAX_AGE = 60 * 60 * 12

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, Exception):
        return False


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="mc-session")


def issue_session(user_id: int) -> str:
    return _serializer().dumps({"uid": user_id})


def read_session(request: Request) -> int | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except (BadSignature, Exception):
        return None
    return data.get("uid")
