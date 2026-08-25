"""Build a marketplace adapter for an account."""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.crypto import decrypt_token
from app.db.models import Account
from app.marketplace.base import MarketplaceAdapter
from app.marketplace.wb.adapter import WbAdapter
from app.marketplace.wb.client import WbClient
from app.ratelimit.bucket import RateLimiter


def build_adapter(account: Account, redis: Redis) -> MarketplaceAdapter:
    if account.marketplace != "wb":
        raise ValueError(
            f"no adapter for marketplace {account.marketplace!r}. "
            "Implement MarketplaceAdapter and register it here."
        )
    client = WbClient(
        token=decrypt_token(account.token_encrypted),
        limiter=RateLimiter(redis),
        # The limiter key is the account, not the worker: every process touching
        # this account draws from one shared budget.
        account_key=str(account.id),
        tier=account.tier,
        sandbox=account.sandbox,
    )
    return WbAdapter(client)
