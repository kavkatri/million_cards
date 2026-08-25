"""The marketplace seam.

Only Wildberries is implemented. The point of this module is that the engine never
imports anything WB-specific: it speaks in ``RemoteCard`` / ``PriceUpdate`` /
``StockUpdate`` and asks the adapter to reconcile them. Adding Ozon later means
writing a second adapter, not touching the reconciler.

Adapters own everything marketplace-shaped: auth, pagination, rate-limit
categories, batch sizes, and the quirks of what "already correct" means. The
engine owns only the diffing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class RemoteCard:
    """A card as the marketplace currently reports it."""

    vendor_code: str
    nm_id: int | None = None
    imt_id: int | None = None
    chrt_id: int | None = None
    photo_count: int = 0
    price: float | None = None
    discount: int | None = None
    stock: int | None = None


@dataclass(slots=True)
class CardDraft:
    """A card we want to exist."""

    vendor_code: str
    subject_id: int
    brand: str
    title: str
    description: str
    dimensions: dict = field(default_factory=dict)
    characteristics: list = field(default_factory=list)
    sizes: list = field(default_factory=list)


@dataclass(slots=True)
class MediaUpload:
    nm_id: int
    photo_number: int
    content: bytes
    filename: str = "image.jpg"


@dataclass(slots=True)
class PriceUpdate:
    nm_id: int
    price: float
    discount: int = 0


@dataclass(slots=True)
class StockUpdate:
    chrt_id: int
    amount: int
    warehouse_id: int


@dataclass(slots=True)
class BatchResult:
    """Outcome of a batched write.

    ``already_correct`` matters: marketplaces reject no-op writes with an error
    status (WB answers 400 "Specified prices and discounts are already set").
    That is success for our purposes and must not inflate the failure count.
    """

    ok: int = 0
    failed: int = 0
    already_correct: int = 0
    errors: list[str] = field(default_factory=list)
    retry_after_s: float | None = None


@dataclass(slots=True)
class CreationQuota:
    """Remaining allowance for creating cards, as the marketplace reports it."""

    free: int
    paid: int

    @property
    def total(self) -> int:
        return self.free + self.paid


class MarketplaceAdapter(Protocol):
    name: str

    async def fetch_cards(self, vendor_code_filter: str | None = None) -> AsyncIterator[RemoteCard]:
        """Stream the whole catalogue.

        Yields rather than returning a list: a catalogue is tens of thousands of
        cards and the caller upserts as it goes.
        """
        ...

    async def creation_quota(self) -> CreationQuota: ...

    async def create_cards(self, drafts: Sequence[CardDraft]) -> BatchResult: ...

    async def failed_creations(self) -> list[tuple[str, str]]:
        """(vendor_code, reason) for cards that silently failed to create."""
        ...

    async def upload_media(self, upload: MediaUpload) -> BatchResult: ...

    async def set_prices(self, updates: Sequence[PriceUpdate]) -> BatchResult: ...

    async def set_stocks(self, updates: Sequence[StockUpdate]) -> BatchResult: ...

    async def close(self) -> None: ...
