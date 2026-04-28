# CLAUDE.md

## Commands

```bash
# Code quality (run on host)
uv sync --group dev
uv run ruff check --fix . && uv run ruff format .
uv run pyrefly check
uv run pytest

# Spin up the full stack (see docs/local_prod_dry_run.md for full walkthrough, no caddy locally)
docker compose -f docker-compose.prod.yml up -d --build postgres redis migrate poller fanout delivery api

# One-off CLI (lookup, games, state, user create, follow, unfollow): see docs/local_prod_dry_run.md
docker compose -f docker-compose.prod.yml run --rm poller atbatwatch <command>

# Logs / status
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f [service]

# Teardown
docker compose -f docker-compose.prod.yml down        # keep volumes
docker compose -f docker-compose.prod.yml down -v     # wipe volumes
```

## Architecture

Four components communicate via Redis Streams:

1. **poller** — polls MLB live feeds every N seconds; emits offense-state changes to `events:transitions`
2. **fanout** — reads `events:transitions`, queries DB for followers, writes per-user jobs to `events:deliveries`
3. **delivery** — reads `events:deliveries`, POSTs Discord webhook, logs to `notification_log` (idempotent on `event_id`)
4. **api** — FastAPI on `:8000`; signup/login (argon2 + JWT), player search, and follow management

In production all four run as separate Docker Compose services. Locally, `run-all` runs them as concurrent asyncio tasks in one process (dev convenience only).

**Offline testing:** `fixtures/` holds captured snapshots (`live_feed/`, `schedule/`, `people_search/`, `person/`). `state` and `games` CLI commands accept `--fixture <path>`.

## Docs

- `docs/howto.md` — local dev setup using `docker-compose.yml` (dev creds, no prod image)
- `docs/local_prod_dry_run.md` — full prod-like stack locally with `docker-compose.prod.yml`
- `docs/mlb_api.md` — MLB Stats API endpoints and offense-state payload shape
- `docs/scale_up_plan.md` — system design, data model, and scaling path to ~1M users
- `docs/agent_frontend_testing.md` — how to verify frontend changes against the local stack
