"""Scheduler: start due runs, and close out finished ones.

Replaces cron. The difference that matters is not the syntax but the guarantee:
``start_run`` refuses to start a second run for a line that is already active, so
a run that overruns its schedule is skipped rather than piled on top of itself.

The system this replaces launched a fresh copy nightly regardless, and because a
run took ~38 hours, six copies ended up racing over one account and overwriting
each other's progress files.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import UTC, datetime

import structlog
from redis.asyncio import Redis
from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import ProductLine, Run, RunState
from app.db.session import SessionLocal
from app.engine.runs import RunAlreadyActive, refresh_run_state, start_run

log = structlog.get_logger(__name__)
settings = get_settings()

TICK_SECONDS = 30


def cron_matches(expr: str, when: datetime) -> bool:
    """Minimal 5-field cron matcher: minute hour day month weekday.

    Supports ``*``, ``a,b``, ``a-b`` and ``*/n``. That covers every schedule this
    system needs; anything more exotic belongs in a real scheduler.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron needs 5 fields, got {len(fields)}: {expr!r}")

    values = (when.minute, when.hour, when.day, when.month, when.isoweekday() % 7)
    ranges = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))

    for field, value, (lo, hi) in zip(fields, values, ranges, strict=True):
        if not _field_matches(field, value, lo, hi):
            return False
    return True


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


async def _tick(redis: Redis, last_fired: dict[int, str]) -> None:
    now = datetime.now(UTC)
    stamp = now.strftime("%Y-%m-%dT%H:%M")

    async with SessionLocal() as session:
        # 1) close out runs whose tasks have all resolved
        running = (
            await session.execute(select(Run).where(Run.state == RunState.RUNNING))
        ).scalars().all()
        for run in running:
            await refresh_run_state(session, run)

        # 2) start due lines
        lines = (
            await session.execute(
                select(ProductLine).where(
                    ProductLine.enabled.is_(True), ProductLine.schedule_cron.is_not(None)
                )
            )
        ).scalars().all()

        for line in lines:
            if last_fired.get(line.id) == stamp:
                continue
            try:
                due = cron_matches(line.schedule_cron, now)
            except ValueError as exc:
                log.warning("schedule.invalid", line=line.id, error=str(exc))
                continue
            if not due:
                continue

            last_fired[line.id] = stamp
            try:
                run, summary = await start_run(
                    session, line, redis, triggered_by="schedule"
                )
                log.info(
                    "schedule.started", line=line.id, run=run.id, **summary.as_dict()
                )
            except RunAlreadyActive as exc:
                # The previous run is still going. Skipping is the correct
                # behaviour; it is recorded so a chronically overrunning line is
                # visible rather than silent.
                log.warning("schedule.skipped", line=line.id, reason=str(exc))
            except Exception:  # noqa: BLE001
                log.exception("schedule.failed", line=line.id)


async def main() -> None:
    configure_logging()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    last_fired: dict[int, str] = {}
    log.info("scheduler.started", tick=TICK_SECONDS)
    while not stop.is_set():
        try:
            await _tick(redis, last_fired)
        except Exception:  # noqa: BLE001
            log.exception("scheduler.tick_failed")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)

    await redis.aclose()
    log.info("scheduler.stopped")


if __name__ == "__main__":
    asyncio.run(main())
