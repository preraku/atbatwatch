"""Notification preference contract tests.

Verifies per-player notify_at_bat / notify_on_deck preferences are respected
throughout the full pipeline and that PATCH /me/follows/{player_id} works.

No atbatwatch imports.
"""

import uuid

_GAME_PK = 823475
_SCHEDULE_PATH = "schedule/schedule_823475_live.json"
_LIVE_FEED_PATH = "live_feed/in_progress_game_823475.json"

# Known values from in_progress_game_823475.json
_BATTER_ID = 669016
_BATTER_NAME = "Brandon Marsh"
_ONDECK_ID = 664761
_ONDECK_NAME = "Alec Bohm"


def _configure_stub(mlb_stub):
    mlb_stub.configure(
        schedule_path=_SCHEDULE_PATH,
        game_pk=_GAME_PK,
        live_feed_path=_LIVE_FEED_PATH,
    )


async def _make_follower(
    http,
    player_id: int,
    player_name: str,
    notify_at_bat: bool,
    notify_on_deck: bool,
) -> tuple[str, str]:
    """Sign up, follow a player, PATCH prefs. Returns (webhook_id, token)."""
    from acceptance.conftest import _WEBHOOK_CAPTURE_INTERNAL

    email = f"prefs-{uuid.uuid4()}@example.com"
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

    patch_resp = await http.patch(
        f"/me/follows/{player_id}",
        json={"notify_at_bat": notify_at_bat, "notify_on_deck": notify_on_deck},
        headers=auth,
    )
    assert patch_resp.status_code == 204

    return webhook_id, token


# ---------------------------------------------------------------------------
# PATCH endpoint contract
# ---------------------------------------------------------------------------


async def test_patch_prefs_reflected_in_get_follows(http, db):
    """PATCH /me/follows/{id} updates the prefs returned by GET /me/follows."""
    email = f"patch-{uuid.uuid4()}@example.com"
    resp = await http.post(
        "/auth/signup",
        json={"email": email, "password": "pass", "discord_webhook": "http://x.invalid/h"},
    )
    token = resp.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    await http.post(
        "/me/follows",
        json={"player_id": _BATTER_ID, "full_name": _BATTER_NAME},
        headers=auth,
    )

    # New follows default to both true
    get_resp = await http.get("/me/follows", headers=auth)
    follow = get_resp.json()["follows"][0]
    assert follow["notify_at_bat"] is True
    assert follow["notify_on_deck"] is True

    # Switch to at_bat-only
    patch_resp = await http.patch(
        f"/me/follows/{_BATTER_ID}",
        json={"notify_at_bat": True, "notify_on_deck": False},
        headers=auth,
    )
    assert patch_resp.status_code == 204

    get_resp2 = await http.get("/me/follows", headers=auth)
    follow2 = get_resp2.json()["follows"][0]
    assert follow2["notify_at_bat"] is True
    assert follow2["notify_on_deck"] is False


async def test_patch_nonexistent_follow_returns_404(http, db):
    """PATCH a follow the user does not own returns 404."""
    email = f"patch404-{uuid.uuid4()}@example.com"
    resp = await http.post(
        "/auth/signup",
        json={"email": email, "password": "pass", "discord_webhook": "http://x.invalid/h"},
    )
    token = resp.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    patch_resp = await http.patch(
        f"/me/follows/{_BATTER_ID}",
        json={"notify_at_bat": True, "notify_on_deck": False},
        headers=auth,
    )
    assert patch_resp.status_code == 404


async def test_patch_requires_auth(http, db):
    """PATCH without a token is rejected."""
    resp = await http.patch(
        f"/me/follows/{_BATTER_ID}",
        json={"notify_at_bat": True, "notify_on_deck": False},
    )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Pipeline filtering
# ---------------------------------------------------------------------------


async def test_at_bat_only_pref_suppresses_on_deck_notification(
    mlb_stub, redis_client, webhook_capture, db, http, run_worker
):
    """User with at_bat-only pref following the on-deck player gets no notification."""
    wh, _ = await _make_follower(
        http, _ONDECK_ID, _ONDECK_NAME, notify_at_bat=True, notify_on_deck=False
    )
    _configure_stub(mlb_stub)

    run_worker("poller", "poll-once")
    run_worker("fanout", "fanout-once")
    run_worker("delivery", "delivery-once")

    assert len(webhook_capture.get_captured(webhook_id=wh)) == 0


async def test_on_deck_only_pref_delivers_on_deck_notification(
    mlb_stub, redis_client, webhook_capture, db, http, run_worker
):
    """User with on_deck-only pref following the on-deck player receives one notification."""
    wh, _ = await _make_follower(
        http, _ONDECK_ID, _ONDECK_NAME, notify_at_bat=False, notify_on_deck=True
    )
    _configure_stub(mlb_stub)

    run_worker("poller", "poll-once")
    run_worker("fanout", "fanout-once")
    run_worker("delivery", "delivery-once")

    captured = webhook_capture.get_captured(webhook_id=wh)
    assert len(captured) == 1
    assert "ON DECK" in captured[0]["body"]["content"]


async def test_game_start_notifies_on_deck_only_follower(
    mlb_stub, redis_client, webhook_capture, db, http, run_worker
):
    """User with on_deck-only pref following the batter is notified when no prior
    on_deck notification exists in the game (covers game-start and pinch-hitter)."""
    wh, _ = await _make_follower(
        http, _BATTER_ID, _BATTER_NAME, notify_at_bat=False, notify_on_deck=True
    )
    _configure_stub(mlb_stub)

    run_worker("poller", "poll-once")
    run_worker("fanout", "fanout-once")
    run_worker("delivery", "delivery-once")

    # Batter went at_bat with no prior on_deck in the log — must still notify.
    captured = webhook_capture.get_captured(webhook_id=wh)
    assert len(captured) == 1
    assert "AT BAT" in captured[0]["body"]["content"]


async def test_both_prefs_false_delivers_nothing(
    mlb_stub, redis_client, webhook_capture, db, http, run_worker
):
    """User with both prefs disabled receives no notifications."""
    wh, _ = await _make_follower(
        http, _BATTER_ID, _BATTER_NAME, notify_at_bat=False, notify_on_deck=False
    )
    _configure_stub(mlb_stub)

    run_worker("poller", "poll-once")
    run_worker("fanout", "fanout-once")
    run_worker("delivery", "delivery-once")

    assert len(webhook_capture.get_captured(webhook_id=wh)) == 0
