"""Diffs live game state against Redis-cached offense positions.

Emits transition events to the events:transitions Redis Stream whenever the
player occupying batter or on-deck position changes.
"""

import os
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis

from atbatwatch.games import (
    GameInfo,
    extract_inning_state,
    extract_offense_state,
    is_game_in_progress,
)
from atbatwatch.types import LiveFeedResponse

TRANSITIONS_STREAM = "events:transitions"
_GAME_STATE_TTL = 86400  # 24 h


def _now_iso() -> str:
    fixed = os.environ.get("ATBATWATCH_FIXED_NOW")
    if fixed:
        return fixed
    return datetime.now(timezone.utc).isoformat()


async def process_game(
    game_pk: int,
    live_data: LiveFeedResponse,
    redis: aioredis.Redis,
    game_info: GameInfo,
) -> int:
    """Diff live_data against cached offense state; XADD transitions. Returns events emitted."""
    if not is_game_in_progress(live_data):
        return 0

    offense = extract_offense_state(live_data)
    if not offense:
        return 0

    inning, inning_half, outs = extract_inning_state(live_data)
    state_key = f"game:{game_pk}:offense"
    # pyrefly: ignore [not-async]
    prev = await redis.hgetall(state_key)

    events_emitted = 0
    positions = [
        ("batter", "at_bat", offense.get("batter")),
        ("onDeck", "on_deck", offense.get("onDeck")),
    ]
    new_state: dict[str, str] = {}
    for pos_key, stream_state, player_data in positions:
        if player_data is None:
            continue
        player_id = str(player_data.get("id", ""))
        player_name = str(player_data.get("fullName", ""))
        if not player_id:
            continue
        new_state[pos_key] = player_id
        if player_id != prev.get(pos_key, ""):
            event_id = str(uuid.uuid4())
            # pyrefly: ignore [not-async]
            await redis.xadd(
                TRANSITIONS_STREAM,
                {
                    "event_id": event_id,
                    "game_id": str(game_pk),
                    "player_id": player_id,
                    "player_name": player_name,
                    "state": stream_state,
                    "home_team_id": str(game_info.home_team_id),
                    "home_team_name": game_info.home_team_name,
                    "away_team_id": str(game_info.away_team_id),
                    "away_team_name": game_info.away_team_name,
                    "inning": str(inning),
                    "inning_half": inning_half,
                    "outs": str(outs),
                    "occurred_at": _now_iso(),
                },
            )
            events_emitted += 1

    if new_state:
        # pyrefly: ignore [not-async]
        await redis.hset(state_key, mapping=new_state)
        await redis.expire(state_key, _GAME_STATE_TTL)

    return events_emitted
