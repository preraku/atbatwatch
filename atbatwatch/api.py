import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

import httpx

from atbatwatch.types import (
    LiveFeedResponse,
    PersonDetail,
    PersonSearchResult,
    ScheduleResponse,
)

FixtureData = LiveFeedResponse | ScheduleResponse

BASE_URL = os.getenv("MLB_API_BASE_URL", "https://ws.statsapi.mlb.com")


def parse_diff_patch(
    body: Any, start_timecode: str
) -> tuple[LiveFeedResponse | None, str, bool]:
    """Parse a diffPatch API response body into (data, new_timecode, needs_full_fetch).

    The API returns one of two shapes:
    - list: each element is {"diff": [<RFC 6902 ops>]}. Returns (None, new_ts, False),
      or (None, start_timecode, True) if any op path contains "offense" — the caller
      must fetch the full feed to resolve the change.
    - dict: a fullUpdate live feed. Returns (body, body.metaData.timeStamp, False).
    """
    if isinstance(body, list):
        all_ops: list[dict] = [op for item in body for op in item.get("diff", [])]

        if any("offense" in op.get("path", "") for op in all_ops):
            return None, start_timecode, True

        ts_op = next(
            (op for op in all_ops if op.get("path") == "/metaData/timeStamp"), None
        )
        new_ts = ts_op["value"] if ts_op else start_timecode
        return None, new_ts, False

    new_ts = body.get("metaData", {}).get("timeStamp", start_timecode)
    return cast(LiveFeedResponse, body), new_ts, False


def _eastern_date() -> str:
    """Return today's date in MM/DD/YYYY using Eastern Time (handles EDT/EST automatically)."""
    return datetime.now(ZoneInfo("America/New_York")).strftime("%m/%d/%Y")


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
            game_date = _eastern_date()
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
        """Fetch only changes since start_timecode via the diffPatch endpoint."""
        resp = await self._client.get(
            f"/api/v1.1/game/{game_pk}/feed/live/diffPatch",
            params={"startTimecode": start_timecode},
        )
        resp.raise_for_status()
        data, new_ts, needs_full_fetch = parse_diff_patch(resp.json(), start_timecode)
        if needs_full_fetch:
            return await self.get_live_feed(game_pk), start_timecode
        return data, new_ts

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
