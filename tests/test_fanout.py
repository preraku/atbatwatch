import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from atbatwatch.db import Base
from atbatwatch.fanout_worker import DELIVERIES_STREAM, FANOUT_GROUP, process_one
from atbatwatch.repos.follows import add_follow, create_user, upsert_player

GAME_PK = 823475
PLAYER_ID = 660271  # Shohei Ohtani


def _sample_fields(player_id: int = PLAYER_ID) -> dict[str, str]:
    return {
        "event_id": str(uuid.uuid4()),
        "game_id": str(GAME_PK),
        "player_id": str(player_id),
        "player_name": "Shohei Ohtani",
        "state": "at_bat",
        "home_team_id": "119",
        "home_team_name": "Los Angeles Dodgers",
        "away_team_id": "144",
        "away_team_name": "Atlanta Braves",
        "inning": "3",
        "inning_half": "Top",
        "outs": "1",
        "occurred_at": "2026-04-22T20:00:00+00:00",
    }


@pytest.fixture
async def sf(session):
    """Return a session_factory backed by the same in-memory DB as the session fixture."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_fanout_creates_delivery_per_follower(sf, fake_redis):
    # Given: two followers of a player
    async with sf() as sess:
        u1 = await create_user("a@example.com", "https://discord.example/hook1", sess)
        u2 = await create_user("b@example.com", "https://discord.example/hook2", sess)
        await upsert_player(PLAYER_ID, "Shohei Ohtani", sess)
        await add_follow(u1.user_id, PLAYER_ID, sess)
        await add_follow(u2.user_id, PLAYER_ID, sess)
    # When: processing an event
    await fake_redis.xgroup_create(
        "events:transitions", FANOUT_GROUP, id="0", mkstream=True
    )
    msg_id = await fake_redis.xadd("events:transitions", _sample_fields())
    await process_one(msg_id, _sample_fields(), fake_redis, sf)
    # Then: one delivery per follower is created
    deliveries = await fake_redis.xread({DELIVERIES_STREAM: "0"})
    assert len(deliveries) == 1
    _stream, messages = deliveries[0]
    assert len(messages) == 2
    user_ids = {m[1]["user_id"] for m in messages}
    assert user_ids == {str(u1.user_id), str(u2.user_id)}


async def test_fanout_delivery_has_webhook_url(sf, fake_redis):
    # Given: a follower with a webhook URL
    async with sf() as sess:
        u = await create_user("a@example.com", "https://discord.example/hookA", sess)
        await upsert_player(PLAYER_ID, "Shohei Ohtani", sess)
        await add_follow(u.user_id, PLAYER_ID, sess)
    # When: processing an event
    await fake_redis.xgroup_create(
        "events:transitions", FANOUT_GROUP, id="0", mkstream=True
    )
    msg_id = await fake_redis.xadd("events:transitions", _sample_fields())
    await process_one(msg_id, _sample_fields(), fake_redis, sf)
    # Then: delivery includes webhook URL
    deliveries = await fake_redis.xread({DELIVERIES_STREAM: "0"})
    _stream, messages = deliveries[0]
    assert messages[0][1]["webhook_url"] == "https://discord.example/hookA"


async def test_fanout_no_followers_no_delivery(sf, fake_redis):
    # Given: a player with no followers
    async with sf() as sess:
        await upsert_player(PLAYER_ID, "Shohei Ohtani", sess)
    # When: processing an event
    await fake_redis.xgroup_create(
        "events:transitions", FANOUT_GROUP, id="0", mkstream=True
    )
    msg_id = await fake_redis.xadd("events:transitions", _sample_fields())
    await process_one(msg_id, _sample_fields(), fake_redis, sf)
    # Then: no delivery stream is created
    assert not await fake_redis.exists(DELIVERIES_STREAM)


async def test_fanout_preserves_all_event_fields(sf, fake_redis):
    # Given: a follower and event fields
    async with sf() as sess:
        u = await create_user("a@example.com", "https://discord.example/hook", sess)
        await upsert_player(PLAYER_ID, "Shohei Ohtani", sess)
        await add_follow(u.user_id, PLAYER_ID, sess)
    fields = _sample_fields()
    # When: processing an event
    await fake_redis.xgroup_create(
        "events:transitions", FANOUT_GROUP, id="0", mkstream=True
    )
    msg_id = await fake_redis.xadd("events:transitions", fields)
    await process_one(msg_id, fields, fake_redis, sf)
    # Then: all event fields are preserved in delivery
    deliveries = await fake_redis.xread({DELIVERIES_STREAM: "0"})
    _stream, messages = deliveries[0]
    delivery_fields = messages[0][1]
    assert delivery_fields["event_id"] == fields["event_id"]
    assert delivery_fields["player_name"] == "Shohei Ohtani"
    assert delivery_fields["state"] == "at_bat"
    assert delivery_fields["home_team_name"] == "Los Angeles Dodgers"
