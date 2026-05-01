"""Stub webhook capture server for acceptance tests.

Records inbound webhook POSTs in memory so tests can assert on them.
"""

import time
from typing import Optional

from fastapi import FastAPI, Request

app = FastAPI()

_captured: list[dict] = []


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/hooks/{webhook_id}")
async def capture(webhook_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = (await request.body()).decode("utf-8", errors="replace")
    _captured.append(
        {
            "webhook_id": webhook_id,
            "body": body,
            "headers": dict(request.headers),
            "timestamp": time.time(),
        }
    )
    return {"status": "ok"}


@app.get("/captured")
def get_captured(webhook_id: Optional[str] = None):
    if webhook_id:
        return [c for c in _captured if c["webhook_id"] == webhook_id]
    return _captured


@app.delete("/captured")
def delete_captured():
    _captured.clear()
    return {"status": "ok"}
