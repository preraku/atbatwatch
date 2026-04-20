import copy
from unittest.mock import AsyncMock

from atbatwatch.games import GameInfo
from atbatwatch.notifier import RecordingNotifier
from atbatwatch.players import ResolvedPlayer
from atbatwatch.repos.follows import add_follow, create_user, upsert_player
from atbatwatch.watcher import _check_game, _DBFanOutNotifier, run_fixture

# IDs from fixtures/live_feed/in_progress_game_823475.json
BATTER_ID = 669016  # Brandon Marsh
ON_DECK_ID = 664761  # Alec Bohm
GAME_PK = 823475


def _game_info(game_pk: int = GAME_PK) -> GameInfo:
    return GameInfo(
        game_pk=game_pk,
        home_team_id=143,
        home_team_name="Philadelphia Phillies",
        away_team_id=144,
        away_team_name="Atlanta Braves",
        status="Live",
    )


async def test_batter_notification_fires(live_feed_in_progress, fake_redis):
    # Given: a live feed with a watched batter
    notifier = RecordingNotifier()
    players = [ResolvedPlayer(player_id=BATTER_ID, full_name="Brandon Marsh")]
    # When: checking the game
    await _check_game(
        GAME_PK, live_feed_in_progress, players, fake_redis, notifier, _game_info()
    )
    # Then: notification fires with correct status
    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["player_id"] == BATTER_ID
    assert call["player_name"] == "Brandon Marsh"
    assert call["status"] == "batting"


async def test_on_deck_notification_fires(live_feed_in_progress, fake_redis):
    # Given: a live feed with a watched player on deck
    notifier = RecordingNotifier()
    players = [ResolvedPlayer(player_id=ON_DECK_ID, full_name="Alec Bohm")]
    # When: checking the game
    await _check_game(
        GAME_PK, live_feed_in_progress, players, fake_redis, notifier, _game_info()
    )
    # Then: notification fires with on_deck status
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["status"] == "on_deck"


async def test_no_duplicate_notification_on_second_check(
    live_feed_in_progress, fake_redis
):
    # Given: a watched player and a notifier
    notifier = RecordingNotifier()
    players = [ResolvedPlayer(player_id=BATTER_ID, full_name="Brandon Marsh")]
    # When: checking the same game twice
    await _check_game(
        GAME_PK, live_feed_in_progress, players, fake_redis, notifier, _game_info()
    )
    await _check_game(
        GAME_PK, live_feed_in_progress, players, fake_redis, notifier, _game_info()
    )
    # Then: notification only fires once
    assert len(notifier.calls) == 1


async def test_no_notification_for_unwatched_player(live_feed_in_progress, fake_redis):
    # Given: a live feed with an unwatched player
    notifier = RecordingNotifier()
    players = [ResolvedPlayer(player_id=99999, full_name="Nobody")]
    # When: checking the game
    await _check_game(
        GAME_PK, live_feed_in_progress, players, fake_redis, notifier, _game_info()
    )
    # Then: no notification fires
    assert len(notifier.calls) == 0


async def test_no_notification_for_warmup_game(live_feed_warmup, fake_redis):
    # Given: a warmup game with a watched player
    notifier = RecordingNotifier()
    players = [ResolvedPlayer(player_id=BATTER_ID, full_name="Brandon Marsh")]
    game_pk = live_feed_warmup.get("gamePk", 0)
    # When: checking the warmup game
    await _check_game(
        game_pk, live_feed_warmup, players, fake_redis, notifier, _game_info(game_pk)
    )
    # Then: no notification fires
    assert len(notifier.calls) == 0


async def test_run_fixture_fires_on_batter(live_feed_in_progress, fake_redis):
    # Given: a live feed with a watched batter
    notifier = RecordingNotifier()
    players = [ResolvedPlayer(player_id=BATTER_ID, full_name="Brandon Marsh")]
    # When: running fixture
    await run_fixture(live_feed_in_progress, players, notifier, fake_redis)
    # Then: batting notification is fired
    assert any(c["status"] == "batting" for c in notifier.calls)


