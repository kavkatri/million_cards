from datetime import UTC, datetime

import pytest

from app.db.models import AccountTier
from app.marketplace.wb.limits import WbCategory, category_for_path, limit_for
from app.worker.scheduler import cron_matches


class TestLimitRouting:
    """cards/upload is far tighter than the rest of Content (10/min vs 100/min),
    so routing it to the wrong bucket would let the system issue ten times the
    permitted card-creation rate."""

    def test_cards_upload_gets_its_own_tighter_bucket(self):
        assert category_for_path("/content/v2/cards/upload") is WbCategory.CARDS_UPLOAD
        assert category_for_path("/content/v2/cards/upload/add") is WbCategory.CARDS_UPLOAD

    def test_longest_prefix_wins_over_general_content(self):
        assert category_for_path("/content/v2/get/cards/list") is WbCategory.CONTENT
        assert category_for_path("/content/v2/cards/limits") is WbCategory.CONTENT

    def test_media_and_prices_route_correctly(self):
        assert category_for_path("/content/v3/media/file") is WbCategory.MEDIA
        assert category_for_path("/content/v3/media/save") is WbCategory.MEDIA
        assert category_for_path("/api/v2/upload/task") is WbCategory.PRICES
        assert category_for_path("/api/v2/history/tasks") is WbCategory.PRICES
        assert category_for_path("/api/v3/stocks/123") is WbCategory.STOCKS

    def test_documented_numbers(self):
        content = limit_for(WbCategory.CONTENT, AccountTier.PERSONAL)
        assert (content.limit, content.period_ms, content.interval_ms, content.burst) == (
            100, 60_000, 600, 5,
        )
        upload = limit_for(WbCategory.CARDS_UPLOAD, AccountTier.PERSONAL)
        assert (upload.limit, upload.interval_ms) == (10, 6_000)
        prices = limit_for(WbCategory.PRICES, AccountTier.PERSONAL)
        assert (prices.limit, prices.period_ms) == (10, 6_000)

    def test_basic_tier_is_much_stricter(self):
        basic = limit_for(WbCategory.MEDIA, AccountTier.BASIC)
        assert basic.limit == 2 and basic.period_ms == 3_600_000

    def test_sandbox_overrides_everything(self):
        sb = limit_for(WbCategory.MEDIA, AccountTier.PERSONAL, sandbox=True)
        assert sb.limit == 1 and sb.period_ms == 1_000


class TestCron:
    def test_daily_at_three(self):
        expr = "0 3 * * *"
        assert cron_matches(expr, datetime(2026, 8, 25, 3, 0, tzinfo=UTC))
        assert not cron_matches(expr, datetime(2026, 8, 25, 3, 1, tzinfo=UTC))
        assert not cron_matches(expr, datetime(2026, 8, 25, 4, 0, tzinfo=UTC))

    def test_step_and_list_and_range(self):
        assert cron_matches("*/15 * * * *", datetime(2026, 8, 25, 1, 30, tzinfo=UTC))
        assert not cron_matches("*/15 * * * *", datetime(2026, 8, 25, 1, 31, tzinfo=UTC))
        assert cron_matches("0 3,15 * * *", datetime(2026, 8, 25, 15, 0, tzinfo=UTC))
        assert cron_matches("0 9-17 * * *", datetime(2026, 8, 25, 12, 0, tzinfo=UTC))
        assert not cron_matches("0 9-17 * * *", datetime(2026, 8, 25, 18, 0, tzinfo=UTC))

    def test_weekday_sunday_is_zero(self):
        sunday = datetime(2026, 8, 30, 6, 0, tzinfo=UTC)
        assert sunday.isoweekday() == 7
        assert cron_matches("0 6 * * 0", sunday)

    def test_bad_expression_raises(self):
        with pytest.raises(ValueError, match="5 fields"):
            cron_matches("0 3 * *", datetime.now(UTC))
