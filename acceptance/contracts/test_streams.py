"""Redis stream contract tests.

Tests assert the field schema of events:transitions and events:deliveries,
without importing any application code.
"""

import re
import uuid

# ---------------------------------------------------------------------------
# Fixtures that get pushed to the mlb-stub before running workers
# ---------------------------------------------------------------------------

_GAME_PK = 823475
_SCHEDULE_PATH = "schedule/schedule_823475_live.json"
_LIVE_FEED_PATH = "live_feed/in_progress_game_823475.json"

# Known values from in_progress_game_823475.json
_EXPECTED_BATTER_ID = "669016"
_EXPECTED_ON_DECK_ID = "664761"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*\+00:00$")
_INT_STR_RE = re.compile(r"^\d+$")

_REQUIRED_TRANSITION_FIELDS = {
    "event_id",
    "game_id",
    "player_id",
    "player_name",
    "state",
    "home_team_id",
    "home_team_name",
    "away_team_id",
    "away_team_name",
    "inning",
    "inning_half",
    "outs",
    "occurred_at",
}


async def test_poll_once_emits_exactly_2_transitions(
    mlb_stub, redis_client, run_worker
):
    mlb_stub.configure(
        schedule_path=_SCHEDULE_PATH,
        game_pk=_GAME_PK,
        live_feed_path=_LIVE_FEED_PATH,
    )

    run_worker("poller", "poll-once")

    messages = await redis_client.xrange("events:transitions", "-", "+")
    assert len(messages) == 2


async def test_transitions_states_are_at_bat_and_on_deck(
    mlb_stub, redis_client, run_worker
):
    mlb_stub.configure(
        schedule_path=_SCHEDULE_PATH,
        game_pk=_GAME_PK,
        live_feed_path=_LIVE_FEED_PATH,
    )

    run_worker("poller", "poll-once")

    messages = await redis_client.xrange("events:transitions", "-", "+")
    states = {fields["state"] for _, fields in messages}
    assert states == {"at_bat", "on_deck"}


async def test_transition_fields_schema(mlb_stub, redis_client, run_worker):
    """Every transition message must have exactly the 13 documented fields."""
    mlb_stub.configure(
        schedule_path=_SCHEDULE_PATH,
        game_pk=_GAME_PK,
        live_feed_path=_LIVE_FEED_PATH,
    )

    run_worker("poller", "poll-once")

    messages = await redis_client.xrange("events:transitions", "-", "+")
    assert messages, "expected at least one transition message"

    for _msg_id, fields in messages:
        assert set(fields.keys()) == _REQUIRED_TRANSITION_FIELDS, (
            f"unexpected fields: {set(fields.keys()) ^ _REQUIRED_TRANSITION_FIELDS}"
        )

        assert _UUID_RE.match(fields["event_id"]), (
            f"event_id is not UUIDv4: {fields['event_id']}"
        )
        assert _INT_STR_RE.match(fields["game_id"]), (
            f"game_id not int-as-string: {fields['game_id']}"
        )
        assert _INT_STR_RE.match(fields["player_id"])
        assert _INT_STR_RE.match(fields["home_team_id"])
        assert _INT_STR_RE.match(fields["away_team_id"])
        assert _INT_STR_RE.match(fields["inning"])
        assert _INT_STR_RE.match(fields["outs"])
        assert fields["state"] in ("at_bat", "on_deck")
        assert fields["inning_half"] in ("Top", "Bot")
        assert _ISO_UTC_RE.match(fields["occurred_at"]), (
            f"occurred_at not ISO 8601 UTC: {fields['occurred_at']}"
        )


async def test_warmup_fixture_emits_no_transitions(mlb_stub, redis_client, run_worker):
    mlb_stub.configure(
        schedule_path=_SCHEDULE_PATH,
        game_pk=_GAME_PK,
        live_feed_path="live_feed/warmup_game_824370.json",
    )
    # Note: warmup_game has a different game_pk but we're overriding live feed path only.
    # The schedule still references 823475 as Live; it will fetch live feed for 823475
    # and get the warmup body — which is_game_in_progress() will reject.

    run_worker("poller", "poll-once")

    messages = await redis_client.xrange("events:transitions", "-", "+")
    assert len(messages) == 0


