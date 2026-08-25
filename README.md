# million_cards

Catalogue reconciliation for generated product grids on Wildberries.

A product line is a **grid** — film in 111 widths × 371 lengths is 41 181 cards —
and keeping that grid correct means continuously answering four questions: does
each card exist, does it have its photos, is its price right, is its stock set.
This service answers them by comparing the marketplace against a declarative
definition and fixing the difference.

## The one idea

**Every decision is derived from observed marketplace state, never from a record
of what we did.**

That sounds obvious and is easy to get wrong. A checkpoint saying "processed"
cannot tell *done* from *attempted and silently failed*, and it cannot notice
work undone elsewhere. A photo count fetched from the marketplace does both.

The system this replaces kept a per-day JSON checkpoint. In production that meant:

| Symptom | Cause |
|---|---|
| ~288 000 redundant photo uploads a night | Checkpoint reset at midnight, so every card was re-uploaded |
| Runtime grew to 38 h and kept climbing | Redundant work scaled with the catalogue |
| Six copies racing on one account | Runs outlived their schedule; each night's cron added another |
| Progress silently lost | Concurrent copies overwrote each other's checkpoint |
| 27 cards stuck part-photographed | Nothing ever asked how many photos a card actually had |

Every one of those is a category of bug this design cannot express.

## How it works

```
grid spec ──expand──> desired SKUs ─┐
                                    ├─ diff ──> tasks ──> workers ──> marketplace
marketplace ──sync──> observed state ┘                                     │
        ▲                                                                  │
        └──────────────── observed state written back ─────────────────────┘
```

1. **`ensure_skus`** expands the grid into rows (idempotent).
2. **`sync_catalogue`** refreshes observed state from the marketplace.
3. **`plan_run`** diffs desired against observed per aspect and emits tasks,
   bounded by the account's *real* remaining quota.
4. **Workers** execute tasks and write the observed result back.

Aspects are independent: `card`, `media`, `price`, `stock`.

## Parallelism

Two things must be true at once: many accounts working simultaneously, and many
workers on a *single* account.

Marketplace limits are per seller account, per category — so a private budget per
worker cannot work. Instead every worker asks a **shared Redis token bucket**,
keyed `(account, category)`, immediately before each call. Throughput is bounded
by the limiter, not by worker count, so `--scale worker=8` increases concurrency
during waits and cannot breach a documented rate.

Tasks are claimed from Postgres with `FOR UPDATE SKIP LOCKED`, so workers never
block one another. Each bucket enforces both documented constraints — the
burst/interval token bucket *and* the limit/period sliding window — in one Lua
script.

## The interface

A workspace, not a dashboard-shaped marketing page. Soft peach surface, Lora
wordmark, a persistent left rail, and one deliberate piece of motion.

**Progress is live.** Workers publish one event per completed task to Redis
pub/sub; the web process relays them over Server-Sent Events. When a batch of
cards is created or a card's photos finish uploading, the counter advances, the
meter moves, and the panel that changed **pulses green** — once, in place. Not a
toast in the corner: the confirmation happens on the thing that changed, so you
can look away and still catch it in peripheral vision.

Pages: **Обзор** (per-line progress + live feed) · **Линейки** (the no-code
builder) · **Шаблоны** (image template editor with live preview) ·
**Аккаунты** (credentials).

The design system is locked in [`design.md`](design.md) — colours, type, motion,
and the rules every page shares. Change it there, not per page.

### Credentials

API tokens are encrypted at rest with Fernet and **never rendered back**. The
accounts page shows a 12-character fingerprint so you can tell which token is
stored without ever putting one on screen. `TOKEN_ENCRYPTION_KEY` must be backed
up alongside the database — lose it and every stored token becomes unreadable.

## Running it

```bash
cp .env.example .env      # then fill in the three required secrets
docker compose up -d --build
docker compose exec web python -m app.cli create-user you@example.com
docker compose exec web python -m app.cli add-account "Плёнка 0,3" --sandbox
```

Open `http://127.0.0.1:8080`. Scale workers with:

```bash
docker compose up -d --scale worker=4
```

### Serving it over a link

Compose binds the web port to `127.0.0.1` deliberately — this service holds
credentials that can rewrite a whole storefront, so it should never face the
internet directly. Put TLS in front of it. Two things the proxy must get right
or the live progress will appear broken:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
location /events/stream {
    proxy_pass http://127.0.0.1:8080;
    proxy_buffering off;        # otherwise frames are held until a buffer fills
    proxy_read_timeout 3600s;   # the stream is long-lived by design
}
```

### Start in the sandbox

Accounts carry a `sandbox` flag that switches every host to
`*-sandbox.wildberries.ru`. Build and verify a line there before pointing it at a
real account — a misconfigured grid writes thousands of cards.

### Preview before writing

Every line has a dry run: it syncs and plans, reports exactly what it *would* do,
and writes nothing.

## Building a line

No code required.

- **Grid** — add axes (integer ranges or value lists). Bounds are inclusive, so
  "10 to 120" is 111 values. The cell count is shown as you type, and a guard
  refuses an accidental multi-million-cell expansion.
- **Vendor code** — a template over axis names, `{w} x {l} / прям / глян / 0,3`.
  A template that omits an axis is rejected, because two cells sharing a code
  would make the line unreconcilable.
- **Price** — an arithmetic formula over axes and named variables, previewed
  against a sample cell. Expressions are parsed and walked against an allow-list,
  never `eval`-ed: this field is typed into a browser and executed by a worker
  holding catalogue-wide credentials.
- **Images** — a base image plus positioned text layers, previewed live.
  Coordinates are fractions of the canvas, so the editor and the renderer agree
  regardless of the original's pixel size.
- **Schedule** — cron. If the previous run is still going the next is skipped,
  and the skip is recorded.

## Layout

```
app/
  marketplace/     adapter seam; wb/ is the only implementation
  ratelimit/       shared token bucket (Redis + Lua)
  engine/          grid, pricing, media, reconcile, runs
  worker/          queue (Postgres SKIP LOCKED), handlers, runner, scheduler
  imaging/         template renderer
  api/ web/        JSON API and the builder UI
docs/
  wb-api-notes.md  marketplace facts the design depends on
```

`docs/wb-api-notes.md` is worth reading before changing the adapter — it records
the rate-limit tables, the asynchronous-creation window, and the `withPhoto`
enum trap.

## Tests

```bash
pytest
```

63 tests, no database or network required. They cover the parts where being wrong
is expensive: grid cardinality (pinned to the real 41 181), the price expression
sandbox, rate-limit routing, photo-slot arithmetic, and adapter handling of
marketplace responses that report failure inside a `200`.

## What's next

[`docs/plan-variation-factory.md`](docs/plan-variation-factory.md) — proposed,
not built. Turning one real product into twenty styled listings, each with its
own generated name, description, and images, aimed at a different buyer. The
key structural claim there: it is a content-generation stage in front of this
reconciler, not a second system.

## Status

See [`DECISIONS.md`](DECISIONS.md) for what is implemented, the assumptions made
where questions were still open, and what remains.
