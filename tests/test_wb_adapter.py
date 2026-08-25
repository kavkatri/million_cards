"""Adapter behaviour against recorded marketplace responses."""

import pytest

from app.marketplace.base import PriceUpdate
from app.marketplace.wb.adapter import WbAdapter


class FakeClient:
    """Stands in for WbClient, returning canned (status, body) pairs."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self._responses.pop(0)

    async def close(self):
        pass


def page(cards, total):
    return 200, {
        "cards": cards,
        "cursor": {"total": total, "updatedAt": "2026-08-25T00:00:00Z", "nmID": 1},
    }


class TestFetchCards:
    async def test_missing_photos_key_counts_as_zero(self):
        """WB omits `photos` entirely for cards with none -- it is not an empty
        list. Reading it with a plain `len(card["photos"])` would raise, and
        `card.get("photos")` returns None, so the `or []` is load-bearing."""
        adapter = WbAdapter(
            FakeClient([page([{"vendorCode": "a", "nmID": 1, "sizes": []}], 1)])
        )
        cards = [c async for c in adapter.fetch_cards()]
        assert cards[0].photo_count == 0

    async def test_partial_and_complete_photo_counts(self):
        adapter = WbAdapter(
            FakeClient(
                [
                    page(
                        [
                            {"vendorCode": "a", "nmID": 1, "photos": [{}, {}, {}], "sizes": []},
                            {"vendorCode": "b", "nmID": 2, "photos": [{}] * 6, "sizes": []},
                        ],
                        2,
                    )
                ]
            )
        )
        counts = {c.vendor_code: c.photo_count async for c in adapter.fetch_cards()}
        assert counts == {"a": 3, "b": 6}

    async def test_always_requests_every_card_not_a_photo_filter(self):
        """withPhoto must stay -1. The value 0 has meant "any card" since
        16 June, and 2 returns only zero-photo cards, which would permanently
        exclude part-way ones."""
        client = FakeClient([page([], 0)])
        adapter = WbAdapter(client)
        [c async for c in adapter.fetch_cards()]
        body = client.calls[0][2]["json_body"]
        assert body["settings"]["filter"]["withPhoto"] == -1

    async def test_pagination_stops_on_short_page(self):
        client = FakeClient(
            [
                page([{"vendorCode": f"v{i}", "nmID": i, "sizes": []} for i in range(100)], 100),
                page([{"vendorCode": "last", "nmID": 999, "sizes": []}], 1),
            ]
        )
        adapter = WbAdapter(client)
        cards = [c async for c in adapter.fetch_cards()]
        assert len(cards) == 101
        assert len(client.calls) == 2

    async def test_vendor_code_filter_selects_only_this_line(self):
        adapter = WbAdapter(
            FakeClient(
                [
                    page(
                        [
                            {"vendorCode": "1 x 2 / глян / 0,3", "nmID": 1, "sizes": []},
                            {"vendorCode": "1 x 2 / глян / 0,7", "nmID": 2, "sizes": []},
                        ],
                        2,
                    )
                ]
            )
        )
        cards = [c async for c in adapter.fetch_cards(vendor_code_filter=" / глян / 0,3")]
        assert [c.nm_id for c in cards] == [1]

    async def test_chrt_id_taken_from_first_size(self):
        adapter = WbAdapter(
            FakeClient([page([{"vendorCode": "a", "nmID": 1, "sizes": [{"chrtID": 555}]}], 1)])
        )
        cards = [c async for c in adapter.fetch_cards()]
        assert cards[0].chrt_id == 555


class TestQuota:
    async def test_reads_free_and_paid_limits(self):
        adapter = WbAdapter(
            FakeClient([(200, {"data": {"freeLimits": 1500, "paidLimits": 10}, "error": False})])
        )
        quota = await adapter.creation_quota()
        assert (quota.free, quota.paid, quota.total) == (1500, 10, 1510)


class TestPrices:
    async def test_already_set_is_success_not_failure(self):
        """WB rejects a no-op price write with a 400. The catalogue is in the
        desired state either way, so this must not be counted as a failure --
        otherwise a steady-state run reports thousands of errors."""
        adapter = WbAdapter(
            FakeClient(
                [(400, {"error": True,
                        "errorText": "Specified prices and discounts are already set"})]
            )
        )
        result = await adapter.set_prices([PriceUpdate(nm_id=1, price=100)])
        assert result.failed == 0
        assert result.already_correct == 1

    async def test_real_error_is_a_failure(self):
        adapter = WbAdapter(FakeClient([(400, {"error": True, "errorText": "nmID not found"})]))
        result = await adapter.set_prices([PriceUpdate(nm_id=1, price=100)])
        assert result.failed == 1
        assert result.ok == 0

    async def test_success(self):
        adapter = WbAdapter(FakeClient([(200, {"error": False})]))
        result = await adapter.set_prices([PriceUpdate(nm_id=1, price=100)])
        assert result.ok == 1


class TestCards:
    async def test_http_200_with_error_true_is_a_failure(self):
        """A 200 from WB does not mean the write succeeded."""
        adapter = WbAdapter(FakeClient([(200, {"error": True, "errorText": "bad subject"})]))
        from app.marketplace.base import CardDraft

        draft = CardDraft(
            vendor_code="a", subject_id=1, brand="b", title="t", description="d"
        )
        result = await adapter.create_cards([draft])
        assert result.failed == 1 and result.ok == 0

    async def test_batch_larger_than_cap_is_refused(self):
        from app.marketplace.base import CardDraft

        adapter = WbAdapter(FakeClient([]))
        drafts = [
            CardDraft(vendor_code=str(i), subject_id=1, brand="", title="", description="")
            for i in range(101)
        ]
        with pytest.raises(ValueError, match="exceeds WB cap"):
            await adapter.create_cards(drafts)
