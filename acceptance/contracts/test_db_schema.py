"""Database schema contract tests.

Introspects the live Postgres instance via information_schema and tests
constraint enforcement.  No atbatwatch imports.
"""

import uuid

import asyncpg
import pytest

# ---------------------------------------------------------------------------
# Table / column presence
# ---------------------------------------------------------------------------


async def test_all_four_tables_exist(db):
    rows = await db.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    tables = {r["table_name"] for r in rows}
    assert {"users", "players", "follows", "notification_log"} <= tables


async def test_users_columns(db):
    rows = await db.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'users'"
    )
    cols = {r["column_name"] for r in rows}
    assert {
        "user_id",
        "email",
        "password_hash",
        "notification_target_type",
        "notification_target_id",
        "created_at",
    } <= cols


async def test_players_columns(db):
    rows = await db.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'players'"
    )
    cols = {r["column_name"] for r in rows}
    assert {"player_id", "full_name"} <= cols


async def test_follows_columns(db):
    rows = await db.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'follows'"
    )
    cols = {r["column_name"] for r in rows}
    assert {"user_id", "player_id", "notify_at_bat", "notify_on_deck"} <= cols


async def test_notification_log_columns(db):
    rows = await db.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'notification_log'"
    )
    cols = {r["column_name"] for r in rows}
    assert {"id", "event_id", "user_id", "player_id", "state", "status", "game_id"} <= cols


# ---------------------------------------------------------------------------
# Constraint enforcement
# ---------------------------------------------------------------------------


async def test_users_email_unique(db):
    email = f"dup-schema-{uuid.uuid4()}@example.com"
    await db.execute(
        "INSERT INTO users (email, notification_target_type, notification_target_id) "
        "VALUES ($1, 'discord', 'http://x.invalid')",
        email,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            "INSERT INTO users (email, notification_target_type, notification_target_id) "
            "VALUES ($1, 'discord', 'http://x.invalid')",
            email,
        )


async def test_follows_notification_prefs_default_to_true(db):
    """New follows must default notify_at_bat and notify_on_deck to TRUE."""
    user_id = await db.fetchval(
        "INSERT INTO users (email, notification_target_type, notification_target_id) "
        "VALUES ($1, 'discord', 'http://x.invalid') RETURNING user_id",
        f"prefs-default-{uuid.uuid4()}@example.com",
    )
    await db.execute(
        "INSERT INTO players (player_id, full_name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        999995,
        "Prefs Default Player",
    )
    await db.execute(
        "INSERT INTO follows (user_id, player_id) VALUES ($1, $2)",
        user_id,
        999995,
    )
    row = await db.fetchrow(
        "SELECT notify_at_bat, notify_on_deck FROM follows WHERE user_id=$1 AND player_id=$2",
        user_id,
        999995,
    )
    assert row["notify_at_bat"] is True
    assert row["notify_on_deck"] is True


async def test_follows_primary_key_is_user_and_player(db):
    """Duplicate (user_id, player_id) insert into follows must raise."""
    user_id = await db.fetchval(
        "INSERT INTO users (email, notification_target_type, notification_target_id) "
        "VALUES ($1, 'discord', 'http://x.invalid') RETURNING user_id",
        f"follows-pk-{uuid.uuid4()}@example.com",
    )
    await db.execute(
        "INSERT INTO players (player_id, full_name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        999998,
        "Schema Test Player",
    )
    await db.execute(
        "INSERT INTO follows (user_id, player_id) VALUES ($1, $2)",
        user_id,
        999998,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            "INSERT INTO follows (user_id, player_id) VALUES ($1, $2)",
            user_id,
            999998,
        )


async def test_notification_log_unique_on_event_id_and_user_id(db):
    user_id = await db.fetchval(
        "INSERT INTO users (email, notification_target_type, notification_target_id) "
        "VALUES ($1, 'discord', 'http://x.invalid') RETURNING user_id",
        f"notif-uniq-{uuid.uuid4()}@example.com",
    )
    await db.execute(
        "INSERT INTO players (player_id, full_name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        999997,
        "Notif Test Player",
    )
    event_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO notification_log (event_id, user_id, player_id, state, status) "
        "VALUES ($1, $2, $3, 'at_bat', 'sent')",
        event_id,
        user_id,
        999997,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            "INSERT INTO notification_log (event_id, user_id, player_id, state, status) "
            "VALUES ($1, $2, $3, 'at_bat', 'sent')",
            event_id,
            user_id,
            999997,
        )


async def test_notification_log_status_is_set_after_delivery(db):
    """notification_log.status column exists and can hold 'sent'."""
    user_id = await db.fetchval(
        "INSERT INTO users (email, notification_target_type, notification_target_id) "
        "VALUES ($1, 'discord', 'http://x.invalid') RETURNING user_id",
        f"status-{uuid.uuid4()}@example.com",
    )
    await db.execute(
        "INSERT INTO players (player_id, full_name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        999996,
        "Status Test Player",
    )
    event_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO notification_log (event_id, user_id, player_id, state, status) "
        "VALUES ($1, $2, $3, 'at_bat', 'sent')",
        event_id,
        user_id,
        999996,
    )
    status = await db.fetchval(
        "SELECT status FROM notification_log WHERE event_id = $1 AND user_id = $2",
        event_id,
        user_id,
    )
    assert status == "sent"
