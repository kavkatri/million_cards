"""JSON API.

Everything the builder UI needs, including the live-validation endpoints that
make a no-code editor safe: a grid spec is costed before it is saved, a price
formula is evaluated against a sample cell, and an image template is rendered to
a preview -- all without writing anything to the marketplace.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import encrypt_token, fingerprint
from app.db.models import (
    Account,
    AccountTier,
    Aspect,
    AuditLog,
    ImageTemplate,
    ProductLine,
    Run,
    Sku,
    Task,
    TaskState,
)
from app.db.session import get_session
from app.engine.grid import GridSpecError
from app.engine.grid import validate as validate_grid
from app.engine.pricing import PriceRuleError, compute_price
from app.engine.runs import RunAlreadyActive, start_run
from app.imaging.render import TemplateRenderError, preview
from app.marketplace.wb.client import decode_jwt_claims

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api")
settings = get_settings()


async def get_redis(request: Request) -> Redis:
    return request.app.state.redis


def _actor(request: Request) -> str:
    return getattr(request.state, "user_email", "system")


async def _audit(session: AsyncSession, request: Request, action: str, target: str, detail: dict):
    session.add(AuditLog(actor=_actor(request), action=action, target=target, detail=detail))
    await session.commit()


# ------------------------------------------------------------------ schemas


class AccountIn(BaseModel):
    name: str
    token: str
    tier: AccountTier = AccountTier.PERSONAL
    sandbox: bool = False


class GridValidateIn(BaseModel):
    grid_spec: dict
    vendor_code_template: str


class PricePreviewIn(BaseModel):
    price_rule: dict
    axes: dict[str, Any]


class ImagePreviewIn(BaseModel):
    base_image_path: str
    layers: list[dict] = Field(default_factory=list)
    values: dict[str, Any] = Field(default_factory=dict)


class LineIn(BaseModel):
    account_id: int
    name: str
    grid_spec: dict
    vendor_code_template: str
    card_template: dict = Field(default_factory=dict)
    image_template_id: int | None = None
    price_rule: dict = Field(default_factory=dict)
    stock_rule: dict = Field(default_factory=dict)
    enabled_aspects: list[str] = Field(default_factory=lambda: [a.value for a in Aspect])
    schedule_cron: str | None = None
    enabled: bool = True


# ----------------------------------------------------------------- accounts


@router.get("/accounts")
async def list_accounts(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Account).order_by(Account.id))).scalars().all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "marketplace": a.marketplace,
            "tier": a.tier.value,
            "sandbox": a.sandbox,
            "external_id": a.external_id,
            "token_fingerprint": a.token_fingerprint,
            "enabled": a.enabled,
        }
        for a in rows
    ]


@router.post("/accounts", status_code=201)
async def create_account(
    body: AccountIn, request: Request, session: AsyncSession = Depends(get_session)
):
    claims = decode_jwt_claims(body.token)
    account = Account(
        name=body.name,
        tier=body.tier,
        sandbox=body.sandbox,
        token_encrypted=encrypt_token(body.token),
        token_fingerprint=fingerprint(body.token),
        external_id=str(claims.get("oid") or "") or None,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    # The token itself is never echoed back, logged, or written to the audit trail.
    await _audit(session, request, "account.create", str(account.id), {"name": account.name})
    return {"id": account.id, "external_id": account.external_id}


# ------------------------------------------------------------- live validation


@router.post("/validate/grid")
async def api_validate_grid(body: GridValidateIn):
    """Cost a grid before it is saved, so nobody creates 4 million cells by accident."""
    try:
        return validate_grid(body.grid_spec, body.vendor_code_template)
    except GridSpecError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/validate/price")
async def api_validate_price(body: PricePreviewIn):
    """Evaluate a price rule against one cell.

    The expression is parsed and walked against an allow-list, never ``eval``-ed.
    """
    try:
        price, discount = compute_price(body.price_rule, body.axes)
    except PriceRuleError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"price": price, "discount": discount}


@router.post("/validate/image")
async def api_validate_image(body: ImagePreviewIn):
    from fastapi.responses import Response

    try:
        data = preview(body.base_image_path, body.layers, body.values)
    except TemplateRenderError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(content=data, media_type="image/jpeg")


@router.post("/upload/asset")
async def upload_asset(file: UploadFile):
    """Store a base image, extra photo, or font for use in a template."""
    root = Path(settings.media_root) / "assets"
    root.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "asset").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".ttf", ".otf"}:
        raise HTTPException(415, f"unsupported asset type {suffix!r}")
    name = f"{uuid.uuid4().hex}{suffix}"
    target = root / name
    target.write_bytes(await file.read())
    return {"path": str(target), "name": file.filename}


# ------------------------------------------------------------ image templates


@router.get("/templates")
async def list_templates(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(ImageTemplate).order_by(ImageTemplate.id))).scalars()
    return [
        {
            "id": t.id,
            "name": t.name,
            "base_image_path": t.base_image_path,
            "layers": t.layers,
            "extra_photo_paths": t.extra_photo_paths,
            "expected_photo_count": t.expected_photo_count,
        }
        for t in rows
    ]


@router.post("/templates", status_code=201)
async def create_template(body: dict, session: AsyncSession = Depends(get_session)):
    tpl = ImageTemplate(
        name=body["name"],
        base_image_path=body["base_image_path"],
        layers=body.get("layers") or [],
        extra_photo_paths=body.get("extra_photo_paths") or [],
    )
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)
    return {"id": tpl.id, "expected_photo_count": tpl.expected_photo_count}


@router.put("/templates/{template_id}")
async def update_template(
    template_id: int, body: dict, session: AsyncSession = Depends(get_session)
):
    tpl = await session.get(ImageTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, "template not found")
    for field in ("name", "base_image_path", "layers", "extra_photo_paths"):
        if field in body:
            setattr(tpl, field, body[field])
    await session.commit()
    return {"id": tpl.id, "expected_photo_count": tpl.expected_photo_count}


# -------------------------------------------------------------------- lines


@router.get("/lines")
async def list_lines(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(ProductLine).order_by(ProductLine.id))).scalars().all()
    out = []
    for line in rows:
        totals = (
            await session.execute(
                select(
                    func.count(Sku.id),
                    func.count(Sku.nm_id),
                    func.coalesce(func.sum(Sku.photo_count), 0),
                ).where(Sku.line_id == line.id)
            )
        ).one()
        out.append(
            {
                "id": line.id,
                "name": line.name,
                "account_id": line.account_id,
                "enabled": line.enabled,
                "schedule_cron": line.schedule_cron,
                "enabled_aspects": line.enabled_aspects,
                "sku_total": totals[0],
                "sku_created": totals[1],
                "photos_total": int(totals[2] or 0),
            }
        )
    return out


@router.post("/lines", status_code=201)
async def create_line(
    body: LineIn, request: Request, session: AsyncSession = Depends(get_session)
):
    try:
        validate_grid(body.grid_spec, body.vendor_code_template)
    except GridSpecError as exc:
        raise HTTPException(422, str(exc)) from exc

    line = ProductLine(**body.model_dump())
    session.add(line)
    await session.commit()
    await session.refresh(line)
    await _audit(session, request, "line.create", str(line.id), {"name": line.name})
    return {"id": line.id}


@router.put("/lines/{line_id}")
async def update_line(
    line_id: int, body: LineIn, request: Request, session: AsyncSession = Depends(get_session)
):
    line = await session.get(ProductLine, line_id)
    if line is None:
        raise HTTPException(404, "line not found")
    try:
        validate_grid(body.grid_spec, body.vendor_code_template)
    except GridSpecError as exc:
        raise HTTPException(422, str(exc)) from exc
    for k, v in body.model_dump().items():
        setattr(line, k, v)
    await session.commit()
    await _audit(session, request, "line.update", str(line.id), {"name": line.name})
    return {"id": line.id}


# --------------------------------------------------------------------- runs


@router.post("/lines/{line_id}/run")
async def trigger_run(
    line_id: int,
    request: Request,
    dry_run: bool = False,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    line = await session.get(ProductLine, line_id)
    if line is None:
        raise HTTPException(404, "line not found")
    try:
        run, summary = await start_run(
            session, line, redis, dry_run=dry_run, triggered_by=_actor(request)
        )
    except RunAlreadyActive as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"run_id": run.id, "dry_run": dry_run, "plan": summary.as_dict()}


@router.get("/runs")
async def list_runs(line_id: int | None = None, session: AsyncSession = Depends(get_session)):
    stmt = select(Run).order_by(Run.id.desc()).limit(50)
    if line_id:
        stmt = stmt.where(Run.line_id == line_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "line_id": r.line_id,
            "state": r.state.value,
            "dry_run": r.dry_run,
            "triggered_by": r.triggered_by,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "stats": r.stats,
            "message": r.message,
        }
        for r in rows
    ]


@router.get("/runs/{run_id}/tasks")
async def run_tasks(run_id: int, session: AsyncSession = Depends(get_session)):
    counts = dict(
        (
            await session.execute(
                select(Task.state, func.count()).where(Task.run_id == run_id).group_by(Task.state)
            )
        ).all()
    )
    failures = (
        await session.execute(
            select(Task)
            .where(Task.run_id == run_id, Task.state == TaskState.FAILED)
            .limit(50)
        )
    ).scalars().all()
    return {
        "counts": {k.value if hasattr(k, "value") else str(k): v for k, v in counts.items()},
        "failures": [
            {
                "id": t.id,
                "aspect": t.aspect.value,
                "sku_id": t.sku_id,
                "attempts": t.attempts,
                "error": t.last_error,
            }
            for t in failures
        ],
    }
