from atbatwatch.db import Player
from atbatwatch.repos.follows import (
    add_follow,
    create_user,
    get_followers,
    get_user_by_email,
    remove_follow,
    upsert_player,
)


async def test_create_user(session):
    # Given: nothing
    # When: creating a user
    u = await create_user("fan@example.com", "https://discord.example/webhook", session)
    # Then: user is created with correct properties
    assert u.user_id is not None
    assert u.email == "fan@example.com"
    assert u.notification_target_type == "discord"


async def test_get_user_by_email(session):
    # Given: a user exists
    await create_user("fan@example.com", "https://discord.example/webhook", session)
    # When: querying user by email
    u = await get_user_by_email("fan@example.com", session)
    # Then: user is retrieved with correct email
    assert u is not None
    assert u.email == "fan@example.com"


async def test_get_user_by_email_missing(session):
    # Given: no user exists with this email
    # When: querying for a non-existent user
    result = await get_user_by_email("nobody@example.com", session)
    # Then: return None
    assert result is None


async def test_upsert_player_create(session):
    # Given: no player exists
    # When: upserting a player
    await upsert_player(660271, "Shohei Ohtani", session)
    # Then: player is created with correct name
    p = await session.get(Player, 660271)
    assert p is not None
    assert p.full_name == "Shohei Ohtani"


async def test_upsert_player_update(session):
    # Given: a player exists
    await upsert_player(660271, "Shohei Ohtani", session)
    # When: upserting the same player with a different name
    await upsert_player(660271, "S. Ohtani", session)
    # Then: player name is updated
    p = await session.get(Player, 660271)
    assert p is not None
    assert p.full_name == "S. Ohtani"


async def test_add_follow_and_get_followers(session):
    # Given: a user and player exist
    u = await create_user("fan@example.com", "https://discord.example/webhook", session)
    await upsert_player(660271, "Shohei Ohtani", session)
    # When: adding a follow
    await add_follow(u.user_id, 660271, session)
    # Then: player's followers include the user
    followers = await get_followers(660271, session)
    assert len(followers) == 1
    assert followers[0].email == "fan@example.com"


async def test_add_follow_idempotent(session):
    # Given: a user and player exist
    u = await create_user("fan@example.com", "https://discord.example/webhook", session)
    await upsert_player(660271, "Shohei Ohtani", session)
    # When: adding the same follow twice
    await add_follow(u.user_id, 660271, session)
    await add_follow(u.user_id, 660271, session)
    # Then: only one follow exists
    followers = await get_followers(660271, session)
    assert len(followers) == 1


async def test_get_followers_empty(session):
    # Given: a player exists with no followers
    await upsert_player(660271, "Shohei Ohtani", session)
    # When: getting followers
    followers = await get_followers(660271, session)
    # Then: empty list is returned
    assert followers == []


async def test_multiple_followers(session):
    # Given: two users and a player exist
    u1 = await create_user("fan1@example.com", "https://discord.example/hook1", session)
    u2 = await create_user("fan2@example.com", "https://discord.example/hook2", session)
    await upsert_player(660271, "Shohei Ohtani", session)
    # When: both users follow the player
    await add_follow(u1.user_id, 660271, session)
    await add_follow(u2.user_id, 660271, session)
    # Then: both followers are returned
    followers = await get_followers(660271, session)
    assert len(followers) == 2


async def test_remove_follow(session):
    # Given: a user follows a player
    u = await create_user("fan@example.com", "https://discord.example/webhook", session)
    await upsert_player(660271, "Shohei Ohtani", session)
    await add_follow(u.user_id, 660271, session)
    # When: removing the follow
    removed = await remove_follow(u.user_id, 660271, session)
    # Then: removal succeeds and no followers remain
    assert removed is True
    followers = await get_followers(660271, session)
    assert followers == []


async def test_remove_follow_not_found(session):
    # Given: a user and player exist but are not followed
    u = await create_user("fan@example.com", "https://discord.example/webhook", session)
    await upsert_player(660271, "Shohei Ohtani", session)
    # When: removing a non-existent follow
    removed = await remove_follow(u.user_id, 660271, session)
    # Then: removal returns False
    assert removed is False
