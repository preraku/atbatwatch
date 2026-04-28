from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from atbatwatch.games import GameInfo


class Notifier(Protocol):
    async def notify(
        self,
        player_id: int,
        player_name: str,
        status: str,
        game_info: GameInfo,
        inning: int,
        inning_half: str,
        outs: int,
    ) -> None: ...


class RecordingNotifier:
    """Captures notify() calls for use in tests and --dry-run mode."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

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
        self.calls.append(
            {
                "player_id": player_id,
                "player_name": player_name,
                "status": status,
                "game_info": game_info,
                "inning": inning,
                "inning_half": inning_half,
                "outs": outs,
            }
        )


class ConsoleNotifier:
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
        ts = datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S ET")
        label = "AT BAT" if status == "batting" else "ON DECK"
        matchup = f"{game_info.away_team_name} @ {game_info.home_team_name}"
        out_word = "out" if outs == 1 else "outs"
        game_state = f"{inning_half} {inning}, {outs} {out_word}" if inning else ""
        suffix = f"  [{game_state}]" if game_state else ""
        print(f"[{ts}] {label}: {player_name}  ({matchup}){suffix}")


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

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
        label = "⚾ **AT BAT**" if status == "batting" else "🔄 **ON DECK**"
        matchup = f"{game_info.away_team_name} @ {game_info.home_team_name}"
        out_word = "out" if outs == 1 else "outs"
        game_state = f"{inning_half} {inning}, {outs} {out_word}" if inning else ""
        suffix = f" — {game_state}" if game_state else ""
        content = f"{label}: **{player_name}** ({matchup}{suffix})"

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(self._webhook_url, json={"content": content})
            resp.raise_for_status()
