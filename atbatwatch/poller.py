"""Polling loop: fetches live MLB game state and pushes transitions via diff_engine."""

import asyncio

import redis.asyncio as aioredis

from atbatwatch.api import MlbApiProtocol
from atbatwatch.config import Config
from atbatwatch.diff_engine import process_game
from atbatwatch.games import get_todays_games


async def run_poller(
    config: Config,
    api: MlbApiProtocol,
    redis: aioredis.Redis,
) -> None:
    print(f"Poller started. Polling every {config.poll_interval_seconds}s.")
    timecodes: dict[int, str] = {}
    while True:
        try:
            games = await get_todays_games(api)
            live_games = [g for g in games if g.status == "Live"]
            if not live_games:
                print("Poller: no live games.")
            for game in live_games:
                try:
                    if game.game_pk not in timecodes:
                        live_data = await api.get_live_feed(game.game_pk)
                        timecodes[game.game_pk] = live_data["metaData"].get(
                            "timeStamp", ""
                        )
                    else:
                        (
                            live_data,
                            timecodes[game.game_pk],
                        ) = await api.get_live_feed_diff(
                            game.game_pk, timecodes[game.game_pk]
                        )
                        if live_data is None:
                            continue
                    n = await process_game(game.game_pk, live_data, redis, game)
                    if n:
                        print(f"Poller: game {game.game_pk} emitted {n} transition(s).")
                except Exception as e:
                    print(f"Poller error for game {game.game_pk}: {e}")
        except Exception as e:
            print(f"Poller error fetching schedule: {e}")
        await asyncio.sleep(config.poll_interval_seconds)
