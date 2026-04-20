from typing import cast

from atbatwatch.games import (
    extract_inning_state,
    extract_offense_state,
    get_player_status,
    get_todays_games,
    is_game_in_progress,
)
from atbatwatch.types import (
    LiveFeedResponse,
    Offense,
    PersonDetail,
    PersonSearchResult,
    ScheduleResponse,
)


class _FakeScheduleApi:
    def __init__(self, schedule_data: ScheduleResponse) -> None:
        self._data = schedule_data

    async def get_schedule(self, game_date: str | None = None) -> ScheduleResponse:
        return self._data

    async def get_live_feed(self, game_pk: int) -> LiveFeedResponse:
        raise NotImplementedError

    async def get_live_feed_diff(
        self, game_pk: int, start_timecode: str
    ) -> tuple[LiveFeedResponse | None, str]:
        raise NotImplementedError

    async def search_player(self, name: str) -> list[PersonSearchResult]:
        raise NotImplementedError

    async def get_person(
        self, player_id: int, hydrate: str | None = None
    ) -> PersonDetail:
        raise NotImplementedError


def test_extract_offense_state_in_progress(live_feed_in_progress):
    # Given: a live feed from an in-progress game
    # When: extracting offense state
    offense = extract_offense_state(live_feed_in_progress)
    # Then: batter and on-deck players are extracted
    assert offense["batter"]["id"] == 669016
    assert offense["onDeck"]["id"] == 664761


def test_extract_offense_state_missing_returns_empty():
    # Given: an empty live feed
    # When: extracting offense state
    # Then: empty dict is returned
    assert extract_offense_state(cast(LiveFeedResponse, {})) == {}


def test_extract_inning_state_in_progress(live_feed_in_progress):
    # Given: a live feed from an in-progress game
    # When: extracting inning state
    inning, half, outs = extract_inning_state(live_feed_in_progress)
    # Then: inning, half, and outs are extracted correctly
    assert inning > 0
    assert half in ("Top", "Bot")
    assert 0 <= outs <= 3


def test_extract_inning_state_missing_returns_zeros():
    # Given: an empty live feed
    # When: extracting inning state
    # Then: zeros are returned
    inning, half, outs = extract_inning_state(cast(LiveFeedResponse, {}))
    assert (inning, half, outs) == (0, "", 0)


def test_is_game_in_progress_true(live_feed_in_progress):
    # Given: a live feed from an in-progress game
    # When: checking if game is in progress
    # Then: True is returned
    assert is_game_in_progress(live_feed_in_progress) is True


def test_is_game_in_progress_false_warmup(live_feed_warmup):
    # Given: a live feed from warmup
    # When: checking if game is in progress
    # Then: False is returned
    assert is_game_in_progress(live_feed_warmup) is False


def test_get_player_status_batting():
    # Given: offense with player as batter
    offense = cast(Offense, {"batter": {"id": 100}, "onDeck": {"id": 200}})
    # When: getting player status for batter
    # Then: "batting" is returned
    assert get_player_status(100, offense) == "batting"


def test_get_player_status_on_deck():
    # Given: offense with player on deck
    offense = cast(Offense, {"batter": {"id": 100}, "onDeck": {"id": 200}})
    # When: getting player status for on-deck player
    # Then: "on_deck" is returned
    assert get_player_status(200, offense) == "on_deck"


def test_get_player_status_other():
    # Given: offense with player neither batting nor on-deck
    offense = cast(Offense, {"batter": {"id": 100}, "onDeck": {"id": 200}})
    # When: getting player status for different player
    # Then: "other" is returned
    assert get_player_status(999, offense) == "other"


def test_get_player_status_empty_offense():
    # Given: empty offense state
    # When: getting player status
    # Then: "other" is returned
    assert get_player_status(100, cast(Offense, {})) == "other"


async def test_get_todays_games_parses_schedule_fixture(schedule_fixture):
    # Given: a schedule fixture
    api = _FakeScheduleApi(schedule_fixture)
    # When: getting today's games
    games = await get_todays_games(api)
    # Then: all games are parsed with required fields
    assert len(games) == 15
    assert all(g.game_pk > 0 for g in games)
    assert all(g.home_team_name for g in games)
    assert all(g.away_team_name for g in games)


async def test_get_todays_games_status_field(schedule_fixture):
    # Given: a schedule fixture
    api = _FakeScheduleApi(schedule_fixture)
    # When: getting today's games
    games = await get_todays_games(api)
    # Then: game statuses are extracted
    statuses = {g.status for g in games}
    assert "Final" in statuses


async def test_get_todays_games_empty_dates():
    # Given: schedule with empty dates
    api = _FakeScheduleApi(cast(ScheduleResponse, {"dates": []}))
    # When: getting today's games
    games = await get_todays_games(api)
    # Then: empty list is returned
    assert games == []


async def test_get_todays_games_missing_dates_key():
    # Given: schedule with missing dates key
    api = _FakeScheduleApi(cast(ScheduleResponse, {}))
    # When: getting today's games
    games = await get_todays_games(api)
    # Then: empty list is returned
    assert games == []
