import copy

from atbatwatch.diff_engine import TRANSITIONS_STREAM, process_game
from atbatwatch.games import GameInfo

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
