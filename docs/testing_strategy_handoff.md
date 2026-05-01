# Testing Strategy — Implementation Handoff

This doc is the work plan for building the acceptance test suite described in `docs/testing_strategy.md`. The strategy doc is the spec. This doc is the sequenced task list.

**Outcome:** a green acceptance suite running against the current Python implementation, ready to be re-run unchanged against a future Go rewrite.

---

## Reading order

1. `docs/testing_strategy.md` — the spec. Read it first; this doc only makes sense after.
2. `CLAUDE.md` — commands and architecture sketch.
3. `docs/scale_up_plan.md` — broader context for why the rewrite is happening.
4. Code skim, in this order: `atbatwatch/api.py`, `diff_engine.py`, `fanout_worker.py`, `delivery_worker.py`, `notifier.py`, `http_api.py`, `db.py`. Don't read tests/ — they're the throwaway.
5. `fixtures/` — the inputs the tests will drive. Note the new `fixtures/diff_patch/<game_pk>/` captures with `.meta.json` sidecars.

Run `docker compose -f docker-compose.prod.yml up -d --build` once and exercise the system manually (signup, follow a player, see a notification). You can't write good acceptance tests for a system you haven't seen run.

---

## Phases

Five phases, sequential. Each phase ends with a verifiable green state. Don't start phase N+1 without finishing phase N — the harness skeleton (phase 2) needs the preconditions (phase 1), and the test files (phases 3–4) need the harness.

### Phase 1 — Preconditions (4 small PRs, ~1 day)

Land these in the Python code. Each is independent and mergeable on its own.

#### 1.1 Env-overridable MLB API base URL

- **File:** `atbatwatch/api.py:18`
- **Change:** `BASE_URL = os.getenv("MLB_API_BASE_URL", "https://ws.statsapi.mlb.com")`
- **Why:** the harness's MLB stub server has to be reachable from the poller container at a non-default URL.
- **Acceptance:** `MLB_API_BASE_URL=http://example.invalid uv run atbatwatch games` fails with a connection error, not a hardcoded-URL error. Existing tests still pass.

#### 1.2 `poll-once` CLI subcommand

- **File:** `atbatwatch/cli.py`
- **Change:** add `atbatwatch poll-once` that runs exactly one iteration of `run_poller`'s loop body and exits 0. Same for `fanout-once` and `delivery-once`. Factor the loop bodies into `_poll_iteration()` etc. so the CLI can call them once.
- **Why:** the current `while True` workers can't be driven deterministically from a test.
- **Acceptance:** with redis + postgres up, `docker compose -f docker-compose.prod.yml run --rm poller atbatwatch poll-once` runs one cycle and exits. `XLEN events:transitions` shows messages if there's a live game.

#### 1.3 Injectable wall clock

- **File:** `atbatwatch/diff_engine.py:75`
- **Change:** wrap `datetime.now(timezone.utc).isoformat()` in a helper that returns `os.environ["ATBATWATCH_FIXED_NOW"]` when set, otherwise the real now.
- **Why:** `occurred_at` field assertions are otherwise flaky.
- **Acceptance:** test that sets `ATBATWATCH_FIXED_NOW=2026-01-01T00:00:00+00:00`, runs `process_game`, and asserts the emitted stream message has that exact `occurred_at`.

#### 1.4 `parse-diff-patch` CLI subcommand

- **File:** `atbatwatch/cli.py` (and refactor `atbatwatch/api.py`)
- **Change:** extract the parser inside `MlbApi.get_live_feed_diff` (currently `api.py:78-90`) into a pure function `parse_diff_patch(body, start_timecode) -> tuple[LiveFeedResponse | None, str]`. Make `get_live_feed_diff` call it. Add CLI: `atbatwatch parse-diff-patch <patch_json_path> --start-timecode <ts>` that loads JSON, calls the pure parser, and prints `{"full_response": <body|null>, "new_timecode": <ts>}` to stdout.
- **Note:** the offense-ops branch currently calls `get_live_feed()`. The pure parser should *not* fetch — it should return a sentinel like `(FETCH_FULL, start_timecode)` that the caller resolves. Or: the parser returns `(None, start_timecode, needs_full_fetch=True)` and the CLI separately handles the fetch decision. Pick whichever feels cleaner.
- **Why:** the diffPatch parser is the trickiest piece of code in the project and currently has zero test coverage.
- **Acceptance:** `atbatwatch parse-diff-patch fixtures/diff_patch/823717/patch_t+15s.json --start-timecode 20260429_191321` prints valid JSON.

### Phase 2 — Harness skeleton (~1–2 days)

Build the test infrastructure. No test cases yet — the goal is "pytest runs zero tests successfully against a running stack."

#### 2.1 Directory layout

Create:
```
acceptance/
  __init__.py
  conftest.py
  stubs/
    __init__.py
    mlb_stub.py
    webhook_capture.py
  contracts/
    __init__.py
  fixtures/   # symlink to ../fixtures
docker-compose.acceptance.yml
```

#### 2.2 `docker-compose.acceptance.yml`

- **Source:** copy from `docker-compose.prod.yml`. Strip caddy. Override `MLB_API_BASE_URL=http://mlb-stub:9001` on api/poller. Override webhook URLs in seeded users to `http://webhook-capture:9002/hooks/<id>`.
- **Add services:** `mlb-stub` (port 9001), `webhook-capture` (port 9002).
- **Use ephemeral volumes** so each `docker compose up` is clean.

