"""Starting and finishing runs.

One run per line at a time. That invariant lives here, in a database constraint
check rather than in a filesystem lock, so it holds across processes and hosts
and cannot be defeated by a stale lock file after a crash.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, ProductLine, Run, RunState, Task, TaskState
from app.engine.reconcile import PlanSummary, ensure_skus, plan_run, sync_catalogue
from app.marketplace.factory import build_adapter

log = structlog.get_logger(__name__)


class RunAlreadyActive(RuntimeError):
    pass


async def active_run(session: AsyncSession, line_id: int) -> Run | None:
    return (
        await session.execute(
            select(Run)
            .where(
                Run.line_id == line_id,
                Run.state.in_([RunState.PLANNING, RunState.RUNNING]),
                Run.dry_run.is_(False),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def start_run(
    session: AsyncSession,
    line: ProductLine,
    redis: Redis,
    *,
    dry_run: bool = False,
    triggered_by: str = "manual",
) -> tuple[Run, PlanSummary]:
    """Sync state, plan the work, and queue it.

    A dry run does everything except write tasks, so the returned summary is an
    accurate preview of what a real run would do right now.
    """
    if not dry_run:
        existing = await active_run(session, line.id)
        if existing is not None:
            raise RunAlreadyActive(
                f"line {line.name!r} already has run #{existing.id} in progress"
            )

    run = Run(
        line_id=line.id,
        state=RunState.PLANNING,
        dry_run=dry_run,
        triggered_by=triggered_by,
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    account = await session.get(Account, line.account_id)
    if account is None or not account.enabled:
        run.state = RunState.FAILED
        run.message = "account missing or disabled"
        run.finished_at = datetime.now(UTC)
        await session.commit()
        raise RuntimeError(run.message)

    adapter = build_adapter(account, redis)
    try:
        await ensure_skus(session, line)
        await sync_catalogue(session, line, adapter)

        quota = None
        try:
            quota = (await adapter.creation_quota()).total
        except Exception as exc:  # noqa: BLE001 - quota is advisory, not fatal
            log.warning("quota.unavailable", line=line.id, error=str(exc))

        summary = await plan_run(session, line, run, quota_remaining=quota)
    finally:
        await adapter.close()

    run.stats = summary.as_dict()
    if dry_run:
        run.state = RunState.DONE
        run.finished_at = datetime.now(UTC)
        run.message = "dry run - nothing was written"
    else:
        run.state = RunState.RUNNING if summary.tasks_created else RunState.DONE
        if not summary.tasks_created:
            run.finished_at = datetime.now(UTC)
            run.message = "nothing to do - catalogue already matches the line definition"
    await session.commit()
    return run, summary


async def refresh_run_state(session: AsyncSession, run: Run) -> Run:
    """Close out a run once its tasks are all resolved."""
    if run.state is not RunState.RUNNING:
        return run

    counts = dict(
        (
            await session.execute(
                select(Task.state, func.count())
                .where(Task.run_id == run.id)
                .group_by(Task.state)
            )
        ).all()
    )
    outstanding = counts.get(TaskState.PENDING, 0) + counts.get(TaskState.RUNNING, 0)
    if outstanding:
        return run

    failed = counts.get(TaskState.FAILED, 0)
    run.state = RunState.FAILED if failed else RunState.DONE
    run.finished_at = datetime.now(UTC)
    stats = dict(run.stats or {})
    stats["tasks"] = {k.value if hasattr(k, "value") else str(k): v for k, v in counts.items()}
    run.stats = stats
    run.message = f"{failed} task(s) failed" if failed else "completed"
    await session.commit()
    return run
