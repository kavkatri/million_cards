"""Test environment.

These variables must be set before anything imports ``app``, because
``app.main`` and several modules read settings at import time. pytest loads
conftest before test modules, which is what makes this work.
"""

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

_TMP = Path(tempfile.mkdtemp(prefix="millioncards-tests-"))

os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MEDIA_ROOT", str(_TMP / "media"))
