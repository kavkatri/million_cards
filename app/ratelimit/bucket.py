"""Distributed rate limiter shared by every worker.

Marketplace limits are per *seller account*, per category -- not per process. Two
workers touching the same account share one budget, so the limiter has to live
outside the workers. It lives in Redis, keyed by ``(account, category)``.

This is what lets the system run many workers against a single account: workers do
not carve up a quota between themselves, they each ask this limiter for permission
immediately before a call. Throughput is bounded by the limiter, so adding workers
increases concurrency during waits without ever breaching the documented rate.

The marketplace documents two constraints per category, and both must hold:

* a **burst/interval** pair -- at most ``burst`` requests bunched together,
  replenishing one every ``interval``. That is a token bucket.
* a **limit/period** pair -- no more than ``limit`` requests in any ``period``.
  That is a sliding window.

A single Lua script evaluates both atomically, so a request is admitted only if it
satisfies each. When denied it returns how long to wait, so callers sleep exactly
as long as needed rather than polling.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from redis.asyncio import Redis

# KEYS[1] token-bucket hash, KEYS[2] sliding-window zset
# ARGV: capacity, refill_per_ms, now_ms, period_ms, window_limit, cost, ttl_ms
_SCRIPT = """
local bucket_key = KEYS[1]
local window_key = KEYS[2]

local capacity   = tonumber(ARGV[1])
local refill     = tonumber(ARGV[2])
local now        = tonumber(ARGV[3])
local period     = tonumber(ARGV[4])
local win_limit  = tonumber(ARGV[5])
local cost       = tonumber(ARGV[6])
local ttl        = tonumber(ARGV[7])

-- 1) sliding window over the longer period
redis.call('ZREMRANGEBYSCORE', window_key, 0, now - period)
local used = redis.call('ZCARD', window_key)
if used + cost > win_limit then
  local oldest = redis.call('ZRANGE', window_key, 0, 0, 'WITHSCORES')
  local wait = period
  if oldest[2] then
    wait = (tonumber(oldest[2]) + period) - now
  end
  if wait < 1 then wait = 1 end
  return {0, math.ceil(wait)}
end

-- 2) token bucket over the short interval
local state = redis.call('HMGET', bucket_key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed > 0 then
  tokens = math.min(capacity, tokens + elapsed * refill)
  ts = now
end

if tokens < cost then
  local need = cost - tokens
  local wait = math.ceil(need / refill)
  if wait < 1 then wait = 1 end
  return {0, wait}
end

tokens = tokens - cost
redis.call('HSET', bucket_key, 'tokens', tokens, 'ts', ts)
redis.call('PEXPIRE', bucket_key, ttl)

-- Window members must be unique or ZADD overwrites rather than appends. A
-- counter on the bucket hash gives uniqueness without depending on math.random,
-- which is seeded per-script-run in Redis Lua and would repeat across calls.
for i = 1, cost do
  local seq = redis.call('HINCRBY', bucket_key, 'seq', 1)
  redis.call('ZADD', window_key, now, seq)
end
redis.call('PEXPIRE', window_key, ttl)

return {1, 0}
"""


@dataclass(frozen=True)
class RateLimit:
    """One documented limit row.

    ``period_ms``/``limit`` is the sliding window; ``interval_ms``/``burst`` is the
    token bucket. Both are enforced.
    """

    limit: int
    period_ms: int
    interval_ms: int
    burst: int

    @property
    def refill_per_ms(self) -> float:
        return 1.0 / self.interval_ms

    def describe(self) -> str:
        return (
            f"{self.limit}/{self.period_ms / 1000:g}s, "
            f"interval {self.interval_ms}ms, burst {self.burst}"
        )


class RateLimiter:
    def __init__(self, redis: Redis, namespace: str = "rl") -> None:
        self._redis = redis
        self._ns = namespace
        self._sha: str | None = None

    async def _ensure_script(self) -> str:
        if self._sha is None:
            self._sha = await self._redis.script_load(_SCRIPT)
        return self._sha

    async def try_acquire(self, key: str, limit: RateLimit, cost: int = 1) -> int:
        """Return 0 if admitted, else milliseconds to wait before retrying."""
        sha = await self._ensure_script()
        now_ms = int(time.time() * 1000)
        ttl = max(limit.period_ms, limit.interval_ms * limit.burst) * 4

        allowed, wait_ms = await self._redis.evalsha(
            sha,
            2,
            f"{self._ns}:{key}:bucket",
            f"{self._ns}:{key}:window",
            limit.burst,
            limit.refill_per_ms,
            now_ms,
            limit.period_ms,
            limit.limit,
            cost,
            ttl,
        )
        return 0 if int(allowed) == 1 else int(wait_ms)

    async def acquire(
        self, key: str, limit: RateLimit, cost: int = 1, timeout: float | None = None
    ) -> None:
        """Block until admitted.

        Sleeps for exactly the interval the limiter reports, so a crowd of workers
        waiting on the same account wakes up staggered rather than stampeding.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            wait_ms = await self.try_acquire(key, limit, cost)
            if wait_ms == 0:
                return
            if deadline is not None and time.monotonic() + wait_ms / 1000 > deadline:
                raise TimeoutError(f"rate limit wait exceeded timeout for {key}")
            await asyncio.sleep(wait_ms / 1000)
