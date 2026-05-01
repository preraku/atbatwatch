"""Fan-out worker: reads transitions stream, writes per-user delivery jobs."""

import asyncio

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atbatwatch.diff_engine import TRANSITIONS_STREAM
from atbatwatch.repos.follows import get_followers

DELIVERIES_STREAM = "events:deliveries"
FANOUT_GROUP = "fanout-group"
FANOUT_CONSUMER = "fanout-1"


async def _ensure_group(redis: aioredis.Redis) -> None:
    try:
        # pyrefly: ignore [not-async]
        await redis.xgroup_create(
            TRANSITIONS_STREAM, FANOUT_GROUP, id="0", mkstream=True
        )
    except Exception:
        pass  # group already exists


async def process_one(
    msg_id: str,
    fields: dict[str, str],
    redis: aioredis.Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fan out one transition event to per-user delivery jobs."""
    player_id = int(fields["player_id"])
    async with session_factory() as session:
        followers = await get_followers(player_id, session)
    for user in followers:
        delivery: dict[str, str] = dict(fields)
        delivery["user_id"] = str(user.user_id)
        delivery["webhook_url"] = user.notification_target_id
        # pyrefly: ignore [not-async, bad-argument-type]
        await redis.xadd(DELIVERIES_STREAM, delivery)
    # pyrefly: ignore [not-async]
    await redis.xack(TRANSITIONS_STREAM, FANOUT_GROUP, msg_id)


async def fanout_once(
    redis: aioredis.Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Process all pending transition messages once and return."""
    await _ensure_group(redis)
    # pyrefly: ignore [not-async]
    results = await redis.xreadgroup(
        FANOUT_GROUP,
        FANOUT_CONSUMER,
        {TRANSITIONS_STREAM: ">"},
        count=100,
    )
    if not results:
        return
    for _stream, messages in results:
        for msg_id, fields in messages:
            try:
                await process_one(msg_id, fields, redis, session_factory)
            except Exception as e:
                print(f"Fanout error for msg {msg_id}: {e}")


async def run_fanout(
    redis: aioredis.Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _ensure_group(redis)
    print("Fanout worker started.")
    while True:
        try:
            # pyrefly: ignore [not-async]
            results = await redis.xreadgroup(
                FANOUT_GROUP,
                FANOUT_CONSUMER,
                {TRANSITIONS_STREAM: ">"},
                count=10,
                block=5000,
            )
            for _stream, messages in results:
                for msg_id, fields in messages:
                    try:
                        await process_one(msg_id, fields, redis, session_factory)
                    except Exception as e:
                        print(f"Fanout error for msg {msg_id}: {e}")
        except Exception as e:
            print(f"Fanout worker error: {e}")
            await asyncio.sleep(1.0)
