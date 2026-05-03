# Go Rewrite — Implementation Handoff

This doc is the work plan for porting atbatwatch from Python to Go. The done condition is simple: `uv run pytest acceptance/` passes 5 runs in a row with all four services running as Go binaries.

You are not asked to design new behavior. The wire contracts are frozen at tag `acceptance-suite-v1`. Your job is to reimplement the four services in Go such that their externally-observable behavior is byte-identical to Python's.

---

## Reading order

1. `docs/testing_strategy.md` — **the spec.** Wire contracts for HTTP, Redis streams, Discord webhooks, MLB diffPatch responses, and DB schema. Treat it as immutable; flag anything you find wrong via PR before changing behavior.
2. `docs/testing_strategy_handoff.md` — context for how the acceptance suite was built and what the four preconditions are. The Go binaries must implement all four (env-overridable MLB URL, `*-once` CLI subcommands, `ATBATWATCH_FIXED_NOW` clock, `parse-diff-patch` CLI).
3. `CLAUDE.md` — commands, architecture sketch, and acceptance suite run instructions.
4. `docs/scale_up_plan.md` — broader context for why the rewrite is happening and the deployment target.
5. **Skim, don't study, the Python source.** It's the spec only insofar as the acceptance suite passes against it. You are not porting code line-by-line. Read each service's entry point in this order to map the architecture: `atbatwatch/cli.py` (subcommand dispatch), `http_api.py`, `delivery_worker.py`, `fanout_worker.py`, `poller.py`, `diff_engine.py`. Skip everything in `atbatwatch/` that isn't reachable from those.

Run the acceptance suite against the Python stack once before writing any Go:

```
docker compose -f docker-compose.acceptance.yml up -d --build --wait
uv run pytest acceptance/ -v
```

You can't reimplement a system you haven't seen running.

---

## Definition of done

- `docker-compose.acceptance.yml` (or a sibling) brings up four Go services.
- `uv run pytest acceptance/ -v` is green five runs in a row, identical pass count to Python (47/47).
- `docker compose -f docker-compose.prod.yml` runs the Go binaries in production-like config.
- The Python tree under `atbatwatch/` is deleted in the cutover PR. Tests under `tests/` are deleted in the same PR. Only `acceptance/`, `frontend/`, `scripts/`, `migrations/`, and the new Go tree remain.

The acceptance suite is non-negotiable. Don't relax it. If you find a contract that's wrong, fix the contract doc + the test in a separate PR before continuing.

---

## Recommended migration order

The four services are independently rewritable because they only communicate via Redis streams and Postgres. Cut over one at a time, run the suite after each, learn, repeat.

| Order | Service | LOC (Py) | Why this order |
|-------|---------|----------|----------------|
| 1 | `delivery` | ~135 | Smallest. Reads stream, posts webhook, writes one DB row. Good warmup. |
| 2 | `fanout` | ~70 | Reads stream, queries DB, writes per-user stream. No external HTTP. |
| 3 | `api` | ~195 | Largest, but isolated (no Redis stream consumption). Lots of contract surface. |
| 4 | `poller` | ~200 | Most complex. Owns `diff_engine` + `diff_patch` parsing. Save for last so you've already learned the project. |

After each service ports, the suite must remain green with mixed-language services running. That's why phase 0 (compose swap) matters — without it you can't validate per-service.

---

## Phase 0 — Compose swap mechanic (~half day)

Before writing any service code, set up the dev loop.

- Add `docker/api.Dockerfile`, `docker/poller.Dockerfile`, `docker/fanout.Dockerfile`, `docker/delivery.Dockerfile` (Go builds, multi-stage, tiny final images).
- Modify `docker-compose.acceptance.yml` so each service's `build.dockerfile` is parameterized by an env var, e.g. `${API_DOCKERFILE:-Dockerfile}` defaulting to the existing Python build. Setting `API_DOCKERFILE=docker/api.Dockerfile` swaps just that one service.
- Verify: with no Go binaries written yet, `docker compose -f docker-compose.acceptance.yml up -d --build` still produces the same 47/47 green run.

**Acceptance:** `API_DOCKERFILE=docker/api.Dockerfile docker compose -f docker-compose.acceptance.yml up` fails to build (no Go code yet) but the unparameterized version still works.

---

## Phase 1 — Go scaffolding (~half day)

Land an empty Go workspace next to the Python tree.

- Module path: `github.com/<your-handle>/atbatwatch` (confirm with the project owner).
- Layout (suggested, not prescriptive):
  ```
  go.mod
  cmd/
    api/main.go
    poller/main.go
    fanout/main.go
    delivery/main.go
  internal/
    contracts/      # types matching the wire contracts
    db/             # pgx wrapper, queries
    redis/          # streams helpers
    mlb/            # MLB API client + diffPatch parser
    notify/         # Discord webhook formatter
    diffengine/     # offense state diff
  ```