async def test_final_fixture_emits_no_transitions(mlb_stub, redis_client, run_worker):
    mlb_stub.configure(
        schedule_path=_SCHEDULE_PATH,
        game_pk=_GAME_PK,
        live_feed_path="live_feed/final_game_822750.json",
    )

    run_worker("poller", "poll-once")

    messages = await redis_client.xrange("events:transitions", "-", "+")
    assert len(messages) == 0


async def test_second_identical_poll_emits_no_new_transitions(
    mlb_stub, redis_client, run_worker
):
    """Second poll with same game state should produce zero new messages (cached)."""
    mlb_stub.configure(
        schedule_path=_SCHEDULE_PATH,
        game_pk=_GAME_PK,
        live_feed_path=_LIVE_FEED_PATH,
    )

    run_worker("poller", "poll-once")
    count_after_first = len(await redis_client.xrange("events:transitions", "-", "+"))

    run_worker("poller", "poll-once")
    count_after_second = len(await redis_client.xrange("events:transitions", "-", "+"))

    assert count_after_second == count_after_first


async def test_fanout_produces_delivery_per_follower(
    mlb_stub, redis_client, db, http, run_worker
):
    """For each transition, fanout must write exactly one delivery per follower."""
    # Create a user and follow both players
    from acceptance.conftest import _WEBHOOK_CAPTURE_INTERNAL

    email = f"fanout-{uuid.uuid4()}@example.com"
    webhook_id = str(uuid.uuid4())
    webhook_url = f"{_WEBHOOK_CAPTURE_INTERNAL}/hooks/{webhook_id}"

    resp = await http.post(
        "/auth/signup",
        json={"email": email, "password": "pass", "discord_webhook": webhook_url},
    )
    assert resp.status_code == 201
    token = resp.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    await http.post(
        "/me/follows",
        json={"player_id": 669016, "full_name": "Brandon Marsh"},
        headers=auth,
    )

    mlb_stub.configure(
        schedule_path=_SCHEDULE_PATH,
        game_pk=_GAME_PK,
        live_feed_path=_LIVE_FEED_PATH,
    )

    run_worker("poller", "poll-once")
    transitions = await redis_client.xrange("events:transitions", "-", "+")
    assert len(transitions) == 2

    run_worker("fanout", "fanout-once")
    deliveries = await redis_client.xrange("events:deliveries", "-", "+")

    # Only 1 follower for player 669016 (the at-bat), 0 for 664761 (on-deck)
    assert len(deliveries) == 1

    _, fields = deliveries[0]
    assert fields["player_id"] == "669016"
    assert fields["user_id"] is not None
    assert fields["webhook_url"] == webhook_url

    # All transition fields must pass through unchanged
    transition_fields = dict(transitions[0][1])
    for key in _REQUIRED_TRANSITION_FIELDS:
        assert fields[key] == transition_fields[key], f"field {key} changed in fanout"


async def test_fanout_delivery_fields_include_user_fields(
    mlb_stub, redis_client, db, http, run_worker
):
    """events:deliveries must contain all transition fields plus user_id and webhook_url."""
    from acceptance.conftest import _WEBHOOK_CAPTURE_INTERNAL

    email = f"fanout2-{uuid.uuid4()}@example.com"
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
        json={"player_id": 669016, "full_name": "Brandon Marsh"},
        headers=auth,
    )

    mlb_stub.configure(
        schedule_path=_SCHEDULE_PATH,
        game_pk=_GAME_PK,
        live_feed_path=_LIVE_FEED_PATH,
    )
    run_worker("poller", "poll-once")
    run_worker("fanout", "fanout-once")

    deliveries = await redis_client.xrange("events:deliveries", "-", "+")
    assert deliveries

    _, fields = deliveries[0]
    expected_keys = _REQUIRED_TRANSITION_FIELDS | {"user_id", "webhook_url"}
    assert set(fields.keys()) == expected_keys
    assert _INT_STR_RE.match(fields["user_id"])
    assert fields["webhook_url"].startswith("http")
