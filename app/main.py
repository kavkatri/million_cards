from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import structlog
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    issue_session,
    read_session,
    verify_password,
)
from app.db.models import Account, ProductLine, Run, Sku, User
from app.db.session import SessionLocal, get_session

log = structlog.get_logger(__name__)
settings = get_settings()

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))

PUBLIC_PATHS = {"/login", "/healthz", "/static"}


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    Path(settings.media_root).mkdir(parents=True, exist_ok=True)
    log.info("web.started")
    yield
    await app.state.redis.aclose()


app = FastAPI(title="million_cards", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Nothing but the login page is reachable unauthenticated.

    This service holds tokens that can rewrite an entire storefront; an open
    endpoint here is an open endpoint onto the catalogue.
    """
    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in PUBLIC_PATHS):
        return await call_next(request)

    uid = read_session(request)
    if uid is None:
        if path.startswith("/api/"):
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

    async with SessionLocal() as session:
        user = await session.get(User, uid)
        if user is None or not user.is_active:
            return RedirectResponse("/login", status_code=302)
        request.state.user_email = user.email
    return await call_next(request)


app.include_router(api_router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    user = (
        await session.execute(select(User).where(User.email == email.lower().strip()))
    ).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        # Same message either way: distinguishing them tells an attacker which
        # half of the guess was right.
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password"}, status_code=401
        )

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(user.id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    lines = (await session.execute(select(ProductLine).order_by(ProductLine.id))).scalars().all()
    accounts = {
        a.id: a for a in (await session.execute(select(Account))).scalars().all()
    }

    cards = []
    for line in lines:
        total, created, photos = (
            await session.execute(
                select(
                    func.count(Sku.id),
                    func.count(Sku.nm_id),
                    func.coalesce(func.sum(Sku.photo_count), 0),
                ).where(Sku.line_id == line.id)
            )
        ).one()
        expected = line.image_template.expected_photo_count if line.image_template else 0
        cards.append(
            {
                "line": line,
                "account": accounts.get(line.account_id),
                "total": total,
                "created": created,
                "photos": int(photos or 0),
                "photos_target": (created or 0) * expected,
            }
        )

    runs = (await session.execute(select(Run).order_by(Run.id.desc()).limit(10))).scalars().all()
    return templates.TemplateResponse(
        request, "dashboard.html", {"cards": cards, "runs": runs}
    )


@app.get("/lines/{line_id}", response_class=HTMLResponse)
async def line_editor(
    line_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    line = await session.get(ProductLine, line_id)
    if line is None:
        raise HTTPException(404, "line not found")
    return templates.TemplateResponse(request, "line_editor.html", {"line": line})


@app.get("/lines", response_class=HTMLResponse)
async def new_line(request: Request):
    return templates.TemplateResponse(request, "line_editor.html", {"line": None})


@app.get("/templates/editor", response_class=HTMLResponse)
async def template_editor(request: Request):
    return templates.TemplateResponse(request, "template_editor.html", {})
