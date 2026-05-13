"""Stub MLB Stats API server for acceptance tests.

Serves fixture files from /fixtures (volume-mounted from ./fixtures).
Test code controls which fixture is served via POST /admin/configure.
Inline JSON can be supplied via schedule_json (takes precedence over schedule_path).
Request counts are tracked per endpoint and exposed via GET /admin/stats.
"""

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI()

_FIXTURES_ROOT = Path("/fixtures")

_state: dict = {
    "schedule_path": None,
    "schedule_json": None,
    "games": {},
}

_counters: dict = {
    "schedule": 0,
    "live_feed": 0,
    "diff_patch": 0,
}


class ConfigureRequest(BaseModel):
    game_pk: Optional[int] = None
    live_feed_path: Optional[str] = None
    diff_patch_path: Optional[str] = None
    schedule_path: Optional[str] = None
    schedule_json: Optional[str] = (
        None  # raw JSON string; takes precedence over schedule_path
    )


def _serve(rel_path: str) -> Response:
    full_path = _FIXTURES_ROOT / rel_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail=f"Fixture not found: {rel_path}")
    return Response(content=full_path.read_bytes(), media_type="application/json")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/admin/configure")
def configure(req: ConfigureRequest):
    if req.schedule_json is not None:
        _state["schedule_json"] = req.schedule_json
        _state["schedule_path"] = None
    elif req.schedule_path is not None:
        _state["schedule_path"] = req.schedule_path
        _state["schedule_json"] = None
    if req.game_pk is not None:
        game = _state["games"].setdefault(req.game_pk, {})
        if req.live_feed_path is not None:
            game["live_feed_path"] = req.live_feed_path
        if req.diff_patch_path is not None:
            game["diff_patch_path"] = req.diff_patch_path
    return {"status": "ok"}


@app.post("/admin/reset")
def reset():
    _state["schedule_path"] = None
    _state["schedule_json"] = None
    _state["games"].clear()
    _counters["schedule"] = 0
    _counters["live_feed"] = 0
    _counters["diff_patch"] = 0
    return {"status": "ok"}


@app.get("/admin/stats")
def stats():
    return dict(_counters)


@app.get("/api/v1/schedule")
def get_schedule():
    _counters["schedule"] += 1
    if _state["schedule_json"] is not None:
        return Response(content=_state["schedule_json"], media_type="application/json")
    path = _state["schedule_path"]
    if not path:
        raise HTTPException(status_code=404, detail="Schedule fixture not configured")
    return _serve(path)


@app.get("/api/v1.1/game/{game_pk}/feed/live")
def get_live_feed(game_pk: int):
    _counters["live_feed"] += 1
    game = _state["games"].get(game_pk, {})
    path = game.get("live_feed_path")
    if not path:
        raise HTTPException(
            status_code=404, detail=f"No live feed configured for game {game_pk}"
        )
    return _serve(path)


@app.get("/api/v1.1/game/{game_pk}/feed/live/diffPatch")
def get_diff_patch(game_pk: int):
    _counters["diff_patch"] += 1
    game = _state["games"].get(game_pk, {})
    path = game.get("diff_patch_path")
    if not path:
        raise HTTPException(
            status_code=404, detail=f"No diff patch configured for game {game_pk}"
        )
    return _serve(path)
