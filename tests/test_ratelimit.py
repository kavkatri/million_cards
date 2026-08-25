"""Rate limiter.

The pure arithmetic is tested unconditionally. The Lua script needs a real Redis
and is skipped without one -- run `docker compose up -d redis` and set
REDIS_TEST_URL to exercise it.
"""

import os
import time

import pytest

from app.ratelimit.bucket import RateLimit, RateLimiter

REDIS_URL = os.getenv("REDIS_TEST_URL")


class TestRateLimitMath:
    def test_refill_rate_is_one_token_per_interval(self):
        limit = RateLimit(limit=100, period_ms=60_000, interval_ms=600, burst=5)
        # 600ms per token => 1/600 tokens per ms => 5 tokens in 3s
        assert limit.refill_per_ms == pytest.approx(1 / 600)
        assert limit.refill_per_ms * 3000 == pytest.approx(5.0)

    def test_cards_upload_is_ten_times_slower_than_content(self):
        content = RateLimit(limit=100, period_ms=60_000, interval_ms=600, burst=5)
        upload = RateLimit(limit=10, period_ms=60_000, interval_ms=6_000, burst=5)
        assert content.refill_per_ms / upload.refill_per_ms == pytest.approx(10.0)

    def test_describe_is_human_readable(self):
        limit = RateLimit(limit=10, period_ms=6_000, interval_ms=600, burst=5)
        assert limit.describe() == "10/6s, interval 600ms, burst 5"


@pytest.mark.skipif(not REDIS_URL, reason="set REDIS_TEST_URL to run limiter integration tests")
class TestLimiterAgainstRedis:
    @pytest.fixture
    async def limiter(self):
        from redis.asyncio import Redis

        redis = Redis.from_url(REDIS_URL, decode_responses=True)
        await redis.flushdb()
        yield RateLimiter(redis, namespace=f"test{int(time.time()*1000)}")
        await redis.aclose()

    async def test_burst_is_admitted_then_throttled(self, limiter):
        limit = RateLimit(limit=100, period_ms=60_000, interval_ms=600, burst=5)
        admitted = 0
        for _ in range(8):
            if await limiter.try_acquire("acct:1:content", limit) == 0:
                admitted += 1
        # Exactly `burst` may bunch; the rest must wait.
        assert admitted == 5

    async def test_wait_hint_is_bounded_by_the_interval(self, limiter):
        limit = RateLimit(limit=100, period_ms=60_000, interval_ms=600, burst=1)
        assert await limiter.try_acquire("acct:2:content", limit) == 0
        wait = await limiter.try_acquire("acct:2:content", limit)
        assert 0 < wait <= 600

    async def test_sliding_window_caps_the_longer_period(self, limiter):
        # burst large enough that only the window can be the binding constraint
        limit = RateLimit(limit=3, period_ms=60_000, interval_ms=1, burst=100)
        admitted = sum(
            1 for _ in range(10) if await limiter.try_acquire("acct:3:prices", limit) == 0
        )
        assert admitted == 3

    async def test_accounts_have_independent_budgets(self, limiter):
        limit = RateLimit(limit=2, period_ms=60_000, interval_ms=1, burst=10)
        assert await limiter.try_acquire("acct:A:media", limit) == 0
        assert await limiter.try_acquire("acct:A:media", limit) == 0
        assert await limiter.try_acquire("acct:A:media", limit) > 0
        # A different account must be unaffected -- this is what makes
        # many-accounts-in-parallel work.
        assert await limiter.try_acquire("acct:B:media", limit) == 0

    async def test_categories_have_independent_budgets(self, limiter):
        limit = RateLimit(limit=1, period_ms=60_000, interval_ms=1, burst=1)
        assert await limiter.try_acquire("acct:C:media", limit) == 0
        assert await limiter.try_acquire("acct:C:media", limit) > 0
        # Media exhaustion must not stall price work on the same account.
        assert await limiter.try_acquire("acct:C:prices", limit) == 0

    async def test_concurrent_workers_share_one_budget(self, limiter):
        """The property the whole design rests on: parallel workers against one
        account draw from a single pool rather than each getting their own."""
        import asyncio

        limit = RateLimit(limit=4, period_ms=60_000, interval_ms=1, burst=10)
        results = await asyncio.gather(
            *[limiter.try_acquire("acct:D:content", limit) for _ in range(12)]
        )
        assert sum(1 for r in results if r == 0) == 4
