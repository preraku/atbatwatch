from dataclasses import dataclass

from atbatwatch.api import MlbApiProtocol
from atbatwatch.config import PlayerConfig
from atbatwatch.types import PersonSearchResult


@dataclass
class ResolvedPlayer:
    player_id: int
    full_name: str


@dataclass
class PlayerCandidate:
    player_id: int
    full_name: str
    position: str
    team: str


class AmbiguousPlayerError(Exception):
    def __init__(self, name: str, candidates: list[PlayerCandidate]):
        self.name = name
        self.candidates = candidates
        super().__init__(f"Multiple active players found for '{name}'")


_cache: dict[str, ResolvedPlayer] = {}


async def _mlb_candidates(
    people: list[PersonSearchResult], api: MlbApiProtocol
) -> list[PlayerCandidate]:
    """Fetch team/position detail and keep only players on MLB rosters.

    Minor league and winter league teams carry a parentOrgId; MLB teams do not.
    """
    candidates = []
    for p in people:
        detail = await api.get_person(p["id"], hydrate="currentTeam")
        team = detail.get("currentTeam", {})
        if "parentOrgId" in team:
            continue
        candidates.append(
            PlayerCandidate(
                player_id=p["id"],
                full_name=p["fullName"],
                position=detail.get("primaryPosition", {}).get("abbreviation", "?"),
                team=team.get("name", "Unknown Team"),
            )
        )
    return candidates


async def resolve_player(config: PlayerConfig, api: MlbApiProtocol) -> ResolvedPlayer:
    cache_key = config.name
    if cache_key in _cache:
        return _cache[cache_key]

    if config.player_id is not None:
        person = await api.get_person(config.player_id)
        result = ResolvedPlayer(
            player_id=config.player_id, full_name=person["fullName"]
        )
        _cache[cache_key] = result
        return result

    people = await api.search_player(config.name)
    active = [p for p in people if p.get("active")]

    if not active:
        raise ValueError(f"No active MLB player found for '{config.name}'")

    if len(active) > 1:
        active = await _mlb_candidates(active, api)  # type: ignore[assignment]

    if not active:
        raise ValueError(f"No active MLB player found for '{config.name}'")
    if len(active) > 1:
        raise AmbiguousPlayerError(config.name, active)  # type: ignore[arg-type]

    person = active[0]
    if isinstance(person, PlayerCandidate):
        result = ResolvedPlayer(player_id=person.player_id, full_name=person.full_name)
    else:
        result = ResolvedPlayer(player_id=person["id"], full_name=person["fullName"])
    _cache[cache_key] = result
    return result


async def resolve_all(
    player_configs: list[PlayerConfig], api: MlbApiProtocol
) -> list[ResolvedPlayer]:
    resolved = []
    errors = []
    for config in player_configs:
        try:
            resolved.append(await resolve_player(config, api))
        except AmbiguousPlayerError as e:
            lines = [
                f"  player_id = {c.player_id}  # {c.full_name} · {c.position} · {c.team}"
                for c in e.candidates
            ]
            errors.append(
                f"Multiple active players found for '{e.name}'.\n"
                f"Add player_id to config.toml to disambiguate:\n" + "\n".join(lines)
            )
        except ValueError as e:
            errors.append(str(e))
    if errors:
        raise ValueError("Player resolution failed:\n" + "\n".join(errors))
    return resolved
