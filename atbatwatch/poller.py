"""Polling loop: fetches live MLB game state and pushes transitions via diff_engine."""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis

from atbatwatch.api import MlbApiProtocol
from atbatwatch.config import Config
from atbatwatch.diff_engine import process_game
from atbatwatch.games import get_todays_games


async def _get_live_games(api: MlbApiProtocol):
    """Return live games from today's schedule, falling back to yesterday's if none found.

    Late-night West Coast games can still be in progress after midnight Eastern, at which
    point _eastern_date() has already rolled to the next day and the schedule call returns
    no games. Checking yesterday catches those stragglers.
    """
    games = await get_todays_games(api)
    live_games = [g for g in games if g.status == "Live"]
    if not live_games:
        now_et = datetime.now(ZoneInfo("America/New_York"))
        # Only look back if it's early morning (games can't still be live after 6 AM ET)
        if now_et.hour < 6:
            yesterday = (now_et - timedelta(days=1)).strftime("%m/%d/%Y")
            yesterday_games = await get_todays_games(api, game_date=yesterday)
            live_games = [g for g in yesterday_games if g.status == "Live"]
    return live_games


async def _poll_iteration(
    config: Config,
    api: MlbApiProtocol,
    redis: aioredis.Redis,
    timecodes: dict[int, str],
) -> None:
    """Run one poll cycle: fetch live games, diff against cached state, emit transitions."""
    live_games = await _get_live_games(api)
    if not live_games:
        print("Poller: no live games.")
    for game in live_games:
        try:
            if game.game_pk not in timecodes:
                live_data = await api.get_live_feed(game.game_pk)
                timecodes[game.game_pk] = live_data["metaData"].get("timeStamp", "")
            else:
                live_data, timecodes[game.game_pk] = await api.get_live_feed_diff(
                    game.game_pk, timecodes[game.game_pk]
                )
                if live_data is None:
                    continue
            n = await process_game(game.game_pk, live_data, redis, game)
            if n:
                print(f"Poller: game {game.game_pk} emitted {n} transition(s).")
        except Exception as e:
            print(f"Poller error for game {game.game_pk}: {e}")


async def run_poller(
    config: Config,
    api: MlbApiProtocol,
    redis: aioredis.Redis,
) -> None:
    print(f"Poller started. Polling every {config.poll_interval_seconds}s.")
    timecodes: dict[int, str] = {}
    while True:
        try:
            await _poll_iteration(config, api, redis, timecodes)
        except Exception as e:
            print(f"Poller error fetching schedule: {e}")
        await asyncio.sleep(config.poll_interval_seconds)
