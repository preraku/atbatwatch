"""Idempotency contract tests.

Verifies that running the pipeline twice with the same fixture does not
produce duplicate notifications or duplicate log rows.  Also asserts the
DB-level unique constraint on (event_id, user_id).

No atbatwatch imports.
"""

import uuid

_GAME_PK = 823475
_SCHEDULE_PATH = "schedule/schedule_823475_live.json"
_LIVE_FEED_PATH = "live_feed/in_progress_game_823475.json"
_BATTER_ID = 669016
_BATTER_NAME = "Brandon Marsh"


async def _run_full_pipeline(mlb_stub, run_worker):
    mlb_stub.configure(
        schedule_path=_SCHEDULE_PATH,
        game_pk=_GAME_PK,
        live_feed_path=_LIVE_FEED_PATH,
    )
    run_worker("poller", "poll-once")
    run_worker("fanout", "fanout-once")
    run_worker("delivery", "delivery-once")


async def test_second_pipeline_run_produces_no_extra_notifications(
    mlb_stub, redis_client, webhook_capture, db, http, run_worker
):
    """Running poll→fanout→delivery twice with the same game state emits only 1 webhook POST."""
    from acceptance.conftest import _WEBHOOK_CAPTURE_INTERNAL

    email = f"idem-{uuid.uuid4()}@example.com"
    webhook_id = str(uuid.uuid4())
    webhook_url = f"{_WEBHOOK_CAPTURE_INTERNAL}/hooks/{webhook_id}"

    resp = await http.post(
        "/auth/signup",
        json={"email": email, "password": "pass", "discord_webhook": webhook_url},
    )
    token = resp.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    await http.post(
        "/me/follows",
        json={"player_id": _BATTER_ID, "full_name": _BATTER_NAME},
        headers=auth,
    )

    await _run_full_pipeline(mlb_stub, run_worker)
    count_after_first = len(webhook_capture.get_captured(webhook_id=webhook_id))
    log_after_first = await db.fetchval("SELECT COUNT(*) FROM notification_log")

    # Second run — poller sees same state, should emit 0 new transitions
    await _run_full_pipeline(mlb_stub, run_worker)
    count_after_second = len(webhook_capture.get_captured(webhook_id=webhook_id))
    log_after_second = await db.fetchval("SELECT COUNT(*) FROM notification_log")

    assert count_after_first == 1
    assert count_after_second == 1, "second run must not produce extra webhooks"
    assert log_after_first == log_after_second, (
        "notification_log count must not change on second run"
    )


async def test_notification_log_unique_constraint(db):
    """Inserting a duplicate (event_id, user_id) into notification_log raises an error."""
    import asyncpg

    # Insert a user first to satisfy FK
    user_id = await db.fetchval(
        "INSERT INTO users (email, notification_target_type, notification_target_id) "
        "VALUES ($1, 'discord', 'http://x.invalid') RETURNING user_id",
        f"constraint-{uuid.uuid4()}@example.com",
    )

    event_id = str(uuid.uuid4())

    # Insert a player to satisfy FK
    player_id = 999999
    await db.execute(
        "INSERT INTO players (player_id, full_name) VALUES ($1, $2) "
        "ON CONFLICT DO NOTHING",
        player_id,
        "Test Player",
    )

    # First insert
    await db.execute(
        "INSERT INTO notification_log (event_id, user_id, player_id, state, status) "
        "VALUES ($1, $2, $3, 'at_bat', 'sent')",
        event_id,
        user_id,
        player_id,
    )

    # Second insert with same (event_id, user_id) must raise
    try:
        await db.execute(
            "INSERT INTO notification_log (event_id, user_id, player_id, state, status) "
            "VALUES ($1, $2, $3, 'at_bat', 'sent')",
            event_id,
            user_id,
            player_id,
        )
        raise AssertionError("expected IntegrityError, got none")
    except asyncpg.UniqueViolationError:
        pass  # expected
