# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                          # install / sync dependencies
uv sync --group dev              # install dev tools (ruff, pyrefly)
uv run atbatwatch <command>      # run any CLI command

# Verification
uv run ruff check --fix .        # lint + auto-fix
uv run ruff format .             # format in place
uv run pyrefly check             # type check
uv run pytest                    # run tests

# Key CLI commands
uv run atbatwatch lookup "Player Name"           # resolve name → MLB player ID
uv run atbatwatch games                          # today's schedule (live API)
uv run atbatwatch games --fixture <path>         # from saved schedule JSON
uv run atbatwatch state <game_pk>                # current batter/on-deck/in-hole
uv run atbatwatch state <game_pk> --fixture <path>
uv run atbatwatch capture <game_pk>              # save live feed to fixtures/
uv run atbatwatch capture-schedule               # save today's schedule to fixtures/
uv run atbatwatch watch                          # live polling loop (Ctrl+C to stop)
uv run atbatwatch watch --fixture <path>         # single-pass offline validation
```

## Architecture

The app polls the undocumented MLB Stats API (`ws.statsapi.mlb.com`) for live game state and notifies users when watched players are at bat or on deck.

**Data flow:**
1. `config.toml` → player names/IDs loaded by `config.py`
2. `players.py` resolves names → player IDs via MLB `/people/search` API, cached in memory
3. `games.py` fetches today's schedule and extracts offense state from live feeds
4. `watcher.py` polls all live games every N seconds, tracks `(game_pk, player_id) → last_status`
5. Notifications fire only on status transitions to `"batting"` or `"on_deck"` — no duplicates within the same game
6. `notifier.py` `ConsoleNotifier` prints to stdout; the `Notifier` protocol is the swap point for Discord/email

**Key API paths:**
- Schedule: `GET /api/v1/schedule?sportId=1&date=MM/DD/YYYY&hydrate=team,linescore`
- Live feed: `GET /api/v1.1/game/{gamePk}/feed/live`
- Player search: `GET /api/v1/people/search?names={name}` — returns `people[]` with `id`, `fullName`, `active`
- Offense state lives at: `liveData.linescore.offense` with keys `batter`, `onDeck`, `inHole` (each `{id, fullName, link}`)

**State deduplication:** `watcher.py` keeps `state: dict[tuple[int, int], str]` keyed by `(game_pk, player_id)`. New `game_pk` = fresh state, so there's no carryover between games. Status values: `"batting"`, `"on_deck"`, `"other"`.

**Offline testing:** `fixtures/` holds captured JSON snapshots organized by API endpoint (`live_feed/`, `schedule/`, `people_search/`, `person/`). All `state`, `games`, and `watch` commands accept `--fixture <path>` to run against saved data instead of the live API.

**Player disambiguation:** If name search returns multiple active players, the error message lists their IDs. Add `player_id = <id>` to the `[[players]]` entry in `config.toml` to bypass search.

## config.toml

```toml
poll_interval_seconds = 10

[[players]]
name = "Shohei Ohtani"

[[players]]
name = "Luis García Jr."
player_id = 671277   # required when name search is ambiguous (Nationals 2B; 472610 is also active)
```
