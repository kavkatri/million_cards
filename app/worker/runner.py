"""Worker process.

Runs ``WORKER_CONCURRENCY`` coroutines, each claiming and executing tasks. Scale
horizontally with ``docker compose up -d --scale worker=N``; the account-scoped
limiter keeps the aggregate request rate legal no matter how many run.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import uuid

import structlog
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import Account
from app.db.session import SessionLocal
from app.marketplace.factory import build_adapter
from app.worker import queue
from app.worker.handlers import TaskFailure, handle

log = structlog.get_logger(__name__)
settings = get_settings()

IDLE_SLEEP = 2.0


class AdapterPool:
    """One adapter (and therefore one HTTP connection pool) per account."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._adapters: dict[int, object] = {}
        self._lock = asyncio.Lock()

    async def get(self, account: Account):
        async with self._lock:
            if account.id not in self._adapters:
                self._adapters[account.id] = build_adapter(account, self._redis)
            return self._adapters[account.id]

    async def close(self) -> None:
        for adapter in self._adapters.values():
            with contextlib.suppress(Exception):
                await adapter.close()
        self._adapters.clear()


async def _worker_loop(worker_id: str, pool: AdapterPool, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with SessionLocal() as session:
                tasks = await queue.claim(session, worker_id, limit=1)
                if not tasks:
                    await _sleep_or_stop(stop, IDLE_SLEEP)
                    continue

                task = tasks[0]
                account = await session.get(Account, task.account_id)
                if account is None or not account.enabled:
                    await queue.skip(session, task, "account missing or disabled")
                    continue

                adapter = await pool.get(account)
                try:
                    await handle(session, task, adapter)
                except TaskFailure as exc:
                    await session.rollback()
                    await queue.fail(session, task, str(exc), retry_after_s=exc.retry_after_s)
                except Exception as exc:  # noqa: BLE001 - a bad task must not kill the worker
                    await session.rollback()
                    log.exception("task.crashed", task=task.id)
                    await queue.fail(session, task, f"{type(exc).__name__}: {exc}")
                else:
                    await queue.complete(session, task)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive through DB blips
            log.exception("worker.loop_error", worker=worker_id)
            await _sleep_or_stop(stop, 5.0)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def main() -> None:
    configure_logging()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    pool = AdapterPool(redis)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    host = socket.gethostname()
    pid = os.getpid()
    workers = [
        asyncio.create_task(
            _worker_loop(f"{host}:{pid}:{i}:{uuid.uuid4().hex[:6]}", pool, stop)
        )
        for i in range(settings.worker_concurrency)
    ]
    log.info("worker.started", concurrency=settings.worker_concurrency, host=host, pid=pid)

    await stop.wait()
    log.info("worker.stopping")
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)
    await pool.close()
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
