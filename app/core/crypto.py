"""Encryption for marketplace API tokens.

Tokens are long-lived bearer credentials for a seller account: anyone holding one
can rewrite that seller's catalogue. They are stored encrypted so a database dump
or a stray backup does not hand them over, and they are never rendered back into
the UI -- only a fingerprint is shown.
"""

from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover - depends on deployment key
        raise RuntimeError(
            "Could not decrypt a stored API token. This usually means "
            "TOKEN_ENCRYPTION_KEY changed since the token was saved."
        ) from exc


def fingerprint(plaintext: str) -> str:
    """Short, non-reversible identifier so the UI can show *which* token is stored."""
    return hashlib.sha256(plaintext.encode()).hexdigest()[:12]
