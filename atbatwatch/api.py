import json
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from atbatwatch.types import (
    LiveFeedResponse,
    PersonDetail,
    PersonSearchResult,
    ScheduleResponse,
)

FixtureData = LiveFeedResponse | ScheduleResponse

BASE_URL = "https://ws.statsapi.mlb.com"


class MlbApiProtocol(Protocol):
    async def get_schedule(self, game_date: str | None = None) -> ScheduleResponse: ...

    async def get_live_feed(self, game_pk: int) -> LiveFeedResponse: ...

    async def get_live_feed_diff(
        self, game_pk: int, start_timecode: str
    ) -> tuple[LiveFeedResponse | None, str]: ...

    async def search_player(self, name: str) -> list[PersonSearchResult]: ...

    async def get_person(
        self, player_id: int, hydrate: str | None = None
    ) -> PersonDetail: ...


class MlbApi:
    def __init__(self):
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=15)

    async def get_schedule(self, game_date: str | None = None) -> ScheduleResponse:
        if game_date is None:
            game_date = datetime.now(ZoneInfo("America/New_York")).strftime("%m/%d/%Y")
        resp = await self._client.get(
            "/api/v1/schedule",
            params={"sportId": 1, "date": game_date, "hydrate": "team,linescore"},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_live_feed(self, game_pk: int) -> LiveFeedResponse:
        resp = await self._client.get(f"/api/v1.1/game/{game_pk}/feed/live")
        resp.raise_for_status()
        return resp.json()

    async def get_live_feed_diff(
        self, game_pk: int, start_timecode: str
    ) -> tuple[LiveFeedResponse | None, str]:
        """Fetch only changes since start_timecode via the diffPatch endpoint.

        Returns (full_data, new_timecode) when the server emits a fullUpdate,
        or (None, new_timecode) when the response is a JSON Patch list with no
        offense changes. Falls back to a full feed fetch if offense ops appear
        in a patch (safety net for undocumented API behaviour).
        """
        resp = await self._client.get(
            f"/api/v1.1/game/{game_pk}/feed/live/diffPatch",
            params={"startTimecode": start_timecode},
        )
        resp.raise_for_status()
        body = resp.json()

        if isinstance(body, list):
            offense_ops = [op for op in body if "offense" in op.get("path", "")]
            if offense_ops:
                return await self.get_live_feed(game_pk), start_timecode

            ts_op = next(
                (op for op in body if op.get("path") == "/metaData/timeStamp"), None
            )
            new_ts = ts_op["value"] if ts_op else start_timecode
            return None, new_ts

        new_ts = body.get("metaData", {}).get("timeStamp", start_timecode)
        return body, new_ts

    async def search_player(self, name: str) -> list[PersonSearchResult]:
        resp = await self._client.get("/api/v1/people/search", params={"names": name})
        resp.raise_for_status()
        return resp.json().get("people", [])

    async def get_person(
        self, player_id: int, hydrate: str | None = None
    ) -> PersonDetail:
        params = {}
        if hydrate:
            params["hydrate"] = hydrate
        resp = await self._client.get(f"/api/v1/people/{player_id}", params=params)
        resp.raise_for_status()
        people = resp.json().get("people", [])
        if not people:
            raise ValueError(f"Player ID {player_id} not found")
        return people[0]

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "MlbApi":
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()


def load_fixture(path: Path) -> FixtureData:
    with open(path) as f:
        return json.load(f)
