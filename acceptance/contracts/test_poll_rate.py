"""Contract tests for schedule-aware polling rate.

These tests verify that the poller makes far fewer schedule API calls when no
games are live or imminent, compared to during live games.  They run the poller
loop (run-poller) in a background subprocess for a fixed window, then inspect
the stub's request counters.

All assertions are against external wire surfaces only (stub /admin/stats).
No atbatwatch imports; safe for a future rewrite.
"""

import json
import subprocess
import time
from datetime import UTC, datetime, timedelta

import httpx

from acceptance.conftest import _COMPOSE_FILE, _PROJECT_ROOT, MLB_STUB_BASE_URL

# Game PK and live fixtures shared with e2e tests
_LIVE_GAME_PK = 823475
_LIVE_SCHEDULE_PATH = "schedule/schedule_823475_live.json"
_LIVE_FEED_PATH = "live_feed/in_progress_game_823475.json"

# How long to let the poller loop run per test (seconds)
_RUN_SECONDS = 8

# Poll interval injected into the poller container for rate tests
_FAST_INTERVAL = "1"


def _schedule_json(games: list[dict]) -> str:
    """Minimal MLB schedule response wrapping a list of game dicts."""
    return json.dumps(
        {
            "totalItems": len(games),
            "totalGames": len(games),
            "totalGamesInProgress": 0,
            "dates": [
                {
                    "date": datetime.now(UTC).strftime("%Y-%m-%d"),
                    "totalGames": len(games),
                    "games": games,
                }
            ],
        }
    )


def _future_game(minutes_from_now: int, status: str = "Scheduled") -> dict:
    """Minimal game dict with a gameDate offset from now."""
    game_time = datetime.now(UTC) + timedelta(minutes=minutes_from_now)
    return {
        "gamePk": 999999,
        "gameDate": game_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": {"abstractGameState": status, "detailedState": status},
        "teams": {
            "home": {"team": {"id": 1, "name": "Home Team"}},
            "away": {"team": {"id": 2, "name": "Away Team"}},
        },
    }


def _run_poller(seconds: int, env: dict | None = None) -> None:
    """Start the poller loop in the background, let it run for `seconds`, then stop it."""
    env_args: list[str] = []
    for k, v in (env or {}).items():
        env_args += ["-e", f"{k}={v}"]

    cmd = [
        "docker",
        "compose",
        "-f",
        str(_COMPOSE_FILE),
        "--profile",
        "workers",
        "run",
        "--rm",
        "-T",
        *env_args,
        "poller",
        "atbatwatch",
        "run-poller",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(_PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(seconds)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _stub_stats() -> dict:
    return httpx.get(f"{MLB_STUB_BASE_URL}/admin/stats", timeout=5).json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_idle_day_low_schedule_call_rate(stack, mlb_stub):
    """When no games are live and none are imminent, the schedule endpoint is
    called only for the initial look-ahead (today + tomorrow), then the poller
    sleeps for up to 2 hours.  In an 8-second window it must make at most 3
    schedule calls total."""
    # All terminal games — nothing to wake up for today or tomorrow.
    mlb_stub.configure(
        schedule_json=_schedule_json(
            [_future_game(minutes_from_now=-60, status="Final")]
        )
    )

    _run_poller(_RUN_SECONDS, env={"POLL_INTERVAL_SECONDS": _FAST_INTERVAL})

    calls = _stub_stats()["schedule"]
    assert calls <= 3, (
        f"Expected ≤3 schedule calls when no live/future games, got {calls}. "
        "Poller may not be sleeping adaptively."
    )


def test_future_game_long_sleep(stack, mlb_stub):
    """When the next game is 60 minutes away, the poller should sleep ~45 minutes
    (60min - 15min lead = 45min > imminentWindow of 30min), so it makes only the
    initial schedule call(s) and does not re-poll within the 8-second test window.
    Using 60min (not 45min) gives a 15-minute margin above the imminentWindow
    boundary so Docker startup time (~5s) cannot tip us into the normal-rate path."""
    mlb_stub.configure(
        schedule_json=_schedule_json([_future_game(minutes_from_now=60)])
    )

    _run_poller(_RUN_SECONDS, env={"POLL_INTERVAL_SECONDS": _FAST_INTERVAL})

    calls = _stub_stats()["schedule"]
    assert calls <= 3, (
        f"Expected ≤3 schedule calls with game 60min away, got {calls}. "
        "Poller may not be honoring the long-sleep path."
    )


def test_live_game_normal_poll_rate(stack, mlb_stub):
    """When a game is live, the poller must poll at roughly the normal interval.
    With POLL_INTERVAL_SECONDS=1 and an 8-second window we expect ≥5 schedule
    calls (allowing for startup and shutdown overhead)."""
    mlb_stub.configure(
        schedule_path=_LIVE_SCHEDULE_PATH,
        game_pk=_LIVE_GAME_PK,
        live_feed_path=_LIVE_FEED_PATH,
    )

    _run_poller(_RUN_SECONDS, env={"POLL_INTERVAL_SECONDS": _FAST_INTERVAL})

    calls = _stub_stats()["schedule"]
    assert calls >= 5, (
        f"Expected ≥5 schedule calls during live game (interval=1s, window=8s), got {calls}. "
        "Poller may be sleeping too long during live games."
    )
