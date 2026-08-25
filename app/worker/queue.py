"""Postgres-backed task queue.

Deliberately not Redis. The task table is already the run's audit trail -- what
was attempted, how often, and why it failed -- and keeping the queue in the same
transactional store means a claim and its state change cannot disagree after a
crash. Redis is used only for rate limiting, where losing state on restart is
harmless.

Claiming uses ``FOR UPDATE SKIP LOCKED``, so any number of workers can drain the
queue concurrently without blocking one another. Combined with the account-scoped
rate limiter, that gives parallelism *within* one account as well as across
accounts: many workers may hold tasks for the same account, and the limiter --
not the worker count -- paces their marketplace calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Task, TaskState

log = structlog.get_logger(__name__)
settings = get_settings()


async def claim(
    session: AsyncSession,
    worker_id: str,
    *,
    limit: int = 1,
    account_id: int | None = None,
) -> list[Task]:
    """Atomically take up to ``limit`` runnable tasks."""
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=settings.task_lease_seconds)

    runnable = and_(
        Task.state.in_([TaskState.PENDING, TaskState.RUNNING]),
        or_(Task.available_at.is_(None), Task.available_at <= now),
        # RUNNING rows are claimable only once their lease has expired, which is
        # how a task survives a worker being killed mid-flight.
        or_(Task.locked_until.is_(None), Task.locked_until <= now),
    )
    if account_id is not None:
        runnable = and_(runnable, Task.account_id == account_id)

    stmt = (
        select(Task.id)
        .where(runnable)
        .order_by(Task.priority, Task.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    ids = (await session.execute(stmt)).scalars().all()
    if not ids:
        await session.commit()
        return []

    await session.execute(
        update(Task)
        .where(Task.id.in_(ids))
        .values(
            state=TaskState.RUNNING,
            locked_by=worker_id,
            locked_until=lease_until,
            attempts=Task.attempts + 1,
        )
    )
    await session.commit()

    tasks = (await session.execute(select(Task).where(Task.id.in_(ids)))).scalars().all()
    return list(tasks)


async def complete(session: AsyncSession, task: Task) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(state=TaskState.DONE, locked_until=None, locked_by=None, last_error=None)
    )
    await session.commit()


async def fail(
    session: AsyncSession, task: Task, error: str, *, retry_after_s: float | None = None
) -> None:
    """Record a failure and either schedule a retry or give up.

    Backoff is exponential unless the marketplace told us how long to wait (a
    quota reset, say), in which case we honour that instead of guessing.
    """
    exhausted = task.attempts >= task.max_attempts
    if exhausted:
        await session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                state=TaskState.FAILED,
                locked_until=None,
                locked_by=None,
                last_error=error[:2000],
            )
        )
        log.warning("task.failed", task=task.id, aspect=task.aspect.value, error=error[:200])
    else:
        delay = retry_after_s if retry_after_s is not None else min(600, 5 * 2**task.attempts)
        await session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                state=TaskState.PENDING,
                locked_until=None,
                locked_by=None,
                last_error=error[:2000],
                available_at=datetime.now(UTC) + timedelta(seconds=delay),
            )
        )
        log.info("task.retry", task=task.id, attempt=task.attempts, delay=delay)
    await session.commit()


async def skip(session: AsyncSession, task: Task, reason: str) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(
            state=TaskState.SKIPPED, locked_until=None, locked_by=None, last_error=reason[:2000]
        )
    )
    await session.commit()