- Suggested deps (use what you're comfortable with — these are not contracts):
  - HTTP: `chi` or `echo`. Both are fine; the contract doesn't care.
  - Postgres: `jackc/pgx/v5`.
  - Redis: `redis/go-redis/v9`.
  - JWT: `golang-jwt/jwt/v5` — must use HS256.
  - Argon2: `golang.org/x/crypto/argon2`. **See "argon2 compatibility" below.**
  - CLI: `spf13/cobra` or `urfave/cli`. The Python uses `click`; either Go option produces the same UX.
- Each `cmd/<service>/main.go` accepts subcommands matching the Python ones — `run`, `<svc>-once` — so the acceptance suite's `run_worker(<svc>, "<svc>-once")` calls work unchanged.

**Acceptance:** `go build ./...` succeeds. Each service binary prints help when run with no args.

---

## Phase 2–5 — Per-service ports

Each service is a separate phase, in the order in §"Recommended migration order." For each:

1. Read the corresponding wire contract section in `testing_strategy.md`.
2. Read the matching Python file as documentation.
3. Write the Go service.
4. Swap that service via `docker-compose.acceptance.yml` (env var from phase 0).
5. Run the acceptance suite. Iterate until green.
6. Run it 5× in a row to confirm no flake.
7. Open a PR with just that service swapped. Land it. Move on.

### Per-service expectations

#### `delivery` (phase 2)

- **Reads:** `XREADGROUP` on `events:deliveries` (group `delivery-group`, consumer `delivery-1`).
- **For each message:** check `notification_log` for `(event_id, user_id)` → if exists, ACK and skip. Otherwise format the Discord content string per `testing_strategy.md` § Outbound Discord webhook (em-dash U+2014 — get this byte-identical) and POST to `webhook_url`. On 2xx, insert into `notification_log` and ACK. On error, do **not** ACK.
- **Subcommands:** `delivery run` (long-running loop) and `delivery delivery-once` (drain pending and exit).
- **Watch for:** the singular/plural `out`/`outs` rule. The unique constraint race (two consumers, same event_id+user_id) — current Python swallows IntegrityError; Go should `ON CONFLICT DO NOTHING` or equivalent.

#### `fanout` (phase 3)

- **Reads:** `events:transitions` (group `fanout-group`, consumer `fanout-1`).
- **For each message:** SELECT user_id + notification_target_id from `users` JOIN `follows` WHERE `player_id = $1`. For each follower, `XADD events:deliveries` with all transition fields preserved + `user_id` + `webhook_url`. ACK after.
- **Subcommands:** `fanout run`, `fanout fanout-once`.
- **Watch for:** "all transition fields preserved" is a contract — every key must round-trip. No new fields, no dropped fields.

#### `api` (phase 4)

- **Endpoints:** see `testing_strategy.md` § HTTP API. All shapes byte-identical.
- **Auth:** Bearer JWT, HS256, secret from `JWT_SECRET` env var, claim `{"sub": "<user_id>", "exp": <unix_ts>}`. 30-day expiration.
- **Argon2:** see compatibility note below.
- **Player search:** proxies the MLB API (`MLB_API_BASE_URL`). Filter to `active=true`, drop minor/winter league entries (those with `currentTeam.parentOrgId`). Hydrate currentTeam in a single batched call per result. The acceptance suite's `mlb_stub` returns canned fixtures — `fixtures/people_search/` and `fixtures/person/` — so you don't talk to real MLB during tests.
- **Watch for:** Python returns 401 for both missing-bearer and bad-token. The contract says Go may return 401 or 403; tests accept either. Pick one and be consistent.

#### `poller` (phase 5, the hard one)

- **Reads:** MLB API. Loops every `POLL_INTERVAL_SECONDS` (config).
- **Logic:**
  1. Fetch today's schedule (Eastern time). Filter to `status == "Live"`.
  2. If no live games and it's before 6 AM ET, also check yesterday's schedule (catches late west-coast games).
  3. For each live game, on first poll: full `get_live_feed` and cache the `metaData.timeStamp`.
  4. On subsequent polls: `diffPatch?startTimecode=<cached_ts>`.
  5. Parse the diffPatch response. **The shape is `[{"diff": [<RFC 6902 ops>]}, ...]`** — a list of envelopes, not a flat list of ops. Flatten before checking for offense ops or extracting the new timestamp. (The original Python had this wrong; see commit `61c7b6b` for the corrected parser.)
  6. If the patch has any op whose path contains `"offense"`, fetch the full feed; otherwise advance the timecode and skip diff.
  7. Run the diff engine: read cached offense state from Redis hash `game:<pk>:offense`, compare to current offense, `XADD events:transitions` for any batter/on-deck change, write new state back with 24h TTL.
- **3-outs special case:** if the linescore shows 3 outs, advance to the next half-inning before emitting (so the notification reads "Bot 4, 0 outs", not "Top 4, 3 outs"). See `testing_strategy.md` § Redis stream: `events:transitions`.
- **Subcommands:** `poller run`, `poller poll-once`.
- **`occurred_at` field:** must honor the `ATBATWATCH_FIXED_NOW` env var when set.
- **`parse-diff-patch` CLI:** the *Go* binary must also expose `parse-diff-patch <patch_json_path> --start-timecode <ts>` printing `{"full_response": <body|null>, "new_timecode": <ts>, "needs_full_fetch": true?}` to stdout. The acceptance suite's `test_diff_patch.py` calls it via `subprocess`. Currently it calls `uv run atbatwatch parse-diff-patch …`; once `poller` is Go you'll want to either keep that command name pointing at the Go binary or update the test.

---

## Argon2 compatibility

The Python implementation uses `passlib.hash.argon2` with default parameters. Go's `golang.org/x/crypto/argon2` is a primitive, not a hash-format library — you'll need to encode/decode the modular crypt format `$argon2id$v=19$m=…,t=…,p=…$<salt>$<hash>` yourself, or pull a library that does (e.g. `alexedwards/argon2id`).

**Two scenarios:**

1. **Fresh DB at cutover (recommended).** Doesn't matter what argon2 parameters Go picks; existing users are gone. The acceptance suite signs up and logs in within the same run, so as long as Go can verify its own hashes, login works.
2. **Migrate existing prod users.** Then Go must be able to verify a passlib-generated hash. Test this explicitly: have the Python stack create a user, dump the hash, and verify it from Go before cutover.

The acceptance suite covers scenario 1. If you choose scenario 2, add a contract test that takes a known passlib hash from a fixture and verifies Go can authenticate against it.

---

## Migrations

`migrations/versions/*.py` are Alembic. Two options:

1. **Keep Alembic.** Run `uv run alembic upgrade head` from a sidecar container in compose. The Go services don't run migrations themselves. Cheap; works today.
2. **Port to `golang-migrate`.** Translate the SQL once; delete the Alembic Python dep at cutover.

I recommend **option 1 until cutover, then option 2**. Don't port migrations until you're deleting the Python tree — Alembic works, and migration tooling is not on the critical path.

---

## Cutover

Once all four services are green in Go and you've run the suite 5× in a row:

1. Update `docker-compose.prod.yml` to point at Go images.
2. Stage to a non-prod environment (compose-up locally with prod-like config).
3. Open the cutover PR: deletes `atbatwatch/`, deletes `tests/`, deletes `pyproject.toml`'s app deps (keep dev tooling for `acceptance/` if it stays in pytest, or port `acceptance/` to Go also — separate decision).
4. Tag `go-cutover-v1` after merge.

The acceptance suite stays in Python (pytest is fine — tests don't need to be in the same language as the system). If you want to port `acceptance/` to Go too for consistency, that's a follow-up project, not a cutover blocker.

---

## Open questions / things to surface

These don't have answers; bring them back if you hit them.

1. **Argon2 strategy.** Fresh-DB vs hash-migration — confirm with the project owner before phase 4. (My read: there are very few prod users today, so fresh-DB is fine.)
2. **HTTP framework choice.** Pick before phase 4. Doesn't matter functionally; pick what you'll enjoy debugging.
3. **CORS configuration.** Python reads `CORS_ORIGINS` from `settings.py`. Confirm the prod value with the project owner; the acceptance suite doesn't exercise CORS so the contract is loose here.
4. **Logging.** Python uses `print()`. Production logs go… wherever stdout goes. If you want structured logging in Go (recommended: `log/slog`), that's an improvement, not a contract change. Confirm log shape isn't being scraped by anything.
5. **`/players/search` 1-char query behavior.** Python returns `{"players": []}` for `q` shorter than 2 chars (not 422). Acceptance test pins this. Don't be tempted to "fix" it.

---

## Estimated scope

Rough only — depends heavily on Go familiarity:

- Phase 0 (compose swap): half day
- Phase 1 (scaffolding): half day
- Phase 2 (delivery): 1 day
- Phase 3 (fanout): 1 day
- Phase 4 (api): 2–3 days
- Phase 5 (poller): 2–3 days
- Cutover: half day

**Total: ~7–10 working days** for someone comfortable in Go and the project. The acceptance suite is the safety net; lean on it heavily and you won't spend time on the kind of integration bugs that usually dominate rewrites.
