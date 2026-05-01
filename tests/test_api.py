"""Tests for API date handling and diffPatch parsing."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import time_machine

from atbatwatch.api import MlbApi, _eastern_date, parse_diff_patch

# ── UTC/Eastern boundary ──────────────────────────────────────────────────────


@time_machine.travel(datetime(2026, 4, 27, 1, 0, 0, tzinfo=timezone.utc))
def test_eastern_date_utc_past_midnight_is_still_previous_eastern_day():
    # 01:00 UTC on Apr 27 = 21:00 EDT on Apr 26
    assert _eastern_date() == "04/26/2026"


@time_machine.travel(datetime(2026, 4, 27, 4, 0, 0, tzinfo=timezone.utc))
def test_eastern_date_utc_4am_matches_eastern_day():
    # 04:00 UTC on Apr 27 = 00:00 EDT on Apr 27
    assert _eastern_date() == "04/27/2026"


# ── EDT vs EST (DST boundary) ─────────────────────────────────────────────────


@time_machine.travel(datetime(2026, 7, 4, 2, 0, 0, tzinfo=timezone.utc))
def test_eastern_date_summer_edt_offset_is_minus_4():
    # 02:00 UTC in July = 22:00 EDT (UTC-4) on July 3
    assert _eastern_date() == "07/03/2026"


@time_machine.travel(datetime(2026, 1, 15, 4, 30, 0, tzinfo=timezone.utc))
def test_eastern_date_winter_est_offset_is_minus_5():
    # 04:30 UTC in January = 23:30 EST (UTC-5) on Jan 14
    assert _eastern_date() == "01/14/2026"


# ── Format ────────────────────────────────────────────────────────────────────


@time_machine.travel(datetime(2026, 3, 5, 12, 0, 0, tzinfo=timezone.utc))
def test_eastern_date_format_is_mm_dd_yyyy():
    assert _eastern_date() == "03/05/2026"


# ── get_schedule passes the correct date to the HTTP client ───────────────────


def _mock_schedule_response(mocker, api: MlbApi) -> AsyncMock:
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"dates": []}
    mock_get = mocker.patch.object(
        api._client, "get", new_callable=AsyncMock, return_value=mock_resp
    )
    return mock_get


@time_machine.travel(datetime(2026, 4, 27, 1, 0, 0, tzinfo=timezone.utc))
async def test_get_schedule_sends_eastern_date_at_utc_midnight(mocker):
    # 01:00 UTC on Apr 27 = 21:00 EDT on Apr 26
    api = MlbApi()
    mock_get = _mock_schedule_response(mocker, api)
    await api.get_schedule()
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["date"] == "04/26/2026"


@time_machine.travel(datetime(2026, 1, 15, 4, 30, 0, tzinfo=timezone.utc))
async def test_get_schedule_sends_eastern_date_in_winter_est(mocker):
    # 04:30 UTC in January = 23:30 EST (UTC-5) on Jan 14
    api = MlbApi()
    mock_get = _mock_schedule_response(mocker, api)
    await api.get_schedule()
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["date"] == "01/14/2026"


# ── parse_diff_patch ──────────────────────────────────────────────────────────

_TC = "20260429_191321"


def _patch_body(ops: list[dict]) -> list[dict]:
    return [{"diff": ops}]


def test_parse_diff_patch_full_update_returns_body_and_timestamp():
    body = {"metaData": {"timeStamp": "20260429_191535"}, "gameData": {}}
    data, ts, needs_fetch = parse_diff_patch(body, _TC)
    assert data is body
    assert ts == "20260429_191535"
    assert needs_fetch is False


def test_parse_diff_patch_full_update_missing_timestamp_falls_back():
    body = {"metaData": {}, "gameData": {}}
    data, ts, needs_fetch = parse_diff_patch(body, _TC)
    assert data is body
    assert ts == _TC
    assert needs_fetch is False


def test_parse_diff_patch_patch_array_extracts_timestamp():
    ops = [{"op": "replace", "path": "/metaData/timeStamp", "value": "20260429_191342"}]
    data, ts, needs_fetch = parse_diff_patch(_patch_body(ops), _TC)
    assert data is None
    assert ts == "20260429_191342"
    assert needs_fetch is False


def test_parse_diff_patch_patch_array_no_timestamp_falls_back():
    ops = [
        {
            "op": "replace",
            "path": "/liveData/plays/currentPlay/about/endTime",
            "value": "x",
        }
    ]
    data, ts, needs_fetch = parse_diff_patch(_patch_body(ops), _TC)
    assert data is None
    assert ts == _TC
    assert needs_fetch is False


def test_parse_diff_patch_empty_array_returns_start_timecode():
    data, ts, needs_fetch = parse_diff_patch([], _TC)
    assert data is None
    assert ts == _TC
    assert needs_fetch is False


def test_parse_diff_patch_offense_ops_signals_full_fetch():
    ops = [
        {"op": "replace", "path": "/liveData/linescore/offense/batter", "value": {}},
        {"op": "replace", "path": "/metaData/timeStamp", "value": "20260429_191342"},
    ]
    data, ts, needs_fetch = parse_diff_patch(_patch_body(ops), _TC)
    assert data is None
    assert ts == _TC
    assert needs_fetch is True


def test_parse_diff_patch_multi_diff_objects_reads_all_ops():
    body = [
        {"diff": [{"op": "replace", "path": "/liveData/plays/x", "value": 1}]},
        {
            "diff": [
                {
                    "op": "replace",
                    "path": "/metaData/timeStamp",
                    "value": "20260429_191500",
                }
            ]
        },
    ]
    data, ts, needs_fetch = parse_diff_patch(body, _TC)
    assert ts == "20260429_191500"
    assert needs_fetch is False
