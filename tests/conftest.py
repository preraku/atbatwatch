import json
from pathlib import Path

import fakeredis.aioredis as aioredis
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from atbatwatch.db import Base

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def load_fixture(rel_path: str):
    with open(FIXTURES_DIR / rel_path) as f:
        return json.load(f)


@pytest.fixture
def live_feed_in_progress():
    return load_fixture("live_feed/in_progress_game_823475.json")


@pytest.fixture
def live_feed_warmup():
    return load_fixture("live_feed/warmup_game_824370.json")


@pytest.fixture
def live_feed_final():
    return load_fixture("live_feed/final_game_822750.json")


@pytest.fixture
def schedule_fixture():
    return load_fixture("schedule/schedule_20260419.json")


@pytest.fixture
def fake_redis():
    return aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()
