# Deployment readiness

**Short answer: not yet.** The code is structurally complete and 88 tests pass,
but the system has never run as a system — no Docker build, no Postgres, no
Redis, no marketplace write. This file lists exactly what has been verified,
what has not, and the shortest path to a first deployment.

## Verified

| | How |
|---|---|
| Pure logic — grid, pricing sandbox, rate-limit routing, photo slots | 88 unit tests |
| The app **boots** and serves | Integration test: real ASGI app, real middleware, driven over HTTP |
| Schema builds from the models | `Base.metadata.create_all` in the integration fixture — the same call the initial migration makes |
| Auth gate | Signed-out redirects, API returns 401 not a 302, wrong password rejected, session issued |
| Every page renders signed-in | Dashboard, line builder, template editor, accounts |
| Credential round-trip | Token encrypts, decrypts back to the original, and never appears in any API response |
| Adapter behaviour against recorded responses | 200-with-`error:true`, "already set" 400, absent `photos` key, pagination |
| No secrets in the repo | Scanned before each commit |

## Not verified — do these before trusting it

1. **The Docker image has never been built.** No Docker on the machine this was
   written on. `docker compose build` is step one, and it is where a missing
   system library or a bad layer will surface.
2. **Never run against Postgres.** The integration test uses SQLite, which does
   not exercise the two Postgres-specific pieces the engine depends on:
   `SELECT … FOR UPDATE SKIP LOCKED` (task claiming) and
   `INSERT … ON CONFLICT DO NOTHING` (SKU materialisation). Both are standard,
   but "standard" is not "observed working".
3. **The rate limiter's Lua script has never executed.** Six tests cover it and
   all six skip without a broker. Run them:
   ```bash
   docker compose up -d redis
   REDIS_TEST_URL=redis://localhost:6379/15 pytest tests/test_ratelimit.py -v
   ```
   This is the highest-value single check in the list: the limiter is what
   stands between many workers and a rate-limit ban.
4. **No marketplace call has gone through the adapter.** The API shapes were
   read from the spec and from a known-working script, and read-only probes
   confirmed the response format — but no write has been made, and no call has
   been made *through this code*.
5. **SSE has never carried a real event.** The wiring is in place end to end;
   nothing has travelled it.
6. **The image renderer has never rendered.** Pillow is installed and the code
   is straightforward, but no template has been composed.
7. **No load at real scale.** A line is 41 181 SKUs. `plan_run` loads matching
   SKUs into memory as ORM objects to diff them — fine at 41k, worth watching as
   lines multiply.

## First deployment, in order

Each step is cheap and fails loudly. Do not skip to 5.

```bash
# 1. build — the first thing that has never happened
docker compose build

# 2. infra only, then prove the limiter
docker compose up -d db redis
REDIS_TEST_URL=redis://localhost:6379/15 pytest tests/test_ratelimit.py -v

# 3. migrate against a real Postgres
docker compose run --rm web alembic upgrade head

# 4. bring it up, create a user
docker compose up -d
docker compose exec web python -m app.cli create-user you@example.com
curl -fsS http://127.0.0.1:8080/healthz
```

5. **Add a sandbox account** (`--sandbox`, or tick the box in the UI) and build
   one small line — a 3×3 grid, not the 41 181-cell one. Dry-run it, read the
   plan, then run it for real. Watch the dashboard: cards should appear, the
   meter should move, the panel should pulse.
6. Only once a sandbox line has completed a full cycle — card → media → price →
   stock — point a line at a production account. Start with an aspect subset if
   you want to be careful: enable `card` only, confirm, then add the rest.

## Deploying on a managed platform

Coolify, Dokploy, Portainer and friends build this compose file directly. Three
things differ from a local run.

**1. The three required variables must be set in the platform's environment
settings, not a `.env` file.** Compose refuses to interpolate without them and
the deploy fails before a container starts:

```
error while interpolating services.web.environment.SECRET_KEY:
required variable SECRET_KEY is missing a value
```

That is the guard working. Generate the values and paste them in:

| Variable | Generate with |
|---|---|
| `POSTGRES_PASSWORD` | `python -c "import secrets; print(secrets.token_urlsafe(24))"` |
| `SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `TOKEN_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

