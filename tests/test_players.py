import pytest

from atbatwatch.api import MlbApiProtocol
from atbatwatch.config import PlayerConfig
from atbatwatch.players import (
    AmbiguousPlayerError,
    ResolvedPlayer,
    resolve_all,
    resolve_player,
)
from atbatwatch.types import (
    LiveFeedResponse,
    PersonDetail,
    PersonSearchResult,
    ScheduleResponse,
)


class _FakeApi:
    """Minimal MlbApiProtocol implementation for tests; no httpx involved."""

    def __init__(
        self, search_results: list, person_details: dict | None = None
    ) -> None:
        self._search = search_results
        self._persons = person_details or {}

    async def get_schedule(self, game_date: str | None = None) -> ScheduleResponse:
        raise NotImplementedError

    async def get_live_feed(self, game_pk: int) -> LiveFeedResponse:
        raise NotImplementedError

    async def get_live_feed_diff(
        self, game_pk: int, start_timecode: str
    ) -> tuple[LiveFeedResponse | None, str]:
        raise NotImplementedError

    async def search_player(self, name: str) -> list[PersonSearchResult]:
        return self._search

    async def get_person(
        self, player_id: int, hydrate: str | None = None
    ) -> PersonDetail:
        return self._persons.get(player_id, {"fullName": f"Player {player_id}"})  # type: ignore[return-value]


def _fake_api(
    search_results: list, person_details: dict | None = None
) -> MlbApiProtocol:
    return _FakeApi(search_results, person_details)  # type: ignore[return-value]


async def test_resolve_by_explicit_id():
    # Given: a player config with explicit ID
    api = _fake_api([], {660271: {"fullName": "Shohei Ohtani", "id": 660271}})
    cfg = PlayerConfig(name="Shohei Ohtani", player_id=660271)
    # When: resolving the player
    result = await resolve_player(cfg, api)
    # Then: player is resolved with correct ID and name
    assert result == ResolvedPlayer(player_id=660271, full_name="Shohei Ohtani")


async def test_resolve_by_name_single_active():
    # Given: a search returns one active player
    api = _fake_api(
        [{"id": 660271, "fullName": "Shohei Ohtani", "active": True}],
        {
            660271: {
                "fullName": "Shohei Ohtani",
                "currentTeam": {"id": 119, "name": "Dodgers"},
                "primaryPosition": {"abbreviation": "DH"},
            }
        },
    )
    cfg = PlayerConfig(name="Shohei Ohtani", player_id=None)
    # When: resolving the player by name
    result = await resolve_player(cfg, api)
    # Then: player is resolved correctly
    assert result.player_id == 660271
    assert result.full_name == "Shohei Ohtani"


async def test_resolve_no_active_player_raises():
    # Given: search returns no active player
    api = _fake_api([{"id": 1, "fullName": "Ghost", "active": False}])
    cfg = PlayerConfig(name="Ghost", player_id=None)
    # When: resolving the player
    # Then: ValueError is raised
    with pytest.raises(ValueError, match="No active MLB player"):
        await resolve_player(cfg, api)


async def test_resolve_ambiguous_raises():
    # Given: search returns multiple active players with same name
    api = _fake_api(
        [
            {"id": 1, "fullName": "Luis Garcia", "active": True},
            {"id": 2, "fullName": "Luis Garcia", "active": True},
        ],
        {
            1: {
                "fullName": "Luis Garcia",
                "currentTeam": {"id": 10, "name": "Team A"},
                "primaryPosition": {"abbreviation": "2B"},
            },
            2: {
                "fullName": "Luis Garcia",
                "currentTeam": {"id": 20, "name": "Team B"},
                "primaryPosition": {"abbreviation": "SS"},
            },
        },
    )
    cfg = PlayerConfig(name="Luis Garcia", player_id=None)
    # When: resolving the player
    # Then: AmbiguousPlayerError is raised
    with pytest.raises(AmbiguousPlayerError):
        await resolve_player(cfg, api)


async def test_resolve_all_success():
    # Given: multiple player configs with explicit IDs
    api = _fake_api(
        [],
        {
            100: {"fullName": "Aaron Judge", "id": 100},
            200: {"fullName": "Mookie Betts", "id": 200},
        },
    )
    configs = [
        PlayerConfig(name="Aaron Judge", player_id=100),
        PlayerConfig(name="Mookie Betts", player_id=200),
    ]
    # When: resolving all players
    results = await resolve_all(configs, api)
    # Then: all players are resolved correctly
    assert len(results) == 2
    assert {r.player_id for r in results} == {100, 200}


async def test_resolve_all_one_error_raises():
    # Given: one config that cannot be resolved
    api = _fake_api([{"id": 1, "fullName": "Ghost", "active": False}])
    configs = [PlayerConfig(name="Ghost", player_id=None)]
    # When: resolving all players
    # Then: ValueError is raised
    with pytest.raises(ValueError, match="No active MLB player"):
        await resolve_all(configs, api)


async def test_resolve_all_collects_multiple_errors():
    # Given: multiple configs that cannot be resolved
    api = _fake_api([{"id": 1, "fullName": "Nobody", "active": False}])
    configs = [
        PlayerConfig(name="Nobody", player_id=None),
        PlayerConfig(name="Also Nobody", player_id=None),
    ]
    # When: resolving all players
    # Then: ValueError is raised with all error messages
    with pytest.raises(ValueError) as exc_info:
        await resolve_all(configs, api)
    msg = str(exc_info.value)
    assert "Nobody" in msg
    assert "Also Nobody" in msg


async def test_resolve_all_ambiguous_formats_candidates_in_error():
    # Given: search returns ambiguous results
    api = _fake_api(
        [
            {"id": 1, "fullName": "Luis Garcia", "active": True},
            {"id": 2, "fullName": "Luis Garcia", "active": True},
        ],
        {
            1: {
                "fullName": "Luis Garcia",
                "currentTeam": {"id": 10, "name": "Team A"},
                "primaryPosition": {"abbreviation": "2B"},
            },
            2: {
                "fullName": "Luis Garcia",
                "currentTeam": {"id": 20, "name": "Team B"},
                "primaryPosition": {"abbreviation": "SS"},
            },
        },
    )
    configs = [PlayerConfig(name="Luis Garcia Jr.", player_id=None)]
    # When: resolving the player
    # Then: error message includes player_id and position info
    with pytest.raises(ValueError) as exc_info:
        await resolve_all(configs, api)
    msg = str(exc_info.value)
    assert "player_id" in msg
    assert "2B" in msg or "SS" in msg
