import asyncio

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atbatwatch.api import MlbApiProtocol
from atbatwatch.config import Config
from atbatwatch.games import (
    GameInfo,
    extract_inning_state,
    extract_offense_state,
    get_player_status,
    get_todays_games,
    is_game_in_progress,
)
from atbatwatch.notifier import ConsoleNotifier, DiscordNotifier, Notifier
from atbatwatch.players import ResolvedPlayer
from atbatwatch.repos.follows import get_followers
from atbatwatch.types import LiveFeedResponse

_GAME_STATE_TTL = 86400  # 24 h — expires well after any game ends


async def _check_game(
    game_pk: int,
    live_data: LiveFeedResponse,
    resolved_players: list[ResolvedPlayer],
    redis: aioredis.Redis,
    notifier: Notifier,
    game_info: GameInfo,
) -> None:
    if not is_game_in_progress(live_data):
        return
    offense = extract_offense_state(live_data)
    inning, inning_half, outs = extract_inning_state(live_data)
    state_key = f"game:{game_pk}:players"
    for player in resolved_players:
        status = get_player_status(player.player_id, offense)
        # pyrefly: ignore [not-async]
        last_status = await redis.hget(state_key, str(player.player_id)) or "other"
        if status != last_status and status in ("batting", "on_deck"):
            await notifier.notify(
                player.player_id,
                player.full_name,
                status,
                game_info,
                inning,
                inning_half,
                outs,
            )
        # pyrefly: ignore [not-async]
        await redis.hset(state_key, str(player.player_id), status)
    await redis.expire(state_key, _GAME_STATE_TTL)


class _DBFanOutNotifier:
    """In production: fans out to each follower's Discord webhook + always logs to console."""

    def __init__(
        self, session: AsyncSession, console: ConsoleNotifier | None = None
    ) -> None:
        self._session = session
        self._console = console or ConsoleNotifier()

    async def notify(
        self,
        player_id: int,
        player_name: str,
        status: str,
        game_info: GameInfo,
        inning: int,
        inning_half: str,
        outs: int,
    ) -> None:
        followers = await get_followers(player_id, self._session)
        for user in followers:
            discord = DiscordNotifier(user.notification_target_id)
            try:
                await discord.notify(
                    player_id, player_name, status, game_info, inning, inning_half, outs
                )
            except Exception as e:
                print(f"Discord delivery failed for user {user.user_id}: {e}")
        await self._console.notify(
            player_id, player_name, status, game_info, inning, inning_half, outs
        )


async def run_fixture(
    fixture_data: LiveFeedResponse,
    resolved_players: list[ResolvedPlayer],
    notifier: Notifier,
    redis: aioredis.Redis,
) -> None:
    """Single-pass check against a saved fixture for offline structure validation."""
    game_pk = fixture_data.get("gamePk", 0)
    teams = fixture_data.get("gameData", {}).get("teams", {})
    game_info = GameInfo(
        game_pk=game_pk,
        home_team_id=teams.get("home", {}).get("id", 0),
        home_team_name=teams.get("home", {}).get("name", "Home"),
        away_team_id=teams.get("away", {}).get("id", 0),
        away_team_name=teams.get("away", {}).get("name", "Away"),
        status="Live",
    )
    await _check_game(
        game_pk, fixture_data, resolved_players, redis, notifier, game_info
    )


async def run(
    config: Config,
    resolved_players: list[ResolvedPlayer],
    api: MlbApiProtocol,
    redis: aioredis.Redis,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    console = ConsoleNotifier()
    print(
        f"Watching {len(resolved_players)} player(s). Polling every {config.poll_interval_seconds}s."
    )
    for p in resolved_players:
        print(f"  - {p.full_name} (id={p.player_id})")

    timecodes: dict[int, str] = {}
    while True:
        try:
            games = await get_todays_games(api)
            live_games = [g for g in games if g.status == "Live"]

            if not live_games:
                print("No live games.")
            else:
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

                        if session_factory is not None:
                            async with session_factory() as session:
                                notifier: Notifier = _DBFanOutNotifier(session, console)
                                await _check_game(
                                    game.game_pk,
                                    live_data,
                                    resolved_players,
                                    redis,
                                    notifier,
                                    game,
                                )
                        else:
                            await _check_game(
                                game.game_pk,
                                live_data,
                                resolved_players,
                                redis,
                                console,
                                game,
                            )
                    except Exception as e:
                        print(f"Error fetching game {game.game_pk}: {e}")
        except Exception as e:
            print(f"Error fetching schedule: {e}")

        await asyncio.sleep(config.poll_interval_seconds)
