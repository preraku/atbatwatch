# Testing Strategy (Rewrite-Safe)

## Goal

Build a test suite that pins down the **observable behavior** of atbatwatch so the codebase can be rewritten from Python to Go without losing correctness. The same suite runs unchanged against both implementations; a green run on Go is the rewrite's done-condition.

**Non-goal:** unit tests of internal functions. Anything that imports `atbatwatch.*` Python modules is throwaway and outside the scope of this doc.

## Principle

Tests interact with the system **only through external surfaces**:

- HTTP requests on a real port
- Redis commands on a real socket
- Postgres queries on a real socket
- Outbound HTTP captured by a stub webhook server
- Inbound MLB API mocked by a stub fixture server

If a test imports application code, it doesn't belong in this suite.

---

## Preconditions (changes to the Python code before the harness can be built)

The current code has three properties that block black-box testing. Fix these *first* — they're cheap and the Go rewrite will need them anyway.

1. **Env-overridable MLB API base URL.** `atbatwatch/api.py:18` hardcodes `BASE_URL`. Move to `MLB_API_BASE_URL` env var, default to `https://ws.statsapi.mlb.com`. Tests point it at the stub server.
2. **A `poll-once` entrypoint.** The poller is a `while True` loop. Add `atbatwatch poll-once` (CLI subcommand) that runs exactly one poll cycle and exits. Same for `fanout-once` and `delivery-once`, or accept a `--max-iterations 1` flag on the existing workers. The Go rewrite implements the same flag.
3. **Injectable wall clock.** `diff_engine.py` writes `datetime.now(timezone.utc).isoformat()` into `occurred_at`. Either honor `ATBATWATCH_FIXED_NOW` (ISO string env var) when set, or document that `occurred_at` is asserted with a regex matcher. Pick one; goldens are otherwise flaky on every run.
4. **A `parse-diff-patch` CLI.** Exposes the diffPatch parser as a black box: `atbatwatch parse-diff-patch <baseline_path> <patch_path> --start-timecode <ts>` reads the two JSON files, runs the parser, and writes `{"full_response": <body|null>, "new_timecode": <ts>}` to stdout. Used by `test_diff_patch.py` to validate parser behavior across the four response shapes. The Go rewrite reimplements the same flag.

These four changes are the entire "test infrastructure" ask. Everything else in this doc lives outside the application code.

---

## Wire contracts

This section is the spec the Go rewrite must implement. Tests assert against these byte-level shapes.

### HTTP API

Base URL: `http://localhost:8001` (acceptance stack port mapping; prod runs on 8000). Auth: `Authorization: Bearer <jwt>` where indicated.

| Method | Path | Auth | Request | Response |
|--------|------|------|---------|----------|
| POST | `/auth/signup` | — | `{email, password, discord_webhook}` | 201 `{token}` / 409 duplicate / 422 missing fields |
| POST | `/auth/login` | — | `{email, password}` | 200 `{token}` / 401 invalid |
| GET | `/me/follows` | yes | — | 200 `{follows: [{player_id, full_name, team, position}]}` |
| POST | `/me/follows` | yes | `{player_id, full_name, team?, position?}` | 201 `{player_id, full_name, team, position}` |
| DELETE | `/me/follows/{player_id}` | yes | — | 204 / 404 not following |
| GET | `/players/search?q=` | yes | — | 200 `{players: [{player_id, full_name, team, position}]}` |

Auth contract: missing/invalid token is rejected. The Python implementation returns **401** for both missing bearer and bad token (verified by acceptance suite). Go may return 401 or 403; tests assert `in (401, 403)` to remain compatible with either choice.

### Redis stream: `events:transitions`

Written by the diff engine after a successful poll. Field names and types are the contract.

| Field | Type | Format |
|-------|------|--------|
| `event_id` | string | UUIDv4 |
| `game_id` | string | int-as-string |
| `player_id` | string | int-as-string |
| `player_name` | string | — |
| `state` | string | `at_bat` \| `on_deck` |
| `home_team_id` | string | int-as-string |
| `home_team_name` | string | — |
| `away_team_id` | string | int-as-string |
| `away_team_name` | string | — |
| `inning` | string | int-as-string, 1-indexed |
| `inning_half` | string | `Top` \| `Bot` |
| `outs` | string | int-as-string, 0–2 (3 outs auto-advances to next half) |
| `occurred_at` | string | ISO 8601 UTC, `…+00:00` |