async def test_transition_from_on_deck_to_batting(live_feed_in_progress, fake_redis):
    # Given: a player initially on deck
    notifier = RecordingNotifier()
    players = [ResolvedPlayer(player_id=ON_DECK_ID, full_name="Alec Bohm")]
    # When: checking the first poll
    await _check_game(
        GAME_PK, live_feed_in_progress, players, fake_redis, notifier, _game_info()
    )
    assert notifier.calls[-1]["status"] == "on_deck"
    # And: the player transitions to batting in the next poll
    next_feed = copy.deepcopy(live_feed_in_progress)
    offense = next_feed["liveData"]["linescore"]["offense"]
    offense["batter"] = {"id": ON_DECK_ID, "fullName": "Alec Bohm", "link": ""}
    offense["onDeck"] = {"id": 99999, "fullName": "Someone Else", "link": ""}
    await _check_game(GAME_PK, next_feed, players, fake_redis, notifier, _game_info())
    # Then: both transitions are notified
    assert len(notifier.calls) == 2
    assert notifier.calls[1]["status"] == "batting"


async def test_db_fan_out_notifies_discord_follower(session, mocker):
    # Given: a follower and a player
    u = await create_user("fan@example.com", "https://discord.example/hook", session)
    await upsert_player(BATTER_ID, "Brandon Marsh", session)
    await add_follow(u.user_id, BATTER_ID, session)
    mock_notify = mocker.patch(
        "atbatwatch.notifier.DiscordNotifier.notify", new_callable=AsyncMock
    )
    console = RecordingNotifier()
    notifier = _DBFanOutNotifier(session, console)  # type: ignore[arg-type]
    # When: notifying about batting event
    await notifier.notify(
        BATTER_ID, "Brandon Marsh", "batting", _game_info(), 5, "Bot", 1
    )
    # Then: Discord is notified and console log created
    mock_notify.assert_called_once()
    assert len(console.calls) == 1
    assert console.calls[0]["status"] == "batting"


async def test_db_fan_out_no_followers_skips_discord(session, mocker):
    # Given: a player with no followers
    await upsert_player(BATTER_ID, "Brandon Marsh", session)
    mock_notify = mocker.patch(
        "atbatwatch.notifier.DiscordNotifier.notify", new_callable=AsyncMock
    )
    console = RecordingNotifier()
    notifier = _DBFanOutNotifier(session, console)  # type: ignore[arg-type]
    # When: notifying about batting event
    await notifier.notify(
        BATTER_ID, "Brandon Marsh", "batting", _game_info(), 5, "Bot", 1
    )
    # Then: Discord is not called but console logs
    mock_notify.assert_not_called()
    assert len(console.calls) == 1


async def test_db_fan_out_discord_failure_does_not_propagate(session, mocker):
    # Given: a follower and Discord notifier throws
    u = await create_user("fan@example.com", "https://discord.example/hook", session)
    await upsert_player(BATTER_ID, "Brandon Marsh", session)
    await add_follow(u.user_id, BATTER_ID, session)
    mocker.patch(
        "atbatwatch.notifier.DiscordNotifier.notify",
        new_callable=AsyncMock,
        side_effect=Exception("network error"),
    )
    console = RecordingNotifier()
    notifier = _DBFanOutNotifier(session, console)  # type: ignore[arg-type]
    # When: notifying about batting event
    await notifier.notify(
        BATTER_ID, "Brandon Marsh", "batting", _game_info(), 5, "Bot", 1
    )
    # Then: error doesn't propagate and console still logs
    assert len(console.calls) == 1


async def test_db_fan_out_multiple_followers_each_get_discord(session, mocker):
    # Given: multiple followers for a player
    u1 = await create_user("fan1@example.com", "https://discord.example/hook1", session)
    u2 = await create_user("fan2@example.com", "https://discord.example/hook2", session)
    await upsert_player(BATTER_ID, "Brandon Marsh", session)
    await add_follow(u1.user_id, BATTER_ID, session)
    await add_follow(u2.user_id, BATTER_ID, session)
    mock_notify = mocker.patch(
        "atbatwatch.notifier.DiscordNotifier.notify", new_callable=AsyncMock
    )
    notifier = _DBFanOutNotifier(session, RecordingNotifier())  # type: ignore[arg-type]
    # When: notifying about event
    await notifier.notify(
        BATTER_ID, "Brandon Marsh", "on_deck", _game_info(), 3, "Top", 2
    )
    # Then: Discord is called for each follower
    assert mock_notify.call_count == 2
