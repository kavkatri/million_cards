"""Wildberries implementation of :class:`MarketplaceAdapter`.

Two WB behaviours drive most of the care in here:

1. **A 200 does not mean success.** WB signals logical failures with
   ``{"error": true, "errorText": ...}`` inside a 200 body, so every response is
   inspected rather than trusted by status code.
2. **"Already correct" is reported as an error.** Re-sending an unchanged price
   returns 400 "Specified prices and discounts are already set". That is the
   desired end state, so it is counted separately and never as a failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import structlog

from app.marketplace.base import (
    BatchResult,
    CardDraft,
    CreationQuota,
    MediaUpload,
    PriceUpdate,
    RemoteCard,
    StockUpdate,
)
from app.marketplace.wb.client import WbClient

log = structlog.get_logger(__name__)

CARDS_PER_UPLOAD = 100  # WB hard cap per request
PRICES_PER_TASK = 100
STOCKS_PER_REQUEST = 1000

_ALREADY_SET_MARKERS = (
    "already set",
    "уже установлен",
)


def _is_already_correct(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _ALREADY_SET_MARKERS)


def _body_error(body) -> str | None:
    """Return the error text if the body signals failure, else None."""
    if isinstance(body, dict):
        if body.get("error"):
            return str(body.get("errorText") or body.get("additionalErrors") or body)
        return None
    return None


class WbAdapter:
    name = "wb"

    def __init__(self, client: WbClient) -> None:
        self._client = client

    async def close(self) -> None:
        await self._client.close()

    # ------------------------------------------------------------------ read

    async def fetch_cards(
        self, vendor_code_filter: str | None = None
    ) -> AsyncIterator[RemoteCard]:
        """Stream the catalogue.

        ``withPhoto`` is pinned to -1 (every card) on purpose. The value that
        looks right for "needs images" is 2, but that returns only cards with
        *zero* photos and would permanently exclude cards left part-way through
        by a failed upload. Photo completeness is therefore judged here, from the
        length of the ``photos`` array -- which WB omits entirely when empty.
        """
        cursor: dict = {"limit": 100}
        while True:
            body = {
                "settings": {
                    "sort": {"ascending": True},
                    "cursor": cursor,
                    "filter": {"withPhoto": -1},
                }
            }
            status, data = await self._client.request(
                "POST", "/content/v2/get/cards/list", json_body=body, params={"locale": "ru"}
            )
            if status != 200 or not isinstance(data, dict):
                raise RuntimeError(f"cards/list returned {status}: {str(data)[:300]}")

            cards = data.get("cards") or []
            for c in cards:
                vc = c.get("vendorCode", "")
                if vendor_code_filter and not vc.endswith(vendor_code_filter):
                    continue
                sizes = c.get("sizes") or []
                yield RemoteCard(
                    vendor_code=vc,
                    nm_id=c.get("nmID"),
                    imt_id=c.get("imtID"),
                    chrt_id=(sizes[0].get("chrtID") if sizes else None),
                    photo_count=len(c.get("photos") or []),
                )

            next_cursor = data.get("cursor") or {}
            total = next_cursor.get("total", 0)
            # WB reports the size of *this page*; a short page means the end.
            if total < cursor["limit"]:
                return
            cursor = {
                "limit": cursor["limit"],
                "updatedAt": next_cursor.get("updatedAt"),
                "nmID": next_cursor.get("nmID"),
            }

    async def creation_quota(self) -> CreationQuota:
        """Ask WB how many cards may still be created.

        Replaces hardcoding a daily limit and learning the real ceiling by
        crashing into an error partway through a batch.
        """
        status, data = await self._client.request("GET", "/content/v2/cards/limits")
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"cards/limits returned {status}: {str(data)[:300]}")
        payload = data.get("data") or {}
        return CreationQuota(
            free=int(payload.get("freeLimits") or 0),
            paid=int(payload.get("paidLimits") or 0),
        )

    async def failed_creations(self) -> list[tuple[str, str]]:
        """Cards WB accepted with a 200 but then failed to create."""
        status, data = await self._client.request(
            "POST", "/content/v2/cards/error/list", json_body={}, params={"locale": "ru"}
        )
        if status != 200 or not isinstance(data, dict):
            return []
        out = []
        for item in data.get("data") or []:
            out.append((item.get("vendorCode", ""), "; ".join(item.get("errors") or [])))
        return out

    # ----------------------------------------------------------------- write

    async def create_cards(self, drafts: Sequence[CardDraft]) -> BatchResult:
        if len(drafts) > CARDS_PER_UPLOAD:
            raise ValueError(f"batch of {len(drafts)} exceeds WB cap of {CARDS_PER_UPLOAD}")

        payload = [
            {
                "subjectID": d.subject_id,
                "variants": [
                    {
                        "vendorCode": d.vendor_code,
                        "title": d.title,
                        "description": d.description,
                        "brand": d.brand,
                        "dimensions": d.dimensions,
                        "characteristics": d.characteristics,
                        "sizes": d.sizes,
                    }
                ],
            }
            for d in drafts
        ]

        status, data = await self._client.request(
            "POST", "/content/v2/cards/upload", json_body=payload
        )
        if status == 200:
            err = _body_error(data)
            if err is None:
                return BatchResult(ok=len(drafts))
            if _is_quota_exhausted(err):
                return BatchResult(failed=len(drafts), errors=[err], retry_after_s=3600)
            return BatchResult(failed=len(drafts), errors=[err])
        return BatchResult(failed=len(drafts), errors=[f"HTTP {status}: {str(data)[:300]}"])

    async def upload_media(self, upload: MediaUpload) -> BatchResult:
        # This endpoint takes the target card and photo slot as headers, not body.
        status, data = await self._client.request(
            "POST",
            "/content/v3/media/file",
            files={"uploadfile": (upload.filename, upload.content, "image/jpeg")},
            headers={
                "X-Nm-Id": str(upload.nm_id),
                "X-Photo-Number": str(upload.photo_number),
            },
        )
        if status == 200:
            err = _body_error(data)
            if err is None:
                return BatchResult(ok=1)
            if "лимит" in err.lower() or "limit" in err.lower():
                return BatchResult(failed=1, errors=[err], retry_after_s=3600)
            return BatchResult(failed=1, errors=[err])
        return BatchResult(failed=1, errors=[f"HTTP {status}: {str(data)[:300]}"])

    async def set_prices(self, updates: Sequence[PriceUpdate]) -> BatchResult:
        if not updates:
            return BatchResult()
        payload = {
            "data": [
                {"nmID": u.nm_id, "price": int(round(u.price)), "discount": u.discount}
                for u in updates
            ]
        }
        status, data = await self._client.request(
            "POST", "/api/v2/upload/task", json_body=payload
        )
        if status == 200 and _body_error(data) is None:
            return BatchResult(ok=len(updates))

        text = str(data)
        if _is_already_correct(text):
            # Not a failure: the catalogue is already in the desired state.
            return BatchResult(already_correct=len(updates))
        return BatchResult(failed=len(updates), errors=[f"HTTP {status}: {text[:300]}"])

    async def set_stocks(self, updates: Sequence[StockUpdate]) -> BatchResult:
        if not updates:
            return BatchResult()
        warehouse_id = updates[0].warehouse_id
        payload = {"stocks": [{"chrtId": u.chrt_id, "amount": u.amount} for u in updates]}
        status, data = await self._client.request(
            "PUT", f"/api/v3/stocks/{warehouse_id}", json_body=payload, expected_ok=(204, 200)
        )
        if status in (200, 204):
            return BatchResult(ok=len(updates))
        return BatchResult(failed=len(updates), errors=[f"HTTP {status}: {str(data)[:300]}"])


def _is_quota_exhausted(text: str) -> bool:
    low = (text or "").lower()
    return "лимит" in low or "limit" in low
