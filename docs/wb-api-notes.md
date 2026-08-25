# Wildberries API — facts that shape this design

Extracted from the official `Работа с товарами` OpenAPI spec (`items`, OpenAPI 3.0.1).
Everything here is load-bearing: each item below is encoded somewhere in the engine.

## Hosts

| Environment | Host |
|---|---|
| Production | `https://content-api.wildberries.ru` |
| Sandbox | `https://content-api-sandbox.wildberries.ru` |

Sandbox caps **all** Content methods at 1 request/second combined. Cards are created
immediately there (no async wait), and quarantined goods auto-clear after 3 days.

## Rate limits

Limits are **per seller account**, per category — not per token and not per process.
Two processes sharing one account share one budget. This is why the limiter is a
shared Redis token bucket keyed by `(account, category)` rather than per-worker state.

Personal / service / basic-with-secret tokens (the normal case):

| Category | Period | Limit | Interval | Burst |
|---|---|---|---|---|
| Content — `get/cards/list`, `cards/limits`, `media/file`, `media/save`, tags, directories | 1 min | 100 | 600 ms | 5 |
| `cards/upload` | 1 min | 10 | 6 s | 5 |
| `cards/upload/add` | 1 min | 10 | 6 s | 5 |
| `cards/recover` | 1 min | 3 | 20 s | 5 |
| Prices & discounts — `upload/task`, `history/tasks`, `buffer/tasks`, `list/goods/*` | 6 s | 10 | 600 ms | 5 |
| `brands` | 1 min | 1 | 1 min | 5 |

Plain "basic" tokens are drastically lower (media: 2/hour; prices: 4/hour). The
account record carries a `tier` so the limiter picks the right numbers.

`interval` is minimum spacing between requests; `burst` is how many may be bunched.
That maps exactly onto a token bucket of capacity `burst` refilling at `1/interval`,
combined with a sliding-window cap of `limit` per `period`. Both must pass.

## Card creation

- Max **100 separate cards per request**, or 100 groups of ≤30 joined cards.
- Max request size **10 MB**.
- **Creation is asynchronous. Sync can take up to 30 minutes.** During that window
  you cannot add stock or set prices for the new card.
- A `200` response does **not** mean every card was created — unlisted failures must
  be collected from `POST /content/v2/cards/error/list`.

The 30-minute window is why the engine gates media/price/stock aspects behind a
grace period after card creation, and why `cards/error/list` is polled rather than
assuming success.

## Card creation quota

`GET /content/v2/cards/limits` returns the seller's actual remaining allowance:

```json
{"data": {"freeLimits": 1500, "paidLimits": 10}, "error": false}
```

Documented tiers: minimum 1000, standard 2000, maximum 5000. Query this endpoint —
never hardcode a daily limit, and never discover the ceiling by crashing into an error.

## Media

- Max **30 images per card**; one video.
- Min resolution **700×900 px**, max size 32 MB, min quality 65%.
- Formats: JPG, PNG, BMP, GIF (static), WebP.
- `POST /content/v3/media/file` adds a single file to a card.
- `POST /content/v3/media/save` **replaces all media** — to append you must resend
  the existing links alongside the new ones.

### Detecting incomplete media

`get/cards/list` returns a `photos` array per card, and **omits the key entirely**
for cards with none. So the correct completeness test is:

```python
len(card.get("photos") or []) < expected_photo_count
```

The `filter.withPhoto` enum is a trap:

| Value | Meaning |
|---|---|
| `-1` | any card |
| `0` | *was* "without photo"; **since 16 June means any card** |
| `1` | only cards with photos |
| `2` | only cards with **no** photos |

`2` looks like the right filter for "needs images" but silently excludes
**partially uploaded** cards — one that got 3 of 7 photos before a 429 reads as
"has photos" and would never be repaired. The engine therefore fetches with `-1`
and compares counts locally.

## Prices

- `POST /api/v2/upload/task` submits price changes as an async task; poll
  `GET /api/v2/history/tasks` for the outcome.
- Setting a price that is already current returns
  `400 "Specified prices and discounts are already set"` — harmless, and it makes
  the price aspect naturally idempotent, but it should not be counted as an error.
- **Quarantine:** if a discounted price is ≥3× lower than the old price the item is
  quarantined and keeps selling at the old price. Check
  `GET /api/v2/quarantine/goods`. Size-level pricing is exempt.

## Stocks

`PUT/DELETE /api/v3/stocks/{warehouseId}` — seller-warehouse model (FBS). Requires a
warehouse id from `GET /api/v3/warehouses`, and operates on `chrtID` (size), not `nmID`.
