"""Delivery worker: reads per-user delivery jobs, sends Discord, logs idempotently."""

import asyncio

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atbatwatch.db import NotificationLog
from atbatwatch.fanout_worker import DELIVERIES_STREAM
from atbatwatch.games import GameInfo
from atbatwatch.notifier import DiscordNotifier

DELIVERY_GROUP = "delivery-group"
DELIVERY_CONSUMER = "delivery-1"


async def _ensure_group(redis: aioredis.Redis) -> None:
    try:
        # pyrefly: ignore [not-async]
        await redis.xgroup_create(
            DELIVERIES_STREAM, DELIVERY_GROUP, id="0", mkstream=True
        )
    except Exception:
        pass  # group already exists


async def _already_sent(event_id: str, user_id: int, session: AsyncSession) -> bool:
    result = await session.execute(
        select(NotificationLog).where(
            NotificationLog.event_id == event_id,
            NotificationLog.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def _log_sent(
    event_id: str,
    user_id: int,
    player_id: int,
    state: str,
    session: AsyncSession,
) -> None:
    session.add(
        NotificationLog(
            event_id=event_id,
            user_id=user_id,
            player_id=player_id,
            state=state,
            status="sent",
        )
    )
    await session.commit()


async def process_one(
    msg_id: str,
    fields: dict[str, str],
    redis: aioredis.Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Send one delivery job: idempotency check → Discord → log → ACK."""
    event_id = fields["event_id"]
    user_id = int(fields["user_id"])
    player_id = int(fields["player_id"])
    state = fields["state"]

    async with session_factory() as session:
        if await _already_sent(event_id, user_id, session):
            # pyrefly: ignore [not-async]
            await redis.xack(DELIVERIES_STREAM, DELIVERY_GROUP, msg_id)
            return

    game_info = GameInfo(
        game_pk=int(fields["game_id"]),
        home_team_id=int(fields["home_team_id"]),
        home_team_name=fields["home_team_name"],
        away_team_id=int(fields["away_team_id"]),
        away_team_name=fields["away_team_name"],
        status="Live",
    )
    notifier_status = "batting" if state == "at_bat" else "on_deck"
    notifier = DiscordNotifier(fields["webhook_url"])
    try:
        await notifier.notify(
            player_id,
            fields["player_name"],
            notifier_status,
            game_info,
            int(fields["inning"]),
            fields["inning_half"],
            int(fields["outs"]),
        )
    except Exception as e:
        print(f"Discord delivery failed for user {user_id}: {e}")
        return  # Don't ACK — let the stream retain it for retry

    async with session_factory() as session:
        try:
            await _log_sent(event_id, user_id, player_id, state, session)
        except IntegrityError:
            pass  # Another worker raced us; ignore

    # pyrefly: ignore [not-async]
    await redis.xack(DELIVERIES_STREAM, DELIVERY_GROUP, msg_id)


async def delivery_once(
    redis: aioredis.Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Process all pending delivery messages once and return."""
    await _ensure_group(redis)
    # pyrefly: ignore [not-async]
    results = await redis.xreadgroup(
        DELIVERY_GROUP,
        DELIVERY_CONSUMER,
        {DELIVERIES_STREAM: ">"},
        count=100,
    )
    if not results:
        return
    for _stream, messages in results:
        for msg_id, fields in messages:
            try:
                await process_one(msg_id, fields, redis, session_factory)
            except Exception as e:
                print(f"Delivery error for msg {msg_id}: {e}")


async def run_delivery(
    redis: aioredis.Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _ensure_group(redis)
    print("Delivery worker started.")
    while True:
        try:
            # pyrefly: ignore [not-async]
            results = await redis.xreadgroup(
                DELIVERY_GROUP,
                DELIVERY_CONSUMER,
                {DELIVERIES_STREAM: ">"},
                count=10,
                block=5000,
            )
            for _stream, messages in results:
                for msg_id, fields in messages:
                    try:
                        await process_one(msg_id, fields, redis, session_factory)
                    except Exception as e:
                        print(f"Delivery error for msg {msg_id}: {e}")
        except Exception as e:
            print(f"Delivery worker error: {e}")
            await asyncio.sleep(1.0)
