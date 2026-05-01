"""Acceptance test fixtures.

The stack (postgres, redis, api, mlb-stub, webhook-capture) is started once per
session if not already running.  Per-test fixtures reset shared state so tests
are fully isolated.

Worker commands (poll-once, fanout-once, delivery-once) are invoked via
`docker compose run --rm` — see the `run_worker` fixture.
"""

import subprocess
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Optional

import asyncpg
import httpx
import pytest
import redis.asyncio as aioredis

# ---------------------------------------------------------------------------
# Constants — mirror the port mappings in docker-compose.acceptance.yml
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent
_COMPOSE_FILE = _PROJECT_ROOT / "docker-compose.acceptance.yml"

POSTGRES_DSN = "postgresql://atbatwatch:atbatwatch_test@localhost:5433/atbatwatch"
REDIS_URL = "redis://localhost:6380/0"
API_BASE_URL = "http://localhost:8001"
MLB_STUB_BASE_URL = "http://localhost:9001"
WEBHOOK_CAPTURE_BASE_URL = "http://localhost:9002"

# Internal Docker-network address the delivery worker POSTs to
_WEBHOOK_CAPTURE_INTERNAL = "http://webhook-capture:9002"


# ---------------------------------------------------------------------------
# Stack management (session-scoped, sync)
# ---------------------------------------------------------------------------


def _compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), *args],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
    )


def _api_is_up() -> bool:
    try:
        resp = httpx.get(f"{API_BASE_URL}/docs", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def stack():
    """Ensure the acceptance stack is running; tear it down only if we started it."""
    started_here = False
    if not _api_is_up():
        started_here = True
        result = _compose("up", "-d", "--build", "--wait")
        if result.returncode != 0:
            raise RuntimeError(
                f"docker compose up failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )

    yield

    if started_here:
        _compose("down", "-v")


# ---------------------------------------------------------------------------
# Per-test isolation fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(stack):
    """asyncpg connection; truncates all tables in teardown."""
    conn = await asyncpg.connect(POSTGRES_DSN)
    yield conn
    await conn.execute(
        "TRUNCATE users, players, follows, notification_log RESTART IDENTITY CASCADE"
    )
    await conn.close()


@pytest.fixture
async def redis_client(stack):
    """redis.asyncio client; FLUSHDBs in teardown."""
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.fixture
async def http(stack):
    """httpx.AsyncClient pointing at the acceptance API."""
    async with httpx.AsyncClient(base_url=API_BASE_URL) as client:
        yield client


# ---------------------------------------------------------------------------
# Stub control fixtures
# ---------------------------------------------------------------------------


class MlbStubClient:
    """Thin wrapper around the mlb-stub /admin endpoints."""

    def __init__(self, base_url: str):
        self._base = base_url

    def configure(
        self,
        game_pk: Optional[int] = None,
        live_feed_path: Optional[str] = None,
        diff_patch_path: Optional[str] = None,
        schedule_path: Optional[str] = None,
    ) -> None:
        payload: dict = {}
        if game_pk is not None:
            payload["game_pk"] = game_pk
        if live_feed_path is not None:
            payload["live_feed_path"] = live_feed_path
        if diff_patch_path is not None:
            payload["diff_patch_path"] = diff_patch_path
        if schedule_path is not None:
            payload["schedule_path"] = schedule_path
        httpx.post(
            f"{self._base}/admin/configure", json=payload, timeout=5
        ).raise_for_status()

    def reset(self) -> None:
        httpx.post(f"{self._base}/admin/reset", timeout=5).raise_for_status()


class WebhookCaptureClient:
    """Thin wrapper around the webhook-capture endpoints."""

    def __init__(self, base_url: str):
        self._base = base_url

    def get_captured(self, webhook_id: Optional[str] = None) -> list[dict]:
        params = {"webhook_id": webhook_id} if webhook_id else {}
        return httpx.get(f"{self._base}/captured", params=params, timeout=5).json()

    def reset(self) -> None:
        httpx.delete(f"{self._base}/captured", timeout=5).raise_for_status()


@pytest.fixture
def mlb_stub(stack) -> Generator[MlbStubClient, None, None]:
    client = MlbStubClient(MLB_STUB_BASE_URL)
    client.reset()
    yield client
    client.reset()


@pytest.fixture
def webhook_capture(stack) -> Generator[WebhookCaptureClient, None, None]:
    client = WebhookCaptureClient(WEBHOOK_CAPTURE_BASE_URL)
    client.reset()
    yield client
    client.reset()


# ---------------------------------------------------------------------------
# Worker runner
# ---------------------------------------------------------------------------


@pytest.fixture
def run_worker(stack):
    """Run an atbatwatch worker subcommand once via docker compose run --rm.

    Usage::

        run_worker("poller", "poll-once")
        run_worker("fanout", "fanout-once")
        run_worker("delivery", "delivery-once")
    """

    def _run(service: str, cmd: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(_COMPOSE_FILE),
                "--profile",
                "workers",
                "run",
                "--rm",
                "-T",
                service,
                "atbatwatch",
                cmd,
            ],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"`atbatwatch {cmd}` in {service} failed (exit {result.returncode}):\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
        return result

    return _run


# ---------------------------------------------------------------------------
# User helper
# ---------------------------------------------------------------------------


@pytest.fixture
def signup_and_login(http, db):
    """Factory: creates a user and returns (user_id, token, webhook_id).

    The webhook URL stored in the DB uses the Docker-internal address of the
    webhook-capture service so the delivery worker can POST to it.
    """

    async def _make(
        email: Optional[str] = None, password: str = "testpass123"
    ) -> tuple[int, str, str]:
        if email is None:
            email = f"test-{uuid.uuid4()}@example.com"
        webhook_id = str(uuid.uuid4())
        webhook_url = f"{_WEBHOOK_CAPTURE_INTERNAL}/hooks/{webhook_id}"

        resp = await http.post(
            "/auth/signup",
            json={"email": email, "password": password, "discord_webhook": webhook_url},
        )
        assert resp.status_code == 201, f"signup failed: {resp.text}"
        token = resp.json()["token"]

        row = await db.fetchrow("SELECT user_id FROM users WHERE email = $1", email)
        assert row is not None, f"user not found in DB for {email}"
        return int(row["user_id"]), token, webhook_id

    return _make