All values are strings (Redis stream encoding). No additional fields permitted.

### Redis stream: `events:deliveries`

Written by fanout. **Every field from `events:transitions` carries through unchanged**, plus:

| Field | Type | Format |
|-------|------|--------|
| `user_id` | string | int-as-string |
| `webhook_url` | string | full URL |

Invariant test: for every transition message that has N followers, exactly N delivery messages exist where `event_id` matches and the transition fields are byte-identical.

### Outbound Discord webhook

POST to `webhook_url` (captured by stub) with body `{"content": "<string>"}`. The `content` string format **is** part of the contract — Go must produce byte-identical output.

Format:

```
<label>: **<player_name>** (<away_team_name> @ <home_team_name> — <inning_half> <inning>, <outs> <out_word>)
```

Where:

- `<label>` is `⚾ **AT BAT**` when `state == "at_bat"`, `🔄 **ON DECK**` when `state == "on_deck"`
- `<inning_half>` is `Top` or `Bot`
- `<out_word>` is `out` when `outs == 1`, `outs` otherwise (so `0 outs`, `1 out`, `2 outs`)
- The em-dash separator is U+2014 (`—`), not a hyphen

Examples:

```
⚾ **AT BAT**: **Brandon Marsh** (Atlanta Braves @ Philadelphia Phillies — Bot 4, 1 out)
🔄 **ON DECK**: **Alec Bohm** (Atlanta Braves @ Philadelphia Phillies — Top 7, 2 outs)
```

Tests assert string equality against expected `content` values built from the fixture data. The em-dash (U+2014) and all label strings have been verified byte-identically against the live Python stack.

### MLB diffPatch response

The poller hits `/api/v1.1/game/{pk}/feed/live/diffPatch?startTimecode=…` on every poll after the first. The response is one of two top-level shapes:

- **List:** `[{"diff": [<op>, …]}, …]` — a list of envelopes, each carrying an array of RFC 6902 ops under `diff`. The parser flattens across envelopes before inspecting paths. (The original Python parser iterated the top-level list directly — a latent bug, since envelopes have no `path` key. Fixed during P1.4.)
- **Dict (fullUpdate):** a full live-feed body with top-level `metaData`.

The parser must handle four cases:

1. **Patch list, no offense ops.** No flattened op has a `path` containing `"offense"`. Parser returns `(None, new_timecode, needs_full_fetch=False)` where `new_timecode` is the `value` of the `/metaData/timeStamp` op, falling back to `start_timecode` if absent.
2. **Patch list, with offense ops.** Any flattened op has a `path` containing `"offense"`. Parser returns `(None, start_timecode, needs_full_fetch=True)`. The caller fetches the full feed.
3. **fullUpdate object.** Parser returns `(body, body.metaData.timeStamp, needs_full_fetch=False)`.
4. **Empty list.** Treated as case 1 with no flattened ops; `new_timecode == start_timecode`.

Fixtures live under `fixtures/diff_patch/<game_pk>/`:

| File | Shape (per `.meta.json`) |
|------|--------------------------|
| `baseline.json`, `baseline_2.json` | full live feed (input to diffPatch tests) |
| `patch_t+15s.json`, `patch_t+60s.json`, `patch_t2+30s.json` | one of the four shapes above |

Each patch fixture has a sibling `.meta.json` recording the `start_timecode` used and the captured `shape` string. Tests dispatch on shape to exercise each parser branch. Note: meta files from the 2026-04-29 capture under-count offense ops (e.g. `823717/patch_t+15s.meta.json` claims `offense_ops=0` but the patch contains them). The capture script was fixed in P1.1 to flatten envelopes; re-run the script if the sidecars need to be authoritative. The contract tests in `acceptance/contracts/test_diff_patch.py` derive expected shapes by inspection, not by trusting the sidecars.

### Database schema

Captured by Alembic migrations under `alembic/versions/`. Tests run `alembic upgrade head` against the test DB; the migrations themselves are the contract. The Go rewrite reuses the same migrations (Go has working Alembic-compatible runners, or a one-time port to `golang-migrate` with the same SQL).

