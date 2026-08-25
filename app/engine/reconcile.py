"""The reconciler: decide what is out of sync, and emit work to fix it.

The whole design turns on one rule: **every decision is derived from observed
marketplace state, never from a record of what we did.** A checkpoint that says
"processed" cannot distinguish "done" from "attempted and silently failed", and
it cannot notice work that was undone elsewhere. A photo count fetched from the
marketplace can do both.

Flow per run:

1. ``ensure_skus``      -- materialise the grid (idempotent)
2. ``sync_catalogue``   -- refresh observed state from the marketplace
3. ``plan_run``         -- diff desired vs observed, emit tasks within quota
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Aspect, ProductLine, Run, Sku, Task
from app.engine.grid import expand
from app.engine.pricing import PriceRuleError, compute_price
from app.marketplace.base import MarketplaceAdapter
from app.marketplace.wb.adapter import CARDS_PER_UPLOAD, PRICES_PER_TASK, STOCKS_PER_REQUEST

log = structlog.get_logger(__name__)
settings = get_settings()


@dataclass
class PlanSummary:
    """What a run intends to do. Also the payload of a dry run."""

    cards_to_create: int = 0
    media_to_upload: int = 0
    prices_to_set: int = 0
    stocks_to_set: int = 0
    tasks_created: int = 0
    quota_remaining: int | None = None
    quota_capped: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "cards_to_create": self.cards_to_create,
            "media_to_upload": self.media_to_upload,
            "prices_to_set": self.prices_to_set,
            "stocks_to_set": self.stocks_to_set,
            "tasks_created": self.tasks_created,
            "quota_remaining": self.quota_remaining,
            "quota_capped": self.quota_capped,
            "notes": self.notes,
        }


async def ensure_skus(session: AsyncSession, line: ProductLine) -> int:
    """Materialise the grid into the ``sku`` table.

    Idempotent: re-running after widening a range adds only the new cells and
    leaves observed state on existing rows untouched.
    """
    desired = expand(line.grid_spec, line.vendor_code_template)
    if not desired:
        return 0

    rows = [
        {"line_id": line.id, "axes": d.axes, "vendor_code": d.vendor_code} for d in desired
    ]
    added = 0
    CHUNK = 5_000
    for i in range(0, len(rows), CHUNK):
        stmt = pg_insert(Sku).values(rows[i : i + CHUNK])
        stmt = stmt.on_conflict_do_nothing(constraint="uq_sku_line_vendor")
        result = await session.execute(stmt)
        added += result.rowcount or 0
    await session.commit()
    log.info("sku.materialised", line=line.id, desired=len(desired), added=added)
    return added


async def sync_catalogue(
    session: AsyncSession, line: ProductLine, adapter: MarketplaceAdapter
) -> int:
    """Pull observed state from the marketplace onto our SKU rows.

    Cards the marketplace has that we do not track are ignored -- a line owns
    only the vendor codes its grid generates.
    """
    now = datetime.now(UTC)
    seen = 0
    batch: list[dict] = []

    async def flush() -> None:
        if not batch:
            return
        for row in batch:
            await session.execute(
                update(Sku)
                .where(Sku.line_id == line.id, Sku.vendor_code == row["vendor_code"])
                .values(
                    nm_id=row["nm_id"],
                    imt_id=row["imt_id"],
                    chrt_id=row["chrt_id"],
                    photo_count=row["photo_count"],
                    last_synced_at=now,
                )
            )
        await session.commit()
        batch.clear()

    suffix = _vendor_suffix(line.vendor_code_template)
    async for card in adapter.fetch_cards(vendor_code_filter=suffix):
        batch.append(
            {
                "vendor_code": card.vendor_code,
                "nm_id": card.nm_id,
                "imt_id": card.imt_id,
                "chrt_id": card.chrt_id,
                "photo_count": card.photo_count,
            }
        )
        seen += 1
        if len(batch) >= 500:
            await flush()
    await flush()

    log.info("catalogue.synced", line=line.id, cards_seen=seen)
    return seen


def _vendor_suffix(template: str) -> str | None:
    """Static tail of the vendor-code template, usable as a cheap server-side filter.

    For ``"{w} x {l} / прям / глян / 0,3"`` this is ``" / прям / глян / 0,3"`` --
    enough to tell one line's cards from another's on the same account.
    """
    tail = template.rsplit("}", 1)[-1]
    return tail or None


async def plan_run(
    session: AsyncSession,
    line: ProductLine,
    run: Run,
    *,
    quota_remaining: int | None = None,
) -> PlanSummary:
    """Diff desired against observed and create tasks. Returns what it planned."""
    summary = PlanSummary(quota_remaining=quota_remaining)
    enabled = set(line.enabled_aspects or [])
    now = datetime.now(UTC)
    grace_cutoff = now - timedelta(seconds=settings.card_sync_grace_seconds)

    tasks: list[Task] = []

    # ---- card aspect: SKUs that do not exist remotely -----------------------
    if Aspect.CARD.value in enabled:
        missing = (
            await session.execute(
                select(Sku).where(Sku.line_id == line.id, Sku.nm_id.is_(None)).order_by(Sku.id)
            )
        ).scalars().all()

        allowed = len(missing)
        if quota_remaining is not None and quota_remaining < allowed:
            allowed = max(0, quota_remaining)
            summary.quota_capped = True
            summary.notes.append(
                f"{len(missing):,} cards missing but only {quota_remaining:,} "
                "creations remain in today's marketplace allowance; "
                f"deferring {len(missing) - allowed:,} to a later run."
            )

        selected = missing[:allowed]
        summary.cards_to_create = len(selected)
        for i in range(0, len(selected), CARDS_PER_UPLOAD):
            chunk = selected[i : i + CARDS_PER_UPLOAD]
            tasks.append(
                Task(
                    run_id=run.id,
                    account_id=line.account_id,
                    aspect=Aspect.CARD,
                    priority=10,
                    payload={"sku_ids": [s.id for s in chunk]},
                )
            )

    # ---- media aspect: cards with fewer photos than the template defines ----
    # This is the check the checkpoint-based design could not express. A card
    # left at 3 of 7 photos by a failed upload reads as "has photos" to the
    # marketplace's own filter, but is caught here.
    if Aspect.MEDIA.value in enabled and line.image_template is not None:
        expected = line.image_template.expected_photo_count
        rows = (
            await session.execute(
                select(Sku).where(
                    Sku.line_id == line.id,
                    Sku.nm_id.is_not(None),
                    Sku.photo_count < expected,
                    _past_grace(grace_cutoff),
                ).order_by(Sku.id)
            )
        ).scalars().all()
        summary.media_to_upload = len(rows)
        for sku in rows:
            tasks.append(
                Task(
                    run_id=run.id,
                    account_id=line.account_id,
                    sku_id=sku.id,
                    aspect=Aspect.MEDIA,
                    priority=50,
                    payload={"expected": expected, "have": sku.photo_count},
                )
            )

    # ---- price aspect ------------------------------------------------------
    if Aspect.PRICE.value in enabled and line.price_rule:
        rows = (
            await session.execute(
                select(Sku).where(
                    Sku.line_id == line.id,
                    Sku.nm_id.is_not(None),
                    _past_grace(grace_cutoff),
                ).order_by(Sku.id)
            )
        ).scalars().all()

        pending: list[tuple[int, int, int]] = []  # (sku_id, price, discount)
        bad = 0
        for sku in rows:
            try:
                price, discount = compute_price(line.price_rule, sku.axes)
            except PriceRuleError:
                bad += 1
                continue
            if sku.price_current is None or int(sku.price_current) != price:
                pending.append((sku.id, price, discount))
        if bad:
            summary.notes.append(f"{bad:,} SKUs skipped: price rule could not be evaluated.")

        summary.prices_to_set = len(pending)
        for i in range(0, len(pending), PRICES_PER_TASK):
            chunk = pending[i : i + PRICES_PER_TASK]
            tasks.append(
                Task(
                    run_id=run.id,
                    account_id=line.account_id,
                    aspect=Aspect.PRICE,
                    priority=60,
                    payload={"items": [{"sku_id": s, "price": p, "discount": d}
                                       for s, p, d in chunk]},
                )
            )

    # ---- stock aspect ------------------------------------------------------
    if Aspect.STOCK.value in enabled and line.stock_rule:
        target = int(line.stock_rule.get("value", 0))
        warehouse_id = line.stock_rule.get("warehouse_id")
        if not warehouse_id:
            summary.notes.append("Stock aspect skipped: no warehouse_id configured.")
        else:
            rows = (
                await session.execute(
                    select(Sku).where(
                        Sku.line_id == line.id,
                        Sku.chrt_id.is_not(None),
                        _past_grace(grace_cutoff),
                    ).order_by(Sku.id)
                )
            ).scalars().all()
            pending_stock = [s for s in rows if s.stock_current != target]
            summary.stocks_to_set = len(pending_stock)
            for i in range(0, len(pending_stock), STOCKS_PER_REQUEST):
                chunk = pending_stock[i : i + STOCKS_PER_REQUEST]
                tasks.append(
                    Task(
                        run_id=run.id,
                        account_id=line.account_id,
                        aspect=Aspect.STOCK,
                        priority=70,
                        payload={
                            "warehouse_id": warehouse_id,
                            "amount": target,
                            "sku_ids": [s.id for s in chunk],
                        },
                    )
                )

    if not run.dry_run and tasks:
        session.add_all(tasks)
        await session.commit()
    summary.tasks_created = 0 if run.dry_run else len(tasks)

    log.info("run.planned", run=run.id, line=line.id, dry_run=run.dry_run, **summary.as_dict())
    return summary


def _past_grace(cutoff: datetime):
    """Cards created moments ago are not yet visible to price/stock endpoints.

    The marketplace documents up to 30 minutes before a new card syncs, during
    which stock and price writes fail. Rather than burn attempts on them, aspects
    other than card creation ignore SKUs inside that window.
    """
    return (Sku.card_created_at.is_(None)) | (Sku.card_created_at < cutoff)
