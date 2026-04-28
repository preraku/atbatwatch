"""Tests for the midnight-ET rollover fallback in _get_live_games."""

from datetime import datetime, timezone
from typing import cast

import time_machine

from atbatwatch.poller import _get_live_games
from atbatwatch.types import LiveFeedResponse, PersonDetail, PersonSearchResult, ScheduleResponse


def _make_schedule(status: str, game_pk: int = 800001) -> ScheduleResponse:
    return cast(
        ScheduleResponse,
        {
            "dates": [
                {
                    "games": [
                        {
                            "gamePk": game_pk,
                            "teams": {
                                "home": {"team": {"id": 1, "name": "Home"}},
                                "away": {"team": {"id": 2, "name": "Away"}},
                            },
                            "status": {"abstractGameState": status},
                        }
                    ]
                }
            ]
        },
    )


class _DateAwareApi:
    """Returns different schedules for today vs yesterday to test the fallback."""

    def __init__(self, today_status: str, yesterday_status: str) -> None:
        self._today_status = today_status
        self._yesterday_status = yesterday_status
        self.call_dates: list[str | None] = []

    async def get_schedule(self, game_date: str | None = None) -> ScheduleResponse:
        self.call_dates.append(game_date)
        # None means "use today" — first call in _get_live_games passes None
        if game_date is None:
            return _make_schedule(self._today_status, game_pk=900001)
        return _make_schedule(self._yesterday_status, game_pk=800001)

    async def get_live_feed(self, game_pk: int) -> LiveFeedResponse:
        raise NotImplementedError

    async def get_live_feed_diff(
        self, game_pk: int, start_timecode: str
    ) -> tuple[LiveFeedResponse | None, str]:
        raise NotImplementedError

    async def search_player(self, name: str) -> list[PersonSearchResult]:
        raise NotImplementedError

    async def get_person(self, player_id: int, hydrate: str | None = None) -> PersonDetail:
        raise NotImplementedError


# ── Normal daytime: no fallback needed ───────────────────────────────────────


@time_machine.travel(datetime(2026, 4, 27, 18, 0, 0, tzinfo=timezone.utc))  # 2 PM ET
async def test_live_game_found_in_todays_schedule():
    # Given: a live game in today's schedule at midday
    api = _DateAwareApi(today_status="Live", yesterday_status="Final")
    # When: fetching live games
    games = await _get_live_games(api)
    # Then: the live game is returned and yesterday's schedule is never queried
    assert len(games) == 1
    assert games[0].game_pk == 900001
    assert len(api.call_dates) == 1


@time_machine.travel(datetime(2026, 4, 27, 18, 0, 0, tzinfo=timezone.utc))  # 2 PM ET
async def test_no_fallback_during_daytime_even_if_no_live_games():
    # Given: no live games in today's schedule at midday
    api = _DateAwareApi(today_status="Preview", yesterday_status="Live")
    # When: fetching live games
    games = await _get_live_games(api)
    # Then: no games returned and yesterday is NOT checked (hour >= 6 ET)
    assert games == []
    assert len(api.call_dates) == 1


# ── Past midnight ET: fallback to yesterday ───────────────────────────────────


@time_machine.travel(datetime(2026, 4, 28, 4, 11, 0, tzinfo=timezone.utc))  # 00:11 AM ET
async def test_past_midnight_et_falls_back_to_yesterday_when_no_live_games_today():
    # Given: no live games on today's (Apr 28) schedule; late games still live on Apr 27
    # This is the exact scenario that triggered the bug (games 823311/823961/824609)
    api = _DateAwareApi(today_status="Preview", yesterday_status="Live")
    # When: fetching live games
    games = await _get_live_games(api)
    # Then: yesterday's live game is returned
    assert len(games) == 1
    assert games[0].game_pk == 800001
    # And both schedules were queried
    assert len(api.call_dates) == 2
    assert api.call_dates[0] is None       # today
    assert api.call_dates[1] == "04/27/2026"  # yesterday (ET date)


@time_machine.travel(datetime(2026, 4, 28, 4, 11, 0, tzinfo=timezone.utc))  # 00:11 AM ET
async def test_past_midnight_et_uses_todays_games_if_live():
    # Given: a live game already on today's schedule (rare but possible)
    api = _DateAwareApi(today_status="Live", yesterday_status="Live")
    # When: fetching live games
    games = await _get_live_games(api)
    # Then: today's game is returned and yesterday is NOT checked
    assert len(games) == 1
    assert games[0].game_pk == 900001
    assert len(api.call_dates) == 1


@time_machine.travel(datetime(2026, 4, 28, 4, 11, 0, tzinfo=timezone.utc))  # 00:11 AM ET
async def test_past_midnight_et_no_games_anywhere():
    # Given: no live games on either day's schedule
    api = _DateAwareApi(today_status="Final", yesterday_status="Final")
    # When: fetching live games
    games = await _get_live_games(api)
    # Then: empty list returned
    assert games == []


# ── 6 AM ET cutoff: no fallback after that ───────────────────────────────────


@time_machine.travel(datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc))  # 6 AM ET exactly
async def test_no_fallback_at_or_after_6am_et():
    # Given: no live games today, but live games yesterday
    api = _DateAwareApi(today_status="Preview", yesterday_status="Live")
    # When: fetching live games at 6 AM ET
    games = await _get_live_games(api)
    # Then: no fallback — any game from yesterday is definitively over by 6 AM
    assert games == []
    assert len(api.call_dates) == 1
