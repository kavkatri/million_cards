"""Live progress events.

The dashboard confirms success by pulsing the exact element that changed. That
only works if the browser learns about a completed task within a moment of it
happening -- polling would both lag and lie.

Workers publish one small event per completed task onto a Redis pub/sub channel.
The web process subscribes and relays to browsers over Server-Sent Events. SSE
rather than WebSockets because the traffic is strictly one-way and SSE
reconnects on its own; there is no client->server message to carry.

Events are advisory. If Redis drops one, the next page load still shows the
truth, because the truth lives in the database and ultimately in the
marketplace -- never in this stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import structlog
from redis.asyncio import Redis

log = structlog.get_logger(__name__)

CHANNEL = "millioncards:events"


@dataclass(slots=True)
class ProgressEvent:
    """One thing that happened, in terms the dashboard can render.

    ``unit`` and ``count`` let the UI say "6 photos" rather than "1 task", which
    is what a person watching actually wants to know.
    """

    type: str  # task.done | task.failed | run.started | run.finished
    line_id: int | None = None
    run_id: int | None = None
    aspect: str | None = None
    unit: str | None = None
    count: int = 0
    label: str | None = None
    detail: str | None = None
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


async def publish(redis: Redis, event: ProgressEvent) -> None:
    """Fire-and-forget. A telemetry failure must never fail the actual work."""
    try:
        await redis.publish(CHANNEL, event.to_json())
    except Exception as exc:  # noqa: BLE001
        log.warning("events.publish_failed", error=str(exc), type=event.type)


async def stream(redis: Redis, stop: asyncio.Event | None = None) -> AsyncIterator[str]:
    """Yield SSE frames until the client disconnects.

    A comment frame every 15 s keeps proxies from closing an idle connection --
    without it a quiet catalogue looks like a broken dashboard.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        yield ": connected\n\n"
        while True:
            if stop is not None and stop.is_set():
                return
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=15.0
            )
            if message is None:
                yield ": keepalive\n\n"
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            yield f"data: {data}\n\n"
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(CHANNEL)
            await pubsub.aclose()