#### 2.3 MLB stub server (`acceptance/stubs/mlb_stub.py`)

Tiny FastAPI app. Three behaviors:

- `GET /api/v1/schedule` → returns a `fixtures/schedule/*.json` body.
- `GET /api/v1.1/game/{pk}/feed/live` → returns the fixture currently configured for that pk.
- `GET /api/v1.1/game/{pk}/feed/live/diffPatch` → returns the patch fixture currently configured.
- `POST /admin/configure` → test-control endpoint: `{game_pk, live_feed_path?, diff_patch_path?, schedule_path?}` updates an in-memory map.
- `POST /admin/reset` → clears the map.

#### 2.4 Webhook capture server (`acceptance/stubs/webhook_capture.py`)

Tiny FastAPI app:

- `POST /hooks/{webhook_id}` → records `(webhook_id, body, headers)` in memory; returns 200.
- `GET /captured` → returns the list, optionally filtered by `?webhook_id=`.
- `DELETE /captured` → resets.

#### 2.5 `conftest.py` fixtures

- `stack` (session): asserts the compose stack is up; tears down only if the test process started it.
- `db` (function): connection to acceptance Postgres. Truncates user/follow/notification_log tables in teardown.
- `redis` (function): redis client. `FLUSHDB` in teardown.
- `mlb_stub` (function): client wrapper around the stub's `/admin` endpoints. `reset` in teardown.
- `webhook_capture` (function): client wrapper. `DELETE /captured` in teardown.
- `http` (function): `httpx.AsyncClient(base_url="http://localhost:8000")`.
- `signup_and_login` (function): helper that creates a user and returns `(user_id, token, webhook_id)`.

**Acceptance for phase 2:** `uv run pytest acceptance/ -v` runs zero tests, exits 0, and the conftest fixtures all import cleanly.

### Phase 3 — First contract test: HTTP API (~1–2 days)

Vertical slice that proves the harness works. Implement `acceptance/contracts/test_http_api.py` covering the ~12 cases listed in `testing_strategy.md` § Test cases > HTTP API.

Expect to find 1–3 things wrong with phase 2 while doing this. That's fine. Iterate.

**Acceptance:** `uv run pytest acceptance/contracts/test_http_api.py -v` is green against the running stack. Run it 3 times in a row to confirm test isolation works (no inter-test state leakage).

### Phase 4 — Remaining contract tests (~2–3 days)

In any order, ideally one PR per file:

- `test_streams.py` — Redis stream contract assertions. See `testing_strategy.md` § Stream contracts.
- `test_diff_patch.py` — drives the `parse-diff-patch` CLI against each captured fixture in `fixtures/diff_patch/`. See `testing_strategy.md` § diffPatch parsing.
- `test_e2e_pipeline.py` — full poll → fanout → delivery → webhook + DB. See `testing_strategy.md` § End-to-end.
- `test_idempotency.py` — duplicate runs don't duplicate notifications. See `testing_strategy.md` § Idempotency.
- `test_db_schema.py` — schema/constraint introspection. See `testing_strategy.md` § DB schema.

**Acceptance for each file:** green run; green re-run (isolation); read by a teammate.

### Phase 5 — Lockdown (~half day)

- Run the full suite 5 times in a row. Investigate any flake, fix root cause (don't add retries).
- Update `testing_strategy.md` with anything you learned that contradicts the spec — especially around field shapes or auth behavior. The spec was written from code reading, not from running tests; it may be wrong in small ways.
- Tag the commit `acceptance-suite-v1`. This is the frozen contract the Go rewrite must satisfy.
- Add a one-paragraph note to `CLAUDE.md` pointing at the suite and how to run it.

---

## Open questions / things to surface to the original author

These came up while writing the spec but don't have answers yet. Bring them back if you hit them:

1. ~~**Auth code on missing token.**~~ **Resolved:** Both missing bearer and bad token return **401**. The strategy doc's claim of 403 for missing bearer was wrong. Tests assert `in (401, 403)` to remain Go-compatible, but the Python impl uses 401 for both.
2. ~~**`/players/search` with 1-char `q`.**~~ **Resolved:** Code returns `{"players": []}` for 1-char queries (not 422). Tests now assert the empty-list behavior. The 422 row in the strategy doc's table was a spec error.
3. **Empty-list response on no active players.** `http_api.py:178` returns `{"players": []}` early. Worth confirming this behavior is intended vs. an artifact.
4. **Discord prose contract.** Locked to byte-identical (`testing_strategy.md` § Outbound Discord webhook). The em-dash is U+2014. Verify the actual emitted body matches before pinning the tests.
5. **`notification_target_type` in `users`.** Currently always `"discord"`. Tests should assert this; if the column is being kept for future webhook types, document that.

---

## Definition of done

- `uv run pytest acceptance/ -v` passes 5 runs in a row against the Python stack.
- All five test files exist and exercise every case in `testing_strategy.md` § Test cases.
- The four phase-1 preconditions are merged.
- `docker-compose.acceptance.yml` brings up a clean stack from a fresh checkout in under 2 minutes.
- A new contributor can read this doc + the strategy doc and run the suite without asking questions.

The Go rewrite begins after this is done. **Do not start any Go work as part of this task.**
