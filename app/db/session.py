"""Database engine and session factory.

The engine is built lazily rather than at import time. Constructing it eagerly
would mean that importing *any* module which transitively reaches this one --
including pure logic like the scheduler's cron matcher -- requires a database
driver to be installed and a valid URL to be configured. That makes the code
hard to test and turns a misconfigured URL into an import-time crash rather than
a connection error at the point of use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)


class _SessionProxy:
    """Callable that defers engine creation until a session is actually opened.

    Lets call sites keep the natural ``async with SessionLocal() as session``
    form without importing a factory function everywhere.
    """

    def __call__(self) -> AsyncSession:
        return get_sessionmaker()()


SessionLocal = _SessionProxy()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
