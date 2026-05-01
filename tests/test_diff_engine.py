import copy
import os
from typing import cast

import pytest

from atbatwatch.diff_engine import TRANSITIONS_STREAM, process_game
from atbatwatch.games import GameInfo, extract_inning_state
from atbatwatch.types import LiveFeedResponse

GAME_PK = 823475
BATTER_ID = 669016  # Brandon Marsh — batter in fixture
ON_DECK_ID = 664761  # Alec Bohm — on-deck in fixture


def _game_info(game_pk: int = GAME_PK) -> GameInfo:
    return GameInfo(
        game_pk=game_pk,
        home_team_id=143,
        home_team_name="Philadelphia Phillies",
        away_team_id=144,
        away_team_name="Atlanta Braves",
        status="Live",
    )


async def test_emits_batter_and_on_deck_events(live_feed_in_progress, fake_redis):
    # Given: a live feed with active players
    # When: processing the game
    n = await process_game(GAME_PK, live_feed_in_progress, fake_redis, _game_info())
    # Then: events are emitted for batter and on-deck
    assert n == 2
    events = await fake_redis.xread({TRANSITIONS_STREAM: "0"})
    assert len(events) == 1
    _stream, messages = events[0]
    assert len(messages) == 2
    states = {m[1]["state"] for m in messages}
    assert states == {"at_bat", "on_deck"}


async def test_player_ids_correct(live_feed_in_progress, fake_redis):
    # Given: a live feed with known players
    # When: processing the game
    await process_game(GAME_PK, live_feed_in_progress, fake_redis, _game_info())
    # Then: events contain correct player IDs
    events = await fake_redis.xread({TRANSITIONS_STREAM: "0"})
    _stream, messages = events[0]
    by_state = {m[1]["state"]: m[1] for m in messages}
    assert by_state["at_bat"]["player_id"] == str(BATTER_ID)
    assert by_state["on_deck"]["player_id"] == str(ON_DECK_ID)


async def test_no_duplicate_on_second_poll(live_feed_in_progress, fake_redis):
    # Given: first poll already processed
    await process_game(GAME_PK, live_feed_in_progress, fake_redis, _game_info())
    # When: processing the same feed again
    n2 = await process_game(GAME_PK, live_feed_in_progress, fake_redis, _game_info())
    # Then: no new events are emitted
    assert n2 == 0


async def test_new_batter_emits_only_one_event(live_feed_in_progress, fake_redis):
    # Given: first poll with initial batter
    await process_game(GAME_PK, live_feed_in_progress, fake_redis, _game_info())
    # When: batter changes in next poll
    next_feed = copy.deepcopy(live_feed_in_progress)
    offense = next_feed["liveData"]["linescore"]["offense"]
    offense["batter"] = {"id": 99001, "fullName": "New Batter", "link": ""}
    n2 = await process_game(GAME_PK, next_feed, fake_redis, _game_info())
    # Then: only one event for new batter
    assert n2 == 1
    events = await fake_redis.xread({TRANSITIONS_STREAM: "0"})
    _stream, messages = events[0]
    assert len(messages) == 3
    last_state = messages[-1][1]["state"]
    assert last_state == "at_bat"
    assert messages[-1][1]["player_id"] == "99001"


async def test_warmup_game_emits_nothing(live_feed_warmup, fake_redis):
    # Given: a warmup game
    game_pk = live_feed_warmup.get("gamePk", 0)
    # When: processing the game
    n = await process_game(game_pk, live_feed_warmup, fake_redis, _game_info(game_pk))
    # Then: no events are emitted
    assert n == 0
    assert not await fake_redis.exists(TRANSITIONS_STREAM)


async def test_event_fields_populated(live_feed_in_progress, fake_redis):
    # Given: a live feed in progress
    # When: processing the game
    await process_game(GAME_PK, live_feed_in_progress, fake_redis, _game_info())
    # Then: all required event fields are populated
    events = await fake_redis.xread({TRANSITIONS_STREAM: "0"})
    _stream, messages = events[0]
    field = messages[0][1]
    assert field["game_id"] == str(GAME_PK)
    assert field["home_team_name"] == "Philadelphia Phillies"
    assert field["away_team_name"] == "Atlanta Braves"
    assert "event_id" in field
    assert "occurred_at" in field
    assert "inning" in field


