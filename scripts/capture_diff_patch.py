#!/usr/bin/env -S uv run python
"""Capture MLB diffPatch fixtures from a live game.

The poller uses /api/v1.1/game/{pk}/feed/live/diffPatch on every poll after
the first. Its response shape (JSON Patch array vs fullUpdate object) is
unfixtured today — this script captures both shapes against a real live game
so the Go rewrite has something to test diffPatch parsing against.

Usage:
    uv run python scripts/capture_diff_patch.py <game_pk>

Writes to fixtures/diff_patch/<game_pk>/:
    baseline.json              full live feed at t0
    baseline.meta.json         { captured_at_utc, timecode }
    patch_t+15s.json           diffPatch with startTimecode=baseline.timecode
    patch_t+15s.meta.json      { captured_at_utc, start_timecode, shape }
    patch_t+60s.json           diffPatch with startTimecode=baseline.timecode
    patch_t+60s.meta.json
    baseline_2.json            fresh full feed at t0+90s
    baseline_2.meta.json
    patch_t2+30s.json          diffPatch with startTimecode=baseline_2.timecode
    patch_t2+30s.meta.json

Run during an in-progress game. Run separately per game_pk.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE_URL = "https://ws.statsapi.mlb.com"
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures" / "diff_patch"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, body: object, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2))
    meta_path = path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2))
    size_kb = path.stat().st_size / 1024
    print(f"  wrote {path.relative_to(ROOT)} ({size_kb:.1f} KB)")


def _shape(body: object) -> str:
    if isinstance(body, list):
        offense_ops = sum(1 for op in body if "offense" in op.get("path", ""))
        return f"patch_array (n_ops={len(body)}, offense_ops={offense_ops})"
    if isinstance(body, dict) and "metaData" in body:
        return "full_update"
    return f"unknown ({type(body).__name__})"


def fetch_baseline(client: httpx.Client, game_pk: int) -> tuple[dict, str]:
    resp = client.get(f"/api/v1.1/game/{game_pk}/feed/live")
    resp.raise_for_status()
    body = resp.json()
    timecode = body["metaData"]["timeStamp"]
    return body, timecode


def fetch_patch(client: httpx.Client, game_pk: int, start_timecode: str) -> object:
    resp = client.get(
        f"/api/v1.1/game/{game_pk}/feed/live/diffPatch",
        params={"startTimecode": start_timecode},
    )
    resp.raise_for_status()
    return resp.json()


def capture(game_pk: int) -> None:
    out_dir = FIXTURES / str(game_pk)
    print(
        f"Capturing diffPatch fixtures for game {game_pk} → {out_dir.relative_to(ROOT)}/"
    )

    with httpx.Client(base_url=BASE_URL, timeout=20) as client:
        # t0: baseline
        print("[t+0s] fetching baseline live feed")
        baseline, t0_timecode = fetch_baseline(client, game_pk)
        status = baseline.get("gameData", {}).get("status", {}).get("detailedState")
        print(f"        game status: {status}, timecode: {t0_timecode}")
        if status != "In Progress":
            print(
                f"  WARNING: game is '{status}', not 'In Progress' — diffPatch may be uninteresting"
            )
        _write(
            out_dir / "baseline.json",
            baseline,
            {
                "captured_at_utc": _now_iso(),
                "timecode": t0_timecode,
                "game_pk": game_pk,
            },
        )

        # t+15s: short delta
        print("[t+15s] sleeping…")
        time.sleep(15)
        body = fetch_patch(client, game_pk, t0_timecode)
        print(f"         shape: {_shape(body)}")
        _write(
            out_dir / "patch_t+15s.json",
            body,
            {
                "captured_at_utc": _now_iso(),
                "start_timecode": t0_timecode,
                "shape": _shape(body),
            },
        )

        # t+60s: longer delta against same baseline (more chance of offense ops)
        print("[t+60s] sleeping…")
        time.sleep(45)
        body = fetch_patch(client, game_pk, t0_timecode)
        print(f"         shape: {_shape(body)}")
        _write(
            out_dir / "patch_t+60s.json",
            body,
            {
                "captured_at_utc": _now_iso(),
                "start_timecode": t0_timecode,
                "shape": _shape(body),
            },
        )

        # t+90s: fresh baseline so we can capture a short delta from a recent timecode
        # (catches the case where a fullUpdate response would appear)
        print("[t+90s] fetching fresh baseline")
        time.sleep(30)
        baseline_2, t2_timecode = fetch_baseline(client, game_pk)
        _write(
            out_dir / "baseline_2.json",
            baseline_2,
            {
                "captured_at_utc": _now_iso(),
                "timecode": t2_timecode,
                "game_pk": game_pk,
            },
        )

        print("[t+120s] sleeping…")
        time.sleep(30)
        body = fetch_patch(client, game_pk, t2_timecode)
        print(f"          shape: {_shape(body)}")
        _write(
            out_dir / "patch_t2+30s.json",
            body,
            {
                "captured_at_utc": _now_iso(),
                "start_timecode": t2_timecode,
                "shape": _shape(body),
            },
        )

    print(f"\nDone. Fixtures in {out_dir.relative_to(ROOT)}/")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    try:
        game_pk = int(sys.argv[1])
    except ValueError:
        print(f"error: game_pk must be an integer, got {sys.argv[1]!r}")
        return 2
    capture(game_pk)
    return 0


if __name__ == "__main__":
    sys.exit(main())