`TOKEN_ENCRYPTION_KEY` must be **backed up with the database**. It is a Fernet
key (44 characters, base64, ends in `=`); changing it makes every stored
marketplace token unreadable.

**2. Ingress — the setting that produces a blank page when it is wrong.**

The container listens on **8000** (`EXPOSE 8000`), and compose publishes it on
the host as **8080**. Point the platform's router at whichever it asks for.

The published port binds to `0.0.0.0` by default. It used to bind to
`127.0.0.1`, which is safer but only works when the router runs on the container
host's own loopback — on a platform whose router sits elsewhere the port
answered nobody, the site was blank, and **no request ever appeared in the
uvicorn log**. That log is the diagnostic: uvicorn prints every request it
receives, so if loading the page adds no line, nothing is reaching the app and
the problem is routing, not the application.

Set `WEB_BIND=127.0.0.1` for a local run or a host where you terminate TLS
yourself. On a managed platform, leave it and let the platform's firewall keep
port 8080 off the public internet.

**2b. Health-check path — set it to `/healthz`.**

Platforms default to probing `/`. That returns **302** here, redirecting to the
login page, and a router that expects 200 can treat the app as unhealthy and
refuse to route to it — which also shows up as a blank page.

`/healthz` is public, unauthenticated, and returns `200 {"ok": true}`. On
Timeweb App Platform this is «путь проверки состояния» in the app settings.

**3. SSE needs an unbuffered proxy.** Whatever sits in front must not buffer
`/events/stream` and must allow a long-lived connection, or live progress will
appear frozen. Most platforms let you set per-service proxy options; the nginx
equivalents are in the README.

**Migrations run on start.** The web service runs `alembic upgrade head` before
uvicorn, so a fresh deploy migrates itself. If the web container restarts in a
loop, read its logs first — that command is where a database problem surfaces.

**4. Creating the first login without a shell.** `python -m app.cli create-user`
assumes you can exec into the container, and many platforms do not offer that.
Set two more environment variables instead:

```
BOOTSTRAP_ADMIN_EMAIL=you@example.com
BOOTSTRAP_ADMIN_PASSWORD=<at least 10 characters>
```

On the next start the web process creates that account — **only while the user
table is empty**. Once any user exists it is ignored on every boot, so it cannot
overwrite an account or reset a password, and it is safe to leave configured.
Watch for this line in the logs:

```json
{"event": "bootstrap.admin_created", "email": "you@example.com", ...}
```

Then sign in, change the password at **/password**, and delete
`BOOTSTRAP_ADMIN_PASSWORD` from the platform — otherwise your admin password
sits in the dashboard for anyone with access to read.

## Before it faces the internet

- TLS in front; the compose port is bound to `127.0.0.1` on purpose. See the
  nginx snippet in the README — `proxy_buffering off` on `/events/stream` or the
  live progress will look dead.
- `SECRET_KEY` and `TOKEN_ENCRYPTION_KEY` generated fresh, and
  `TOKEN_ENCRYPTION_KEY` **backed up with the database**. Lose it and every
  stored marketplace token becomes unreadable.
- A backup of the `pgdata` volume. The SKU table is the reconciler's memory of
  observed state; losing it means a full catalogue re-sync, not data loss, but
  the run history and credentials go with it.
- Log shipping, or at least `docker compose logs -f`. The logs are structured
  JSON precisely so they can be shipped somewhere greppable.

## Sizing

Postgres + Redis + web + N workers. Two vCPU and 4 GB is comfortable; one vCPU
and 2 GB will run but image generation is CPU-bound and will contend. Disk is
dominated by the media volume — base images and per-line extras, not the
database.

`WORKER_CONCURRENCY` is safe to raise: the per-account token buckets, not the
worker count, bound the marketplace request rate. Raising it buys more in-flight
work while workers wait on tokens, and cannot cause a breach.

## Known gaps, carried from DECISIONS.md

Not blockers, but things a first deployment will notice:

- `cards/error/list` is implemented and tested but not yet polled, so cards that
  fail *after* a 200 are not surfaced.
- Price submission is asynchronous on WB; `history/tasks` is not yet polled, so
  a submitted price is assumed applied.
- No notifications. Failures are visible in the UI and the logs, and nowhere else.
