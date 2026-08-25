"""Documented Wildberries rate limits, transcribed from the OpenAPI spec.

Kept as data rather than sprinkled through the client so the numbers can be
audited against the spec in one place, and so a tier change is a lookup rather
than a code change.

See ``docs/wb-api-notes.md`` for the source tables.
"""

from __future__ import annotations

import enum

from app.db.models import AccountTier
from app.ratelimit.bucket import RateLimit


class WbCategory(str, enum.Enum):
    """Limit buckets. Endpoints in the same bucket share one budget."""

    CONTENT = "content"
    CARDS_UPLOAD = "cards_upload"
    MEDIA = "media"
    PRICES = "prices"
    STOCKS = "stocks"


_MINUTE = 60_000
_HOUR = 3_600_000

# Personal / service / basic-with-secret tokens all share the same numbers.
_STANDARD: dict[WbCategory, RateLimit] = {
    WbCategory.CONTENT: RateLimit(limit=100, period_ms=_MINUTE, interval_ms=600, burst=5),
    # cards/upload is far tighter than the rest of Content: 10/min, 6s apart.
    WbCategory.CARDS_UPLOAD: RateLimit(limit=10, period_ms=_MINUTE, interval_ms=6_000, burst=5),
    WbCategory.MEDIA: RateLimit(limit=100, period_ms=_MINUTE, interval_ms=600, burst=5),
    WbCategory.PRICES: RateLimit(limit=10, period_ms=6_000, interval_ms=600, burst=5),
    # Not given a table in the items spec; deliberately conservative.
    WbCategory.STOCKS: RateLimit(limit=100, period_ms=_MINUTE, interval_ms=600, burst=5),
}

# Plain "basic" tokens are throttled to near-uselessness for bulk work.
_BASIC: dict[WbCategory, RateLimit] = {
    WbCategory.CONTENT: RateLimit(limit=2, period_ms=_HOUR, interval_ms=1_800_000, burst=1),
    WbCategory.CARDS_UPLOAD: RateLimit(limit=10, period_ms=_MINUTE, interval_ms=6_000, burst=5),
    WbCategory.MEDIA: RateLimit(limit=2, period_ms=_HOUR, interval_ms=1_800_000, burst=1),
    WbCategory.PRICES: RateLimit(limit=4, period_ms=_HOUR, interval_ms=900_000, burst=1),
    WbCategory.STOCKS: RateLimit(limit=2, period_ms=_HOUR, interval_ms=1_800_000, burst=1),
}

# The sandbox caps every Content method at 1 req/s combined.
_SANDBOX = RateLimit(limit=1, period_ms=1_000, interval_ms=1_000, burst=1)


def limit_for(category: WbCategory, tier: AccountTier, sandbox: bool = False) -> RateLimit:
    if sandbox:
        return _SANDBOX
    table = _BASIC if tier is AccountTier.BASIC else _STANDARD
    return table[category]


# Endpoint path prefix -> bucket. Longest prefix wins, so the tighter
# cards/upload rule takes precedence over the general content rule.
_ROUTES: list[tuple[str, WbCategory]] = [
    ("/content/v2/cards/upload", WbCategory.CARDS_UPLOAD),
    ("/content/v3/media/", WbCategory.MEDIA),
    ("/content/", WbCategory.CONTENT),
    ("/api/content/", WbCategory.CONTENT),
    ("/api/v2/upload/task", WbCategory.PRICES),
    ("/api/v2/history/", WbCategory.PRICES),
    ("/api/v2/buffer/", WbCategory.PRICES),
    ("/api/v2/list/goods/", WbCategory.PRICES),
    ("/api/v2/quarantine/", WbCategory.PRICES),
    ("/api/discounts-prices/", WbCategory.PRICES),
    ("/api/v3/stocks", WbCategory.STOCKS),
    ("/api/v3/warehouses", WbCategory.STOCKS),
    ("/api/v3/offices", WbCategory.STOCKS),
]


def category_for_path(path: str) -> WbCategory:
    best: tuple[int, WbCategory] = (-1, WbCategory.CONTENT)
    for prefix, cat in _ROUTES:
        if path.startswith(prefix) and len(prefix) > best[0]:
            best = (len(prefix), cat)
    return best[1]
