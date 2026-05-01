"""End-to-end pipeline tests: poll → fanout → delivery → webhook + DB.

Each test drives the full pipeline via external surfaces only:
  - HTTP for user setup
  - mlb_stub to control what the poller sees
  - run_worker to step each worker once
  - webhook_capture to assert outbound POSTs
  - db for direct Postgres assertions
  - redis_client for stream inspection

No atbatwatch imports.
"""

import uuid

_GAME_PK = 823475
_SCHEDULE_PATH = "schedule/schedule_823475_live.json"
_LIVE_FEED_PATH = "live_feed/in_progress_game_823475.json"

# Known values from the fixture
_BATTER_ID = 669016
_BATTER_NAME = "Brandon Marsh"
_ONDECK_ID = 664761
_ONDECK_NAME = "Alec Bohm"
_HOME_TEAM = "Philadelphia Phillies"
_AWAY_TEAM = "Atlanta Braves"
_INNING = 6
_INNING_HALF = "Bot"
_OUTS = 0

# Expected Discord content strings (byte-identical to notifier.py output)
_EXPECTED_AT_BAT = (
    f"⚾ **AT BAT**: **{_BATTER_NAME}** "
    f"({_AWAY_TEAM} @ {_HOME_TEAM} — {_INNING_HALF} {_INNING}, {_OUTS} outs)"
)
_EXPECTED_ON_DECK = (
    f"🔄 **ON DECK**: **{_ONDECK_NAME}** "
    f"({_AWAY_TEAM} @ {_HOME_TEAM} — {_INNING_HALF} {_INNING}, {_OUTS} outs)"
)


def _configure_stub(mlb_stub):
    mlb_stub.configure(
        schedule_path=_SCHEDULE_PATH,
        game_pk=_GAME_PK,
        live_feed_path=_LIVE_FEED_PATH,
    )


async def _create_user_follow(
    http, player_id: int, player_name: str
) -> tuple[str, str]:
    """Returns (webhook_id, token)."""
    from acceptance.conftest import _WEBHOOK_CAPTURE_INTERNAL

    email = f"e2e-{uuid.uuid4()}@example.com"
    webhook_id = str(uuid.uuid4())
    webhook_url = f"{_WEBHOOK_CAPTURE_INTERNAL}/hooks/{webhook_id}"

    resp = await http.post(
        "/auth/signup",
        json={"email": email, "password": "pass", "discord_webhook": webhook_url},
    )
    assert resp.status_code == 201
    token = resp.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    follow_resp = await http.post(
        "/me/follows",
        json={"player_id": player_id, "full_name": player_name},
        headers=auth,
    )
    assert follow_resp.status_code == 201
    return webhook_id, token


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_single_follower_receives_notification(
    mlb_stub, redis_client, webhook_capture, db, http, run_worker
):
    """One user following the batter gets exactly one POST with the correct content."""
    webhook_id, _ = await _create_user_follow(http, _BATTER_ID, _BATTER_NAME)
    _configure_stub(mlb_stub)

    run_worker("poller", "poll-once")
    run_worker("fanout", "fanout-once")
    run_worker("delivery", "delivery-once")

    captured = webhook_capture.get_captured(webhook_id=webhook_id)
    assert len(captured) == 1
    assert captured[0]["body"]["content"] == _EXPECTED_AT_BAT

    row = await db.fetchrow(
        "SELECT event_id, status FROM notification_log WHERE user_id = ("
        "  SELECT user_id FROM users WHERE notification_target_id LIKE $1"
        ")",
        f"%{webhook_id}%",
    )
    assert row is not None
    assert row["status"] == "sent"


async def test_two_followers_each_get_notification(
    mlb_stub, redis_client, webhook_capture, db, http, run_worker
):
    """Two users following the same player each receive one POST."""
    wh_a, _ = await _create_user_follow(http, _BATTER_ID, _BATTER_NAME)
    wh_b, _ = await _create_user_follow(http, _BATTER_ID, _BATTER_NAME)
    _configure_stub(mlb_stub)

    run_worker("poller", "poll-once")
    run_worker("fanout", "fanout-once")
    run_worker("delivery", "delivery-once")

    for wh in (wh_a, wh_b):
        captured = webhook_capture.get_captured(webhook_id=wh)
        assert len(captured) == 1, f"expected 1 POST for {wh}, got {len(captured)}"
        assert captured[0]["body"]["content"] == _EXPECTED_AT_BAT

    log_count = await db.fetchval("SELECT COUNT(*) FROM notification_log")
    assert log_count == 2


async def test_on_deck_notification_uses_correct_label(
    mlb_stub, redis_client, webhook_capture, db, http, run_worker
):
    """Follower of the on-deck player gets the 🔄 ON DECK label."""
    wh, _ = await _create_user_follow(http, _ONDECK_ID, _ONDECK_NAME)
    _configure_stub(mlb_stub)

    run_worker("poller", "poll-once")
    run_worker("fanout", "fanout-once")
    run_worker("delivery", "delivery-once")

    captured = webhook_capture.get_captured(webhook_id=wh)
    assert len(captured) == 1
    assert captured[0]["body"]["content"] == _EXPECTED_ON_DECK


async def test_no_followers_means_no_delivery(
    mlb_stub, redis_client, webhook_capture, db, http, run_worker
):
    """When no one follows the active players, fanout writes 0 deliveries."""
    _configure_stub(mlb_stub)

    run_worker("poller", "poll-once")
    run_worker("fanout", "fanout-once")
    run_worker("delivery", "delivery-once")

    all_captured = webhook_capture.get_captured()
    assert len(all_captured) == 0

    log_count = await db.fetchval("SELECT COUNT(*) FROM notification_log")
    assert log_count == 0


async def test_notification_content_string_is_byte_identical(
    mlb_stub, redis_client, webhook_capture, db, http, run_worker
):
    """Verify the exact content string format including the em-dash (U+2014)."""
    wh_batter, _ = await _create_user_follow(http, _BATTER_ID, _BATTER_NAME)
    wh_deck, _ = await _create_user_follow(http, _ONDECK_ID, _ONDECK_NAME)
    _configure_stub(mlb_stub)

    run_worker("poller", "poll-once")
    run_worker("fanout", "fanout-once")
    run_worker("delivery", "delivery-once")

    batter_captured = webhook_capture.get_captured(webhook_id=wh_batter)
    deck_captured = webhook_capture.get_captured(webhook_id=wh_deck)
    assert len(batter_captured) == 1
    assert len(deck_captured) == 1

    batter_content = batter_captured[0]["body"]["content"]
    deck_content = deck_captured[0]["body"]["content"]

    # Verify em-dash (U+2014) not hyphen
    assert "—" in batter_content
    assert "—" in deck_content

    # Byte-identical expected strings
    assert batter_content == _EXPECTED_AT_BAT
    assert deck_content == _EXPECTED_ON_DECK
