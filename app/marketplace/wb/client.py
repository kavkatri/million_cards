"""Rate-limited HTTP client for the Wildberries API.

Every request passes through the shared limiter first, keyed by
``(account, category)``. Because that key is account-scoped and lives in Redis,
any number of workers -- in one process or across many containers -- can hammer
the same account without exceeding its documented budget.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
from typing import Any

import httpx
import structlog

from app.db.models import AccountTier
from app.marketplace.wb.limits import WbCategory, category_for_path, limit_for
from app.ratelimit.bucket import RateLimiter

log = structlog.get_logger(__name__)

PROD_HOSTS = {
    "content": "https://content-api.wildberries.ru",
    "prices": "https://discounts-prices-api.wildberries.ru",
    "marketplace": "https://marketplace-api.wildberries.ru",
}
SANDBOX_HOSTS = {
    "content": "https://content-api-sandbox.wildberries.ru",
    "prices": "https://discounts-prices-api-sandbox.wildberries.ru",
    "marketplace": "https://marketplace-api-sandbox.wildberries.ru",
}


def host_group_for_path(path: str) -> str:
    if path.startswith(("/api/v2/", "/api/discounts-prices/")):
        return "prices"
    if path.startswith("/api/v3/"):
        return "marketplace"
    return "content"


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Read the (unverified) payload of a WB token.

    Used only to display which seller account a stored token belongs to. The
    signature is not checked -- we are not authenticating anyone, just labelling.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


class WbApiError(RuntimeError):
    def __init__(self, status: int, body: str, path: str) -> None:
        super().__init__(f"{path} -> HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body
        self.path = path


class WbClient:
    def __init__(
        self,
        token: str,
        limiter: RateLimiter,
        account_key: str,
        tier: AccountTier = AccountTier.PERSONAL,
        sandbox: bool = False,
        timeout: float = 60.0,
        max_retries: int = 4,
    ) -> None:
        self._token = token
        self._limiter = limiter
        self._account_key = account_key
        self._tier = tier
        self._sandbox = sandbox
        self._max_retries = max_retries
        self._hosts = SANDBOX_HOSTS if sandbox else PROD_HOSTS
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": token},
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _limit_key(self, category: WbCategory) -> str:
        return f"wb:{self._account_key}:{category.value}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict | None = None,
        files: dict | None = None,
        data: dict | None = None,
        headers: dict[str, str] | None = None,
        expected_ok: tuple[int, ...] = (200,),
    ) -> tuple[int, Any]:
        """Perform one rate-limited request, retrying transient failures.

        Returns ``(status, parsed_body)``. Statuses in ``expected_ok`` and any 4xx
        that the caller may want to interpret (e.g. the "already set" 400) are
        returned rather than raised; 5xx and 429 are retried with backoff.
        """
        category = category_for_path(path)
        limit = limit_for(category, self._tier, self._sandbox)
        url = self._hosts[host_group_for_path(path)] + path

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire(self._limit_key(category), limit)
            try:
                resp = await self._client.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    files=files,
                    data=data,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                await self._backoff(attempt, reason=type(exc).__name__, path=path)
                continue

            if resp.status_code == 429:
                # We were admitted locally but the server disagreed. Honour
                # Retry-After when present; otherwise back off and let the local
                # bucket drain. Persistent 429s mean our table is out of date.
                retry_after = _parse_retry_after(resp)
                log.warning(
                    "wb.429", path=path, attempt=attempt, retry_after=retry_after,
                    account=self._account_key,
                )
                await asyncio.sleep(retry_after if retry_after else _backoff_delay(attempt))
                continue

            if resp.status_code >= 500:
                last_exc = WbApiError(resp.status_code, resp.text, path)
                await self._backoff(attempt, reason=f"http{resp.status_code}", path=path)
                continue

            return resp.status_code, _parse(resp)

        if last_exc:
            raise last_exc
        raise WbApiError(429, "exhausted retries against rate limit", path)

    async def _backoff(self, attempt: int, *, reason: str, path: str) -> None:
        delay = _backoff_delay(attempt)
        log.warning("wb.retry", path=path, attempt=attempt, reason=reason, delay=delay)
        await asyncio.sleep(delay)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter, capped at 60s."""
    return min(60.0, (2**attempt)) * (0.5 + random.random() / 2)


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After") or resp.headers.get("X-Ratelimit-Retry")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text
