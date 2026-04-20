import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from atbatwatch.db import Base, NotificationLog
from atbatwatch.delivery_worker import DELIVERY_GROUP, process_one
from atbatwatch.fanout_worker import DELIVERIES_STREAM
from atbatwatch.repos.follows import add_follow, create_user, upsert_player

GAME_PK = 823475
PLAYER_ID = 660271

SAMPLE_FIELDS: dict[str, str] = {
    "event_id": "",  # filled per-test
    "game_id": str(GAME_PK),
    "player_id": str(PLAYER_ID),
    "player_name": "Shohei Ohtani",
    "state": "at_bat",
    "home_team_id": "119",
    "home_team_name": "Los Angeles Dodgers",
    "away_team_id": "144",
    "away_team_name": "Atlanta Braves",
    "inning": "5",
    "inning_half": "Top",
    "outs": "2",
    "occurred_at": "2026-04-22T21:00:00+00:00",
    "user_id": "",  # filled per-test
    "webhook_url": "https://discord.example/webhook",
}


@pytest.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def seeded_sf(sf):
    """Returns (session_factory, user_id) with a user and player seeded."""
    async with sf() as sess:
        u = await create_user(
            "fan@example.com", "https://discord.example/webhook", sess
        )
        await upsert_player(PLAYER_ID, "Shohei Ohtani", sess)
        await add_follow(u.user_id, PLAYER_ID, sess)
    return sf, u.user_id


def _fields(event_id: str, user_id: int) -> dict[str, str]:
    f = dict(SAMPLE_FIELDS)
    f["event_id"] = event_id
    f["user_id"] = str(user_id)
    return f


async def test_delivery_sends_notification(seeded_sf, fake_redis, mocker):
    # Given: a seeded database and delivery message
    sf, user_id = seeded_sf
    mock_notify = mocker.patch(
        "atbatwatch.notifier.DiscordNotifier.notify", new_callable=AsyncMock
    )
    await fake_redis.xgroup_create(
        DELIVERIES_STREAM, DELIVERY_GROUP, id="0", mkstream=True
    )
    event_id = str(uuid.uuid4())
    fields = _fields(event_id, user_id)
    msg_id = await fake_redis.xadd(DELIVERIES_STREAM, fields)
    # When: processing the delivery
    await process_one(msg_id, fields, fake_redis, sf)
    # Then: Discord is notified with mapped status
    mock_notify.assert_called_once()
    call_kwargs = mock_notify.call_args
    assert call_kwargs.args[2] == "batting"


async def test_delivery_logs_to_notification_log(seeded_sf, fake_redis, mocker):
    # Given: a seeded database and delivery message
    sf, user_id = seeded_sf
    mocker.patch("atbatwatch.notifier.DiscordNotifier.notify", new_callable=AsyncMock)
    await fake_redis.xgroup_create(
        DELIVERIES_STREAM, DELIVERY_GROUP, id="0", mkstream=True
    )
    event_id = str(uuid.uuid4())
    fields = _fields(event_id, user_id)
    msg_id = await fake_redis.xadd(DELIVERIES_STREAM, fields)
    # When: processing the delivery
    await process_one(msg_id, fields, fake_redis, sf)
    # Then: notification is logged with correct status
    async with sf() as sess:
        result = await sess.execute(
            select(NotificationLog).where(
                NotificationLog.event_id == event_id,
                NotificationLog.user_id == user_id,
            )
        )
        log = result.scalar_one_or_none()
    assert log is not None
    assert log.status == "sent"
    assert log.player_id == PLAYER_ID


async def test_delivery_idempotent_on_replay(seeded_sf, fake_redis, mocker):
    # Given: same delivery message added twice
    sf, user_id = seeded_sf
    mock_notify = mocker.patch(
        "atbatwatch.notifier.DiscordNotifier.notify", new_callable=AsyncMock
    )
    await fake_redis.xgroup_create(
        DELIVERIES_STREAM, DELIVERY_GROUP, id="0", mkstream=True
    )
    event_id = str(uuid.uuid4())
    fields = _fields(event_id, user_id)
    msg_id1 = await fake_redis.xadd(DELIVERIES_STREAM, fields)
    msg_id2 = await fake_redis.xadd(DELIVERIES_STREAM, fields)
    # When: processing both messages
    await process_one(msg_id1, fields, fake_redis, sf)
    await process_one(msg_id2, fields, fake_redis, sf)
    # Then: notification is only sent once
    assert mock_notify.call_count == 1


async def test_delivery_discord_failure_does_not_log(seeded_sf, fake_redis, mocker):
    # Given: Discord call will fail
    sf, user_id = seeded_sf
    mocker.patch(
        "atbatwatch.notifier.DiscordNotifier.notify",
        new_callable=AsyncMock,
        side_effect=Exception("network error"),
    )
    await fake_redis.xgroup_create(
        DELIVERIES_STREAM, DELIVERY_GROUP, id="0", mkstream=True
    )
    event_id = str(uuid.uuid4())
    fields = _fields(event_id, user_id)
    msg_id = await fake_redis.xadd(DELIVERIES_STREAM, fields)
    # When: processing the delivery
    await process_one(msg_id, fields, fake_redis, sf)
    # Then: no log entry is created
    async with sf() as sess:
        result = await sess.execute(
            select(NotificationLog).where(NotificationLog.event_id == event_id)
        )
        assert result.scalar_one_or_none() is None


async def test_delivery_on_deck_maps_to_on_deck_status(seeded_sf, fake_redis, mocker):
    # Given: a delivery message with on_deck state
    sf, user_id = seeded_sf
    mock_notify = mocker.patch(
        "atbatwatch.notifier.DiscordNotifier.notify", new_callable=AsyncMock
    )
    await fake_redis.xgroup_create(
        DELIVERIES_STREAM, DELIVERY_GROUP, id="0", mkstream=True
    )
    fields = _fields(str(uuid.uuid4()), user_id)
    fields["state"] = "on_deck"
    msg_id = await fake_redis.xadd(DELIVERIES_STREAM, fields)
    # When: processing the delivery
    await process_one(msg_id, fields, fake_redis, sf)
    # Then: status is correctly mapped
    assert mock_notify.call_args.args[2] == "on_deck"
