# Local prod-like dry run

How to spin up the full stack on your Mac using `docker-compose.prod.yml`, before deploying to a real server.

We skip the `caddy` service locally — it tries to bind ports 80/443 and provision real TLS, which is unwanted on a laptop. The app workers and API server don't depend on it.

## 1. Prerequisites

- Colima running (`colima start`, then `docker info` should succeed). Stop it later with `colima stop`.
- A local `.env` file (see step 2).

## 2. Create a local `.env`

```bash
cp .env.example .env
```

Then edit `.env`:

- The defaults mostly work — hostnames `postgres` and `redis` resolve inside the compose network.
- Change `POSTGRES_PASSWORD` to anything, and make sure the password in `DATABASE_URL` matches.
- Set `JWT_SECRET` to any string for local use.
- Set `CORS_ORIGIN=http://localhost:8080` (matches the local frontend server in step 6).
- Leave `DOMAIN` unset or blank — Caddy isn't running locally.

Minimal additions on top of the defaults:

```
JWT_SECRET=dev-secret
CORS_ORIGIN=http://localhost:8080
```

## 3. Bring it up

Build the image first to surface any Dockerfile errors quickly:

```bash
docker compose -f docker-compose.prod.yml build
```

Then start everything except Caddy:

```bash
docker compose -f docker-compose.prod.yml up -d --build postgres redis migrate poller fanout delivery api
```

- `--build` rebuilds the app image from the `Dockerfile`.
- `-d` detaches; stream logs separately (see step 4).
- `migrate` runs Alembic once (`restart: "no"`) and exits 0 when done.
- `api` starts FastAPI on port 8000, bound to `127.0.0.1` only (loopback — not reachable externally even in prod, where Caddy routes to it over the Docker network instead).

## 4. Verify it's working

```bash
# Service status — migrate should be "Exited (0)", others "Up"
docker compose -f docker-compose.prod.yml ps

# Tail all logs
docker compose -f docker-compose.prod.yml logs -f

# Tail a specific service
docker compose -f docker-compose.prod.yml logs -f poller

# Poke Postgres
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U atbatwatch -d atbatwatch -c "\dt"

# Poke Redis
docker compose -f docker-compose.prod.yml exec redis redis-cli ping
docker compose -f docker-compose.prod.yml exec redis redis-cli XLEN events:transitions

# Run a one-off CLI inside the image (same env as workers)
docker compose -f docker-compose.prod.yml run --rm poller atbatwatch games
```

**Signs it's healthy:**

- `migrate` exited cleanly (`docker compose logs migrate`).
- `poller` log shows it hitting the MLB API every N seconds.
- `redis-cli XLEN events:transitions` grows when the poller detects at-bat transitions.
- `fanout` and `delivery` logs show them consuming from the stream.
- No restart loops in `ps`.

## 5. Test the API with curl

The API is at `http://localhost:8000`. The auto-generated docs are at `http://localhost:8000/docs`.

```bash
# Sign up
curl -s -X POST http://localhost:8000/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"hunter2","discord_webhook":"https://discord.com/api/webhooks/..."}' \
  | uv run -m json.tool

# Log in and save the token
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"hunter2"}' \
  | uv run -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Search for a player
curl -s "http://localhost:8000/players/search?q=Shohei" \
  -H "Authorization: Bearer $TOKEN" | uv run -m json.tool

# Follow a player (use player_id from search results above)
curl -s -X POST http://localhost:8000/me/follows \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"player_id":660271,"full_name":"Shohei Ohtani"}' \
  | uv run -m json.tool

# List follows
curl -s http://localhost:8000/me/follows \
  -H "Authorization: Bearer $TOKEN" | uv run -m json.tool

# Unfollow
curl -s -X DELETE http://localhost:8000/me/follows/660271 \
  -H "Authorization: Bearer $TOKEN"
```

## 6. Test the frontend

The frontend HTML files are in `frontend/`. They use ES modules so they need a real HTTP server (not `file://`).

In a second terminal:

```bash
uv run --directory frontend/ -m http.server 8080
```

Then open `http://localhost:8080` in a browser. It redirects to `login.html`.

`api.js` automatically targets `http://localhost:8000` when served from `localhost`, and `https://api.atbatwatch.prerak.net` in production — no manual edits needed.

You can now sign up, log in, search for players, follow and unfollow — all backed by the local Postgres DB.

## 7. Seed the DB manually (alternative to the UI)

The CLI commands still work. Run them inside a one-off container so `DATABASE_URL` resolves to the `postgres` service:

```bash
# Create a user (password_hash will be empty — fine for CLI-created users)
docker compose -f docker-compose.prod.yml run --rm poller \
  atbatwatch user create \
  --email you@example.com \
  --discord-webhook 'https://discord.com/api/webhooks/...'

# Follow players
docker compose -f docker-compose.prod.yml run --rm poller \
  atbatwatch follow --email you@example.com --player "Shohei Ohtani"

docker compose -f docker-compose.prod.yml run --rm poller \
  atbatwatch follow --email you@example.com --player "Aaron Judge"
```

If a name is ambiguous (e.g. "Luis García"), the command errors out. Resolve the ID first with `atbatwatch lookup "Luis García"` (interactive picker), then insert directly via SQL:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U atbatwatch -d atbatwatch -c \
  "INSERT INTO players (player_id, full_name) VALUES (671277, 'Luis García Jr.') ON CONFLICT DO NOTHING;
   INSERT INTO follows (user_id, player_id) VALUES (1, 671277) ON CONFLICT DO NOTHING;"
```

### Verify

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U atbatwatch -d atbatwatch -c \
  "SELECT u.email, p.full_name, p.player_id
   FROM follows f JOIN users u USING (user_id) JOIN players p USING (player_id)
   ORDER BY u.email;"
```

## 8. Shut down + cleanup

```bash
# Stop containers, keep volumes (fast reset — data persists)
docker compose -f docker-compose.prod.yml down

# Stop + delete volumes (postgres data, redis data) — full wipe
docker compose -f docker-compose.prod.yml down -v

# Also remove the built image for a clean slate
docker compose -f docker-compose.prod.yml down -v --rmi local

# Nuke dangling build cache (optional, reclaims disk)
docker system prune -f
```

Logs are stored by Docker's json-file driver per container and are removed with `down`. Nothing leaks onto the host filesystem except the named volumes, which `-v` clears.
