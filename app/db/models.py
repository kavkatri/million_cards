"""Schema.

The shape follows the domain: an *account* owns *product lines*; a line expands a
grid spec into *SKUs*; reconciling a line produces a *run* containing *tasks*, one
per (SKU, aspect) that is out of sync.

The important property is that no table records "what we did today". Every decision
is derived from observed marketplace state stored on `Sku`. That is deliberate --
the checkpoint-driven design it replaces silently redid completed work and silently
skipped incomplete work.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class Aspect(str, enum.Enum):
    """The four independently-reconcilable facets of a SKU."""

    CARD = "card"
    MEDIA = "media"
    PRICE = "price"
    STOCK = "stock"


class TaskState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunState(str, enum.Enum):
    PLANNING = "planning"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AccountTier(str, enum.Enum):
    """Determines which documented rate-limit column applies."""

    PERSONAL = "personal"
    SERVICE = "service"
    BASIC_SECRET = "basic_secret"
    BASIC = "basic"


class User(Base, TimestampMixin):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Account(Base, TimestampMixin):
    """One seller account on one marketplace.

    Rate limits and card-creation quota are per account, so this is the unit the
    limiter and the quota ledger key on.
    """

    __tablename__ = "account"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    marketplace: Mapped[str] = mapped_column(String(32), default="wb", nullable=False)
    tier: Mapped[AccountTier] = mapped_column(
        SAEnum(AccountTier, native_enum=False), default=AccountTier.PERSONAL, nullable=False
    )
    sandbox: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    token_fingerprint: Mapped[str] = mapped_column(String(32), nullable=False)
    # Parsed out of the WB JWT for display; not used for auth.
    external_id: Mapped[str | None] = mapped_column(String(64))

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    lines: Mapped[list[ProductLine]] = relationship(back_populates="account")


class ImageTemplate(Base, TimestampMixin):
    """A base image plus positioned layers, rendered per SKU.

    `layers` holds text/overlay definitions with coordinates as 0..1 fractions of
    the canvas, so the browser editor and the server renderer agree regardless of
    the base image's pixel size.
    """

    __tablename__ = "image_template"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_image_path: Mapped[str] = mapped_column(String(512), nullable=False)
    layers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Static photos appended after the generated main image, in order.
    extra_photo_paths: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    @property
    def expected_photo_count(self) -> int:
        """Generated main image plus every static extra."""
        return 1 + len(self.extra_photo_paths or [])


class ProductLine(Base, TimestampMixin):
    """A generated product family: a grid spec plus how to render each cell."""

    __tablename__ = "product_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)

    # {"axes": [{"name": "w", "type": "range", "start": 10, "stop": 120, "step": 1}, ...]}
    grid_spec: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Format string over axis names, e.g. "{w} x {l} / прям / глян / 0,3"
    vendor_code_template: Mapped[str] = mapped_column(String(512), nullable=False)
    # subjectID, brand, title, description, dimensions, characteristics
    card_template: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    image_template_id: Mapped[int | None] = mapped_column(ForeignKey("image_template.id"))
    # {"type": "formula", "expr": "w * l * ratio", "vars": {...}, "discount": 0}
    price_rule: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # {"type": "constant", "value": 1000, "warehouse_id": 1234}
    stock_rule: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    enabled_aspects: Mapped[list] = mapped_column(
        JSON, default=lambda: [a.value for a in Aspect], nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    schedule_cron: Mapped[str | None] = mapped_column(String(64))

    account: Mapped[Account] = relationship(back_populates="lines")
    image_template: Mapped[ImageTemplate | None] = relationship()

    __table_args__ = (UniqueConstraint("account_id", "name", name="uq_line_account_name"),)


class Sku(Base, TimestampMixin):
    """One grid cell, and the last observed marketplace state for it.

    This row is the single source of truth the reconciler diffs against. It is
    refreshed from the marketplace, never from a record of our own actions.
    """

    __tablename__ = "sku"

    id: Mapped[int] = mapped_column(primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("product_line.id"), nullable=False)
    # Axis values for this cell, e.g. {"w": 27, "l": 107}
    axes: Mapped[dict] = mapped_column(JSON, nullable=False)
    vendor_code: Mapped[str] = mapped_column(String(512), nullable=False)

    # --- observed marketplace state ---
    nm_id: Mapped[int | None] = mapped_column(BigInteger)
    imt_id: Mapped[int | None] = mapped_column(BigInteger)
    chrt_id: Mapped[int | None] = mapped_column(BigInteger)
    photo_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    price_current: Mapped[float | None] = mapped_column(Numeric(12, 2))
    discount_current: Mapped[int | None] = mapped_column(Integer)
    stock_current: Mapped[int | None] = mapped_column(Integer)

    card_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("line_id", "vendor_code", name="uq_sku_line_vendor"),
        Index("ix_sku_line_nm", "line_id", "nm_id"),
    )


class Run(Base, TimestampMixin):
    """One reconciliation pass over a line."""

    __tablename__ = "run"

    id: Mapped[int] = mapped_column(primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("product_line.id"), nullable=False)
    state: Mapped[RunState] = mapped_column(
        SAEnum(RunState, native_enum=False), default=RunState.PLANNING, nullable=False
    )
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Per-aspect planned/done/failed counters, plus the plan summary.
    stats: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_run_line_state", "line_id", "state"),)


class Task(Base, TimestampMixin):
    """A single unit of work: make one aspect of one SKU correct.

    Claimed with SELECT ... FOR UPDATE SKIP LOCKED, so any number of workers may
    drain the queue concurrently -- including several against the same account.
    """

    __tablename__ = "task"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), nullable=False)
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("sku.id"))
    aspect: Mapped[Aspect] = mapped_column(SAEnum(Aspect, native_enum=False), nullable=False)

    state: Mapped[TaskState] = mapped_column(
        SAEnum(TaskState, native_enum=False), default=TaskState.PENDING, nullable=False
    )
    # Batched aspects (card creation is 100/request) carry their members here.
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_task_claim", "state", "available_at", "priority"),
        Index("ix_task_run", "run_id", "state"),
    )


class QuotaUsage(Base, TimestampMixin):
    """Daily consumption of a countable, account-scoped allowance.

    Card creation is the one that matters: the marketplace grants a per-day
    allowance per account, and every line on that account draws from the same
    pool. Tracking it here is what stops two lines from each assuming the whole
    budget is theirs.
    """

    __tablename__ = "quota_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    limit_observed: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("account_id", "day", "kind", name="uq_quota_account_day_kind"),
    )


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
