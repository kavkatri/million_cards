"""Per-aspect task execution.

Each handler makes one facet of one (or a batch of) SKU(s) correct, then writes
the *observed* result back onto the SKU row so the next reconcile sees reality
rather than an assumption.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Aspect, ProductLine, Run, Sku, Task
from app.engine.media import missing_slots
from app.imaging.render import render
from app.marketplace.base import (
    BatchResult,
    CardDraft,
    MarketplaceAdapter,
    MediaUpload,
    PriceUpdate,
    StockUpdate,
)

log = structlog.get_logger(__name__)


class TaskFailure(Exception):
    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass(slots=True)
class Outcome:
    """What a task actually achieved, in units a person recognises.

    The dashboard says "6 photos" rather than "1 task" because that is what
    someone watching a catalogue fill up wants to know.
    """

    unit: str
    count: int
    label: str | None = None


async def _line_for_task(session: AsyncSession, task: Task) -> ProductLine:
    run = await session.get(Run, task.run_id)
    if run is None:
        raise TaskFailure(f"run {task.run_id} disappeared")
    line = await session.get(ProductLine, run.line_id)
    if line is None:
        raise TaskFailure(f"line {run.line_id} disappeared")
    return line


async def handle(session: AsyncSession, task: Task, adapter: MarketplaceAdapter) -> Outcome:
    line = await _line_for_task(session, task)
    if task.aspect is Aspect.CARD:
        return await _handle_card(session, task, line, adapter)
    if task.aspect is Aspect.MEDIA:
        return await _handle_media(session, task, line, adapter)
    if task.aspect is Aspect.PRICE:
        return await _handle_price(session, task, adapter)
    if task.aspect is Aspect.STOCK:
        return await _handle_stock(session, task, adapter)
    raise TaskFailure(f"unknown aspect {task.aspect}")  # pragma: no cover


# --------------------------------------------------------------------- cards


async def _handle_card(
    session: AsyncSession, task: Task, line: ProductLine, adapter: MarketplaceAdapter
) -> Outcome:
    sku_ids = task.payload.get("sku_ids") or []
    skus = (await session.execute(select(Sku).where(Sku.id.in_(sku_ids)))).scalars().all()
    if not skus:
        return Outcome("cards", 0)

    tpl = line.card_template or {}
    drafts = [
        CardDraft(
            vendor_code=s.vendor_code,
            subject_id=int(tpl.get("subjectID", 0)),
            brand=_fmt(tpl.get("brand", ""), s.axes),
            title=_fmt(tpl.get("title", ""), s.axes),
            description=_fmt(tpl.get("description", ""), s.axes),
            dimensions=_fmt_deep(tpl.get("dimensions", {}), s.axes),
            characteristics=_fmt_deep(tpl.get("characteristics", []), s.axes),
            sizes=_fmt_deep(tpl.get("sizes", []), s.axes),
        )
        for s in skus
    ]

    result = await adapter.create_cards(drafts)
    if result.failed:
        raise TaskFailure("; ".join(result.errors) or "card creation failed", result.retry_after_s)

    # Creation is asynchronous. We deliberately do NOT set nm_id here -- it is
    # not known yet, and guessing would make the next reconcile believe the card
    # exists. Stamping card_created_at is enough: it both records the attempt and
    # holds the SKU out of the price/stock aspects until the sync window passes.
    now = datetime.now(UTC)
    for s in skus:
        s.card_created_at = now
    await session.commit()
    return Outcome("cards", len(skus))


# --------------------------------------------------------------------- media


async def _handle_media(
    session: AsyncSession, task: Task, line: ProductLine, adapter: MarketplaceAdapter
) -> Outcome:
    sku = await session.get(Sku, task.sku_id)
    if sku is None or sku.nm_id is None:
        return Outcome("photos", 0)
    template = line.image_template
    if template is None:
        raise TaskFailure("line has no image template")

    expected = template.expected_photo_count
    have = sku.photo_count or 0
    slots = missing_slots(have, expected)
    if not slots:
        return Outcome("photos", 0, sku.vendor_code)

    uploaded = 0
    for slot in slots:
        if slot == 1:
            content = render(
                template.base_image_path, template.layers or [], sku.axes
            ).content
            filename = "main.jpg"
        else:
            extras = template.extra_photo_paths or []
            idx = slot - 2
            if idx >= len(extras):
                break
            path = Path(extras[idx])
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise TaskFailure(f"extra photo {path} unreadable: {exc}") from exc
            filename = path.name

        result: BatchResult = await adapter.upload_media(
            MediaUpload(nm_id=sku.nm_id, photo_number=slot, content=content, filename=filename)
        )
        if result.failed:
            # Persist what did land, so a retry resumes rather than restarts.
            sku.photo_count = have + uploaded
            await session.commit()
            raise TaskFailure(
                "; ".join(result.errors) or "media upload failed", result.retry_after_s
            )
        uploaded += 1

    sku.photo_count = have + uploaded
    sku.last_synced_at = datetime.now(UTC)
    await session.commit()
    return Outcome("photos", uploaded, sku.vendor_code)


# -------------------------------------------------------------------- prices


async def _handle_price(
    session: AsyncSession, task: Task, adapter: MarketplaceAdapter
) -> Outcome:
    items = task.payload.get("items") or []
    if not items:
        return Outcome("prices", 0)
    sku_ids = [i["sku_id"] for i in items]
    skus = {
        s.id: s
        for s in (await session.execute(select(Sku).where(Sku.id.in_(sku_ids)))).scalars()
    }

    updates, applied = [], []
    for item in items:
        sku = skus.get(item["sku_id"])
        if sku is None or sku.nm_id is None:
            continue
        updates.append(
            PriceUpdate(nm_id=sku.nm_id, price=item["price"], discount=item["discount"])
        )
        applied.append((sku, item))

    if not updates:
        return Outcome("prices", 0)

    result = await adapter.set_prices(updates)
    if result.failed:
        raise TaskFailure("; ".join(result.errors) or "price update failed", result.retry_after_s)

    # `already_correct` is a success: the marketplace rejects no-op price writes
    # with an error status, but the desired state holds either way.
    for sku, item in applied:
        sku.price_current = item["price"]
        sku.discount_current = item["discount"]
    await session.commit()
    return Outcome("prices", len(applied))


# -------------------------------------------------------------------- stocks


async def _handle_stock(
    session: AsyncSession, task: Task, adapter: MarketplaceAdapter
) -> Outcome:
    sku_ids = task.payload.get("sku_ids") or []
    amount = int(task.payload.get("amount", 0))
    warehouse_id = int(task.payload["warehouse_id"])
    skus = (await session.execute(select(Sku).where(Sku.id.in_(sku_ids)))).scalars().all()

    updates = [
        StockUpdate(chrt_id=s.chrt_id, amount=amount, warehouse_id=warehouse_id)
        for s in skus
        if s.chrt_id is not None
    ]
    if not updates:
        return Outcome("stocks", 0)

    result = await adapter.set_stocks(updates)
    if result.failed:
        raise TaskFailure("; ".join(result.errors) or "stock update failed", result.retry_after_s)

    for s in skus:
        if s.chrt_id is not None:
            s.stock_current = amount
    await session.commit()
    return Outcome("stocks", len(updates))


# ------------------------------------------------------------------- helpers


def _fmt(value: str, axes: dict) -> str:
    try:
        return value.format(**axes)
    except (KeyError, IndexError):
        return value


def _fmt_deep(value, axes: dict):
    """Interpolate axis values through nested template structures."""
    if isinstance(value, str):
        return _fmt(value, axes)
    if isinstance(value, list):
        return [_fmt_deep(v, axes) for v in value]
    if isinstance(value, dict):
        return {k: _fmt_deep(v, axes) for k, v in value.items()}
    return value
