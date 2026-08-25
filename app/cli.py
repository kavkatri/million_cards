"""Small admin CLI.

    python -m app.cli create-user you@example.com
    python -m app.cli add-account "Плёнка 0,3" --sandbox
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.core.crypto import encrypt_token, fingerprint
from app.core.security import hash_password
from app.db.models import Account, AccountTier, User
from app.db.session import SessionLocal
from app.marketplace.wb.client import decode_jwt_claims


async def create_user(email: str, password: str | None) -> None:
    password = password or getpass.getpass("Password: ")
    if len(password) < 10:
        sys.exit("Password must be at least 10 characters.")
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.email == email.lower()))
        ).scalar_one_or_none()
        if existing:
            sys.exit(f"User {email} already exists.")
        session.add(
            User(email=email.lower(), password_hash=hash_password(password), is_admin=True)
        )
        await session.commit()
    print(f"Created user {email}")


async def add_account(name: str, tier: str, sandbox: bool) -> None:
    # Read from a prompt rather than argv so the token does not land in shell
    # history or the process list.
    token = getpass.getpass("API token: ").strip()
    if not token:
        sys.exit("No token given.")
    claims = decode_jwt_claims(token)
    async with SessionLocal() as session:
        account = Account(
            name=name,
            tier=AccountTier(tier),
            sandbox=sandbox,
            token_encrypted=encrypt_token(token),
            token_fingerprint=fingerprint(token),
            external_id=str(claims.get("oid") or "") or None,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
    print(f"Added account #{account.id} {name} (oid={account.external_id})")


def main() -> None:
    parser = argparse.ArgumentParser(prog="million_cards")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_user = sub.add_parser("create-user")
    p_user.add_argument("email")
    p_user.add_argument("--password", default=None)

    p_acct = sub.add_parser("add-account")
    p_acct.add_argument("name")
    p_acct.add_argument("--tier", default="personal", choices=[t.value for t in AccountTier])
    p_acct.add_argument("--sandbox", action="store_true")

    args = parser.parse_args()
    if args.cmd == "create-user":
        asyncio.run(create_user(args.email, args.password))
    elif args.cmd == "add-account":
        asyncio.run(add_account(args.name, args.tier, args.sandbox))


if __name__ == "__main__":
    main()
