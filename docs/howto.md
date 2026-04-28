# How-to Guide

## Prerequisites

- [Colima](https://github.com/abiosoft/colima) (or Docker Desktop)
- `uv` installed
- A Discord webhook URL (Server Settings → Integrations → Webhooks → New Webhook)

---

## 1. Start services

```bash
colima start
docker compose up -d
```

Verify both are healthy:

```bash
docker compose ps
docker compose exec redis redis-cli ping   # should print PONG
```

---

## 2. Apply the database schema

Only needed on first run or after pulling new migrations:

```bash
uv run alembic upgrade head
```

Verify tables exist:

```bash
docker compose exec postgres psql -U atbatwatch -c '\dt'
# should list: users, players, follows, notification_log, alembic_version
```

---

## 3. Seed your user and follows

**Preferred:** Use the web UI or API directly (see step 6 below).

**CLI alternative** (if you skipped the UI setup):

```bash
uv run atbatwatch user create \
  --email you@example.com \
  --discord-webhook https://discord.com/api/webhooks/...

uv run atbatwatch follow \
  --email you@example.com \
  --player "Shohei Ohtani"
```

Add more `follow` calls for any other players you want to track.

---

## 4. Check today's games

```bash
uv run atbatwatch games
```

Games marked `[Live]` are active right now. `[Preview]` means starting soon.

---

## 5. Run the watcher

For quick local testing, `run-all` runs all three workers as concurrent asyncio tasks in one process:

```bash
uv run atbatwatch run-all
```

1. **Poller** — hits the MLB API every 10s, detects when batter/on-deck changes, writes to the `events:transitions` Redis stream
2. **Fanout worker** — reads from `events:transitions`, looks up followers in Postgres, writes one job per follower to `events:deliveries`
3. **Delivery worker** — reads from `events:deliveries`, checks `notification_log` for duplicates, POSTs to each follower's Discord webhook

Leave it running (Ctrl+C to stop). Discord pings arrive within ~10s of a lineup change.

**Production** runs these as three separate Docker Compose services (`poller`, `fanout`, `delivery`) plus `api`. See `docs/local_prod_dry_run.md` for the prod-like setup.

---

## 6. Use the API and frontend

The FastAPI server exposes the HTTP API on port 8000. Start it with:

```bash
uv run atbatwatch run-api
```

Endpoints:
- `POST /auth/signup` — create an account (email, password, discord_webhook)
- `POST /auth/login` — get a JWT token
- `GET /players/search?q=<name>` — search MLB players
- `GET/POST/DELETE /me/follows` — manage follows

Interactive docs: `http://localhost:8000/docs`

**Frontend** — HTML/JS files live in `frontend/`. They need a real HTTP server (not `file://`):

```bash
uv run --directory frontend/ -m http.server 8080
```

Open `http://localhost:8080`. It auto-targets `http://localhost:8000` when served from localhost and `https://api.atbatwatch.prerak.net` in production — no manual edits needed.

For the full prod-image workflow (building the Docker image, running all services together), see `docs/local_prod_dry_run.md`.

---

## Resource usage (typical)

| Process | RAM |
|---------|-----|
| Postgres container | ~28 MB |
| Redis container | ~4 MB |
| `run-all` Python process | ~40 MB idle, spikes to ~90 MB during each MLB API poll |

Monitor containers live: `docker stats`

The `run-all` spike is httpx buffering each full live feed response (~1.5 MB raw JSON) into memory before parsing — all three representations (bytes, string, parsed dict) exist simultaneously, adding ~6 MB per live game per poll cycle.

---

## Teardown

```bash
docker compose down       # stop containers, keep data volumes
docker compose down -v    # stop containers AND delete all data
colima stop               # shut down the VM entirely
```

---

## `watch` vs `run-all`

`watch` is a simpler monolithic loop that skips the Redis Stream pipeline. It still works but doesn't log to `notification_log` and doesn't support the split-worker deployment model planned for Phase 3. Prefer `run-all` for all local testing going forward.

---

## Profiling memory with memray

To see exactly where RAM is going:

```bash
# Capture a profile (let it run for 2-3 minutes, then Ctrl+C)
uv run memray run --output memray.bin profile_run_all.py

# Generate a flamegraph (opens as memray-flamegraph-*.html)
uv run memray flamegraph memray.bin

# Or watch allocations live in the terminal
uv run memray run --live profile_run_all.py
```

`profile_run_all.py` is a thin script at the repo root that starts `run-all` directly, bypassing the Click CLI wrapper so memray can attach properly.

The flamegraph shows call stacks with bar widths proportional to memory allocated. The dominant allocation is `httpx._models.aread` — httpx buffers the full response body before handing it to the JSON parser.


## Deployment notes

### Github Secrets

┌───────────────────┬─────────────────────────────────────────────────────────────────────┐
│    Secret name    │                                Value                                │
├───────────────────┼─────────────────────────────────────────────────────────────────────┤
│ POSTGRES_USER     │ atbatwatch                                                          │
├───────────────────┼─────────────────────────────────────────────────────────────────────┤
│ POSTGRES_PASSWORD │ a strong random password                                            │
├───────────────────┼─────────────────────────────────────────────────────────────────────┤
│ POSTGRES_DB       │ atbatwatch                                                          │
├───────────────────┼─────────────────────────────────────────────────────────────────────┤
│ DATABASE_URL      │ postgresql+asyncpg://atbatwatch:<password>@postgres:5432/atbatwatch │
├───────────────────┼─────────────────────────────────────────────────────────────────────┤
│ REDIS_URL         │ redis://redis:6379/0                                                │
├───────────────────┼─────────────────────────────────────────────────────────────────────┤
│ DOMAIN            │ your domain (or leave unset — the [[ -n ]] check skips it)          │
├───────────────────┼─────────────────────────────────────────────────────────────────────┤
│ DEPLOY_HOST       │ already set                                                         │
├───────────────────┼─────────────────────────────────────────────────────────────────────┤
│ DEPLOY_KEY        │ already set                                                         │
└───────────────────┴─────────────────────────────────────────────────────────────────────┘

Optional (backup, only if you set up the Hetzner Storage Box):

These also require editing deploy.yml "Deploy to Hetzner CX22" step's env vars.

┌──────────────────┬───────────────────┐
│      Secret      │       Notes       │
├──────────────────┼───────────────────┤
│ STORAGE_BOX_HOST │ from .env.example │
├──────────────────┼───────────────────┤
│ STORAGE_BOX_USER │ from .env.example │
└──────────────────┴───────────────────┘