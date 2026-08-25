"""First-run admin bootstrap.

Creating the first user with a CLI assumes you can get a shell inside the
container. Plenty of managed platforms do not offer one, which left a
successfully deployed app with no way to log into it.

So the web process can create the first account from environment variables
instead. It fires **only when the user table is empty**, so it cannot overwrite
an existing account, cannot reset a password, and quietly does nothing on every
subsequent boot.
"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import User
from app.db.session import SessionLocal

log = structlog.get_logger(__name__)

MIN_PASSWORD_LENGTH = 10


async def bootstrap_admin() -> None:
    settings = get_settings()
    email = (settings.bootstrap_admin_email or "").strip().lower()
    password = settings.bootstrap_admin_password or ""

    if not email or not password:
        return

    if len(password) < MIN_PASSWORD_LENGTH:
        # Refuse rather than create a weak admin on an internet-facing service.
        log.error(
            "bootstrap.rejected",
            reason=f"BOOTSTRAP_ADMIN_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters",
        )
        return

    async with SessionLocal() as session:
        existing = (await session.execute(select(func.count(User.id)))).scalar_one()
        if existing:
            log.info("bootstrap.skipped", reason="a user already exists", users=existing)
            return

        session.add(
            User(email=email, password_hash=hash_password(password), is_admin=True)
        )
        await session.commit()

    log.warning(
        "bootstrap.admin_created",
        email=email,
        next_step=(
            "sign in, change the password at /password, then delete "
            "BOOTSTRAP_ADMIN_PASSWORD from the environment"
        ),
    )