`users` columns (confirmed against live schema): `user_id`, `email`, `password_hash`, `notification_target_type`, `notification_target_id`, `created_at`. `notification_target_type` is always `"discord"` for the current Discord-only MVP; the column is retained for future webhook types. `notification_target_id` stores the full Discord webhook URL.

Invariants tests assert:

- `users.email` unique
- `(follows.user_id, follows.player_id)` primary key (idempotent inserts)
- `notification_log` unique on `(event_id, user_id)` — duplicate insert raises an integrity error
- `notification_log.status` is set after a successful delivery

---

## Test harness

A separate top-level directory `acceptance/` holds the suite. Pytest is the runner — language doesn't matter, but pytest is already in the project. **No imports from `atbatwatch.*`.**

```
acceptance/
  conftest.py            # fixtures: stack, db_reset, mlb_stub, webhook_capture, http_client
  stubs/
    mlb_stub.py          # tiny aiohttp/FastAPI server serving fixtures/live_feed/*.json
    webhook_capture.py   # records POSTs in-memory, exposes /captured for assertions
  contracts/
    test_http_api.py
    test_streams.py
    test_db_schema.py
    test_e2e_pipeline.py
    test_idempotency.py
  fixtures/              # symlink to ../fixtures or copies
```

### Stack

A dedicated `docker-compose.acceptance.yml` brings up the SUT:

```
postgres   # ephemeral volume, dropped between runs
redis      # ephemeral
migrate    # runs alembic upgrade head, exits
api        # MLB_API_BASE_URL=http://mlb-stub:9001
poller
fanout
delivery
mlb-stub        # acceptance/stubs/mlb_stub.py
webhook-capture # acceptance/stubs/webhook_capture.py
```

The stack is started once per test session (`pytest-docker-compose` or a `subprocess.run` fixture). Tests assume it's running on `localhost`.

### Per-test isolation

- **DB:** `TRUNCATE users, players, follows, notification_log RESTART IDENTITY CASCADE` in a session-scoped fixture's teardown, or per-test if needed.
- **Redis:** `FLUSHDB` between tests.
- **Webhook capture:** `DELETE /captured` resets recorded calls.
- **MLB stub:** stateless; a fixture parameter controls which fixture file it serves.

### Driving the system

Triggering a single pipeline cycle:

1. Test seeds users/follows via HTTP.
2. Test sets MLB stub to serve a chosen fixture (`POST /admin/fixture` with `{game_pk, path}`).
3. Test calls `docker compose exec poller atbatwatch poll-once` (and `fanout-once`, `delivery-once`).
4. Test reads Redis streams via `XRANGE`, queries Postgres directly, fetches `GET /captured` from webhook stub.

This sequence has zero Python-app coupling. Replacing every service with a Go binary changes nothing in the test code.

---

## Test cases

Roughly 25 cases, grouped by surface. Each one is a black-box scenario.

### HTTP API (`test_http_api.py`)

- signup happy path returns token; token decodes to a user that exists in DB (asserted via direct SQL, not via a `/me` endpoint that may not exist)
- signup duplicate email → 409
- signup missing field → 422
- login wrong password → 401
- login unknown email → 401
- `/me/follows` without auth → rejected (401 or 403 — pin the choice)
- `/me/follows` returns empty list for new user
- `POST /me/follows` then `GET /me/follows` returns the player
- `POST /me/follows` is idempotent on duplicate
- `DELETE /me/follows/{id}` removes; second delete → 404
- `/players/search?q=` requires auth
- `/players/search?q=a` (1 char) returns 200 `{"players": []}` (not 422 — confirmed by acceptance suite)

### Stream contracts (`test_streams.py`)

- After `poll-once` with `in_progress_game_823475.json`: `events:transitions` has exactly 2 messages with `state` ∈ {`at_bat`, `on_deck`}
- All 13 documented fields are present on every message; no extra fields
- Field types match the contract table (regex-validate UUID, ISO 8601, int-as-string)
- Warmup fixture → 0 messages
- Final fixture → 0 messages
- 3-outs end-of-half-inning → messages show `Bot N+0` / `Top (N+1)` / `outs=0`, never `outs=3`
- Second identical poll → 0 new messages (cached state)
- After fanout-once: every transition produces `followers_count` delivery messages with all transition fields preserved + `user_id` + `webhook_url`