# --- extract_inning_state: between-half-inning display fix ---


def _feed_with_linescore(inning: int, is_top: bool, outs: int) -> LiveFeedResponse:
    return cast(
        LiveFeedResponse,
        {
            "gameData": {"status": {"detailedState": "In Progress"}},
            "liveData": {
                "linescore": {
                    "currentInning": inning,
                    "isTopInning": is_top,
                    "outs": outs,
                    "offense": {
                        "batter": {"id": 1, "fullName": "P1", "link": ""},
                        "onDeck": {"id": 2, "fullName": "P2", "link": ""},
                    },
                }
            },
        },
    )


@pytest.mark.parametrize(
    "inning,is_top,outs,expected",
    [
        # Mid-inning: values passed through unchanged
        (4, True, 1, (4, "Top", 1)),
        (4, False, 2, (4, "Bot", 2)),
        # 3 outs at top: advance to bottom of same inning
        (4, True, 3, (4, "Bot", 0)),
        # 3 outs at bottom: advance to top of next inning
        (4, False, 3, (5, "Top", 0)),
    ],
)
def test_extract_inning_state_advances_on_three_outs(inning, is_top, outs, expected):
    # Given: a linescore at a specific inning/half/outs
    feed = _feed_with_linescore(inning, is_top, outs)
    # When: extracting the inning state
    # Then: 3-outs advances to next half; mid-inning values are unchanged
    assert extract_inning_state(feed) == expected


async def test_three_outs_event_shows_next_half_inning(fake_redis):
    # Given: a feed with 3 outs in Top 4 (between half-innings)
    feed = _feed_with_linescore(inning=4, is_top=True, outs=3)
    # When: processing the game
    await process_game(GAME_PK, feed, fake_redis, _game_info())
    # Then: emitted events display Bot 4, 0 outs — not the just-finished Top 4, 3 outs
    events = await fake_redis.xread({TRANSITIONS_STREAM: "0"})
    _stream, messages = events[0]
    for _msg_id, fields in messages:
        assert fields["inning"] == "4"
        assert fields["inning_half"] == "Bot"
        assert fields["outs"] == "0"


async def test_no_duplicate_after_half_inning_transition(fake_redis):
    # Given: first poll shows Top 4, 3 outs (between halves) and emits for players 1 and 2
    feed_between = _feed_with_linescore(inning=4, is_top=True, outs=3)
    n1 = await process_game(GAME_PK, feed_between, fake_redis, _game_info())
    assert n1 == 2
    # When: second poll shows Bot 4, 0 outs with the same players still in position
    feed_new_half = _feed_with_linescore(inning=4, is_top=False, outs=0)
    n2 = await process_game(GAME_PK, feed_new_half, fake_redis, _game_info())
    # Then: no duplicate events are emitted
    assert n2 == 0


async def test_final_game_emits_nothing(live_feed_final, fake_redis):
    # Given: a completed game
    # When: processing the game
    n = await process_game(GAME_PK, live_feed_final, fake_redis, _game_info())
    # Then: no events are emitted
    assert n == 0
    assert not await fake_redis.exists(TRANSITIONS_STREAM)


async def test_fixed_now_env_var_sets_occurred_at(live_feed_in_progress, fake_redis):
    # Given: ATBATWATCH_FIXED_NOW is set
    fixed = "2026-01-01T00:00:00+00:00"
    os.environ["ATBATWATCH_FIXED_NOW"] = fixed
    try:
        await process_game(GAME_PK, live_feed_in_progress, fake_redis, _game_info())
        events = await fake_redis.xread({TRANSITIONS_STREAM: "0"})
        _stream, messages = events[0]
        for _msg_id, fields in messages:
            assert fields["occurred_at"] == fixed
    finally:
        del os.environ["ATBATWATCH_FIXED_NOW"]
