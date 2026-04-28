from dataclasses import dataclass
from typing import cast

from atbatwatch.api import MlbApiProtocol
from atbatwatch.types import LiveFeedResponse, Offense


@dataclass
class GameInfo:
    game_pk: int
    home_team_id: int
    home_team_name: str
    away_team_id: int
    away_team_name: str
    status: str


async def get_todays_games(
    api: MlbApiProtocol, game_date: str | None = None
) -> list[GameInfo]:
    data = await api.get_schedule(game_date)
    games = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            games.append(
                GameInfo(
                    game_pk=game["gamePk"],
                    home_team_id=game["teams"]["home"]["team"]["id"],
                    home_team_name=game["teams"]["home"]["team"]["name"],
                    away_team_id=game["teams"]["away"]["team"]["id"],
                    away_team_name=game["teams"]["away"]["team"]["name"],
                    status=game["status"]["abstractGameState"],
                )
            )
    return games


def extract_inning_state(live_data: LiveFeedResponse) -> tuple[int, str, int]:
    """Returns (inning, half, outs) from the linescore, or (0, '', 0) when unavailable.

    When outs == 3 the half-inning just ended and the feed is between halves.
    Advance to the next half so notifications show the upcoming at-bat context.
    """
    try:
        ls = live_data["liveData"]["linescore"]
        inning = ls.get("currentInning", 0)
        is_top = ls.get("isTopInning", True)
        outs = ls.get("outs", 0)
        if outs == 3:
            # Between half-innings: advance to the half that is about to bat.
            if is_top:
                return inning, "Bot", 0
            else:
                return inning + 1, "Top", 0
        half = "Top" if is_top else "Bot"
        return inning, half, outs
    except (KeyError, TypeError):
        return 0, "", 0


def extract_offense_state(live_data: LiveFeedResponse) -> Offense:
    """Returns offense dict (batter/onDeck/inHole keys) or {} when unavailable."""
    try:
        return cast(Offense, live_data["liveData"]["linescore"].get("offense", {}))
    except (KeyError, TypeError):
        return cast(Offense, {})


_NON_LIVE_STATES = {
    "Warmup",
    "Pre-Game",
    "Delayed Start",
    "Scheduled",
    "Final",
    "Game Over",
    "Completed",
    "Completed Early",
    "Postponed",
    "Cancelled",
    "Suspended",
}


def is_game_in_progress(live_data: LiveFeedResponse) -> bool:
    """Returns False for any state where offense data is not actionable."""
    detailed = live_data.get("gameData", {}).get("status", {}).get("detailedState", "")
    return detailed not in _NON_LIVE_STATES


def get_player_status(player_id: int, offense: Offense) -> str:
    if player_id == offense.get("batter", {}).get("id"):
        return "batting"
    if player_id == offense.get("onDeck", {}).get("id"):
        return "on_deck"
    return "other"
