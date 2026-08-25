"""Boot the real application and exercise it end to end.

Everything else in the suite tests pure logic. This file is the answer to
"has it ever actually run?" -- it creates the schema from the real models,
starts the real ASGI app with its real middleware, and drives it over HTTP.

SQLite and a fake Redis stand in for Postgres and a real broker. That covers
the request path, auth, templates, serialisation, and the credential
round-trip. It deliberately does NOT cover the parts that are Postgres-specific
(``FOR UPDATE SKIP LOCKED`` claims, ``ON CONFLICT`` upserts) or that require the
marketplace -- those need the integration environment described in DEPLOY.md.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("aiosqlite", reason="integration tests need aiosqlite")
fakeredis = pytest.importorskip("fakeredis", reason="integration tests need fakeredis")


@pytest.fixture(scope="module")
def client():
    import app.main as main
    from app.core.security import hash_password
    from app.db.base import Base
    from app.db.models import User
    from app.db.session import SessionLocal, get_engine

    async def setup():
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as s:
            s.add(
                User(
                    email="tester@example.com",
                    password_hash=hash_password("correct-horse-battery"),
                    is_admin=True,
                )
            )
            await s.commit()

    asyncio.run(setup())

    # The app opens Redis in its lifespan; swap in an in-process fake.
    main.Redis = fakeredis.aioredis.FakeRedis
    with TestClient(main.app) as c:
        yield c


class TestSchema:
    def test_models_create_cleanly(self, client):
        """If the declarative models cannot build a schema, the initial
        migration -- which is `Base.metadata.create_all` -- cannot either."""
        from app.db.base import Base

        names = set(Base.metadata.tables)
        assert {
            "user", "account", "product_line", "sku", "run", "task",
            "image_template", "quota_usage", "audit_log",
        } <= names


class TestAuthGate:
    def test_health_is_public(self, client):
        assert client.get("/healthz").json() == {"ok": True}

    def test_dashboard_redirects_when_signed_out(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_api_returns_401_not_a_redirect(self, client):
        """An API client following a 302 to an HTML login page would parse
        garbage; it needs a status it can act on."""
        assert client.get("/api/lines").status_code == 401

    def test_login_page_renders(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        assert "MillionCards" in r.text

    def test_wrong_password_is_rejected(self, client):
        r = client.post(
            "/login",
            data={"email": "tester@example.com", "password": "wrong"},
            follow_redirects=False,
        )
        assert r.status_code == 401
        assert "mc_session" not in r.cookies

    def test_login_succeeds_and_sets_session(self, client):
        r = client.post(
            "/login",
            data={"email": "tester@example.com", "password": "correct-horse-battery"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert client.cookies.get("mc_session")


class TestSignedInPages:
    """Runs after TestAuthGate has signed the shared client in."""

    @pytest.mark.parametrize(
        "path,needle",
        [
            ("/", "Обзор"),
            ("/lines", "Сетка размеров"),
            ("/templates/editor", "Предпросмотр"),
            ("/accounts", "Аккаунты"),
        ],
    )
    def test_pages_render(self, client, path, needle):
        r = client.get(path)
        assert r.status_code == 200, r.text[:300]
        assert needle in r.text
        assert "MillionCards" in r.text

    def test_static_css_is_served(self, client):
        r = client.get("/static/app.css")
        assert r.status_code == 200
        assert "--color-accent" in r.text or "@import" in r.text


class TestValidationEndpoints:
    def test_grid_is_costed(self, client):
        r = client.post(
            "/api/validate/grid",
            json={
                "grid_spec": {
                    "axes": [
                        {"name": "w", "type": "range", "start": 10, "stop": 120},
                        {"name": "l", "type": "range", "start": 10, "stop": 380},
                    ]
                },
                "vendor_code_template": "{w} x {l} / прям / глян / 0,3",
            },
        )
        assert r.status_code == 200
        assert r.json()["cells"] == 41_181

    def test_bad_grid_is_422_with_a_reason(self, client):
        r = client.post(
            "/api/validate/grid",
            json={
                "grid_spec": {"axes": [{"name": "w", "type": "range", "start": 1, "stop": 3}]},
                "vendor_code_template": "плёнка",
            },
        )
        assert r.status_code == 422
        assert "duplicate" in r.json()["detail"]

    def test_price_preview(self, client):
        r = client.post(
            "/api/validate/price",
            json={
                "price_rule": {"type": "formula", "expr": "w * l * 2", "min_price": 50},
                "axes": {"w": 30, "l": 40},
            },
        )
        assert r.json()["price"] == 2400

    def test_injection_in_a_price_formula_is_refused(self, client):
        r = client.post(
            "/api/validate/price",
            json={"price_rule": {"expr": "__import__('os').system('id')"}, "axes": {}},
        )
        assert r.status_code == 422


class TestCredentials:
    # A real WB-shaped JWT payload: {"oid": 4379287, "sid": "...", "uid": 1}
    FAKE_TOKEN = (
        "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJvaWQiOjQzNzkyODcsInNpZCI6InRlc3Qtc2lkIiwidWlkIjoxfQ."
        "c2lnbmF0dXJl"
    )

    def test_account_round_trip(self, client):
        created = client.post(
            "/api/accounts",
            json={"name": "Песочница", "token": self.FAKE_TOKEN, "sandbox": True},
        )
        assert created.status_code == 201, created.text
        assert created.json()["external_id"] == "4379287"

        listed = client.get("/api/accounts").json()
        row = next(a for a in listed if a["name"] == "Песочница")

        # The token must never come back out of the API in any form.
        assert "token" not in row
        assert self.FAKE_TOKEN not in listed.__str__()
        assert len(row["token_fingerprint"]) == 12

    def test_stored_token_decrypts_to_the_original(self, client):
        """Encryption is only useful if it round-trips; a token that cannot be
        decrypted is a line that silently stops working."""
        from sqlalchemy import select

        from app.core.crypto import decrypt_token
        from app.db.models import Account
        from app.db.session import SessionLocal

        async def read():
            async with SessionLocal() as s:
                acct = (
                    await s.execute(select(Account).where(Account.name == "Песочница"))
                ).scalar_one()
                assert acct.token_encrypted != self.FAKE_TOKEN  # actually encrypted
                return decrypt_token(acct.token_encrypted)

        assert asyncio.run(read()) == self.FAKE_TOKEN

    def test_line_can_be_created_against_the_account(self, client):
        account_id = client.get("/api/accounts").json()[0]["id"]
        r = client.post(
            "/api/lines",
            json={
                "account_id": account_id,
                "name": "Плёнка 0,3",
                "grid_spec": {
                    "axes": [{"name": "w", "type": "range", "start": 10, "stop": 12}]
                },
                "vendor_code_template": "{w} / 0,3",
                "price_rule": {"type": "constant", "value": 500},
                "stock_rule": {"type": "constant", "value": 1000},
            },
        )
        assert r.status_code == 201, r.text
        assert client.get("/api/lines").json()[0]["name"] == "Плёнка 0,3"

    def test_dashboard_shows_the_new_line(self, client):
        assert "Плёнка 0,3" in client.get("/").text