### End-to-end (`test_e2e_pipeline.py`)

- Seed user A following player 669016. MLB stub serves `in_progress_game_823475.json`. Run poll → fanout → delivery. Webhook capture has exactly 1 POST whose `content` equals the expected string built from the fixture (e.g. `⚾ **AT BAT**: **Brandon Marsh** (Atlanta Braves @ Philadelphia Phillies — <half> <inning>, <outs> <out_word>)`, with inning/outs pulled from the fixture's linescore). `notification_log` has 1 row with matching `event_id` and `user_id`.
- Seed user A and user B following the same player. Same flow. Webhook capture has 2 POSTs — one to each user's webhook URL — each with `content` byte-identical to the expected string. `notification_log` has 2 rows sharing `event_id` and differing `user_id`.
- Seed two followers, but emit a transition for the on-deck player. Webhook capture's two POSTs both use the `🔄 **ON DECK**` label; assert string equality.
- No followers → fanout produces 0 delivery messages, webhook capture is empty, `notification_log` empty.

Each test computes its expected `content` string from the fixture's known fields rather than hard-coding it, so a fixture refresh updates expectations automatically.

### Idempotency (`test_idempotency.py`)

- Run the full pipeline twice with the same fixture (same `event_id`s persisted via redis state). Webhook capture stays at 1 POST per (event, user). `notification_log` count unchanged on second run.
- Direct insert: writing a duplicate `(event_id, user_id)` to `notification_log` via SQL raises an integrity error.

### diffPatch parsing (`test_diff_patch.py`)

Unlike the other groups, this one tests a *parser* rather than a service surface — but it's still language-agnostic: feed JSON in, assert tuple out. The Go rewrite exposes the parser via a CLI: `atbatwatch parse-diff-patch <baseline_path> <patch_path> --start-timecode <ts>` returning JSON `{"full_response": <body|null>, "new_timecode": <ts>}` on stdout.

- For each `fixtures/diff_patch/<game_pk>/patch_*.json`:
  - Run the parser CLI with the matching `start_timecode` from `.meta.json`.
  - Assert the returned tuple matches the expected shape:
    - `shape: full_update` → `full_response` is non-null and equals the patch body; `new_timecode` equals `body.metaData.timeStamp`.
    - `shape: patch_array (offense_ops=0)` → `full_response` is null; `new_timecode` equals the value of the `/metaData/timeStamp` op (or `start_timecode` if no such op exists).
    - `shape: patch_array (offense_ops>0)` → `full_response` equals what `get_live_feed` would return (test against a stub-served baseline); `new_timecode` equals `start_timecode`.
- Empty array case (`fixtures/diff_patch/824445/patch_t+15s.json`, `n_ops=0`): `full_response` is null, `new_timecode` equals `start_timecode`.

### DB schema (`test_db_schema.py`)

- After `alembic upgrade head` against an empty DB, all four tables exist with the expected columns (introspect via `information_schema`).
- Constraints from the contract section are enforced (unique email, follow PK, notification_log unique).

---

## Migration plan

1. **Land preconditions** in current Python (env-overridable MLB URL, `poll-once`, fixed-clock support). Small PR.
2. **Build harness** in `acceptance/` and the stub servers. Run against the existing Python stack until green.
3. **Freeze contracts.** Once the suite is green, the wire-contracts section above is locked. Any change to it from this point is a deliberate API change, not a refactor.
4. **Begin Go rewrite** service by service. Add a parallel `docker-compose.acceptance-go.yml` that swaps services. The same suite runs against it.
5. **Cutover** when the Go suite matches the Python suite. Delete `atbatwatch/` Python and the Python-side unit tests in the same PR.

The existing pytest tests under `tests/` stay in place during the rewrite (they catch regressions in the Python code while the harness is being built) but are not part of the rewrite contract and are deleted at cutover.

---

## What this doc does **not** cover

- Performance / load testing — separate concern, not a rewrite blocker.
- MLB API edge cases beyond the existing fixtures — capture more fixtures as bugs are found.
- Discord prose formatting — explicitly out of contract.
- Frontend testing — see `docs/agent_frontend_testing.md`.
