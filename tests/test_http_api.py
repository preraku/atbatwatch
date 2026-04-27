from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from atbatwatch.db import Base
from atbatwatch.http_api import _db, app


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[_db] = override_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture
async def auth_client(client):
    # Given: a registered user
    resp = await client.post(
        "/auth/signup",
        json={
            "email": "test@example.com",
            "password": "password123",
            "discord_webhook": "https://discord.example/webhook",
        },
    )
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def test_signup_returns_token(client):
    # Given: no existing account
    # When: signing up with valid credentials
    resp = await client.post(
        "/auth/signup",
        json={
            "email": "user@example.com",
            "password": "secret",
            "discord_webhook": "https://discord.example/webhook",
        },
    )
    # Then: returns 201 with a JWT token
    assert resp.status_code == 201
    data = resp.json()
    assert "token" in data
    assert isinstance(data["token"], str)


async def test_signup_duplicate_email(client):
    # Given: an account already exists for an email
    payload = {
        "email": "dupe@example.com",
        "password": "secret",
        "discord_webhook": "https://discord.example/webhook",
    }
    await client.post("/auth/signup", json=payload)
    # When: signing up again with the same email
    resp = await client.post("/auth/signup", json=payload)
    # Then: returns 409 conflict
    assert resp.status_code == 409


async def test_login_returns_token(client):
    # Given: an existing account
    await client.post(
        "/auth/signup",
        json={
            "email": "user@example.com",
            "password": "secret",
            "discord_webhook": "https://discord.example/webhook",
        },
    )
    # When: logging in with correct credentials
    resp = await client.post(
        "/auth/login",
        json={"email": "user@example.com", "password": "secret"},
    )
    # Then: returns 200 with a JWT token
    assert resp.status_code == 200
    assert "token" in resp.json()


async def test_login_wrong_password(client):
    # Given: an existing account
    await client.post(
        "/auth/signup",
        json={
            "email": "user@example.com",
            "password": "secret",
            "discord_webhook": "https://discord.example/webhook",
        },
    )
    # When: logging in with the wrong password
    resp = await client.post(
        "/auth/login",
        json={"email": "user@example.com", "password": "wrong"},
    )
    # Then: returns 401 unauthorized
    assert resp.status_code == 401


async def test_login_unknown_email(client):
    # Given: no account exists for the email
    # When: attempting to log in
    resp = await client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "secret"},
    )
    # Then: returns 401 unauthorized
    assert resp.status_code == 401


async def test_requires_token(client):
    # Given: no Authorization header
    # When: accessing a protected endpoint
    resp = await client.get("/me/follows")
    # Then: returns 401
    assert resp.status_code == 401


async def test_list_follows_empty(auth_client):
    # Given: authenticated user with no follows
    # When: listing follows
    resp = await auth_client.get("/me/follows")
    # Then: returns empty list
    assert resp.status_code == 200
    assert resp.json() == {"follows": []}


async def test_follow_and_list(auth_client):
    # Given: authenticated user
    # When: following a player then listing follows
    resp = await auth_client.post(
        "/me/follows",
        json={
            "player_id": 660271,
            "full_name": "Shohei Ohtani",
            "team": "Los Angeles Dodgers",
            "position": "DH",
        },
    )
    assert resp.status_code == 201
    follows = (await auth_client.get("/me/follows")).json()["follows"]
    # Then: player appears in the list with correct fields
    assert len(follows) == 1
    assert follows[0]["player_id"] == 660271
    assert follows[0]["full_name"] == "Shohei Ohtani"
    assert follows[0]["team"] == "Los Angeles Dodgers"
    assert follows[0]["position"] == "DH"


async def test_follow_idempotent(auth_client):
    # Given: authenticated user
    # When: following the same player twice
    payload = {"player_id": 660271, "full_name": "Shohei Ohtani"}
    await auth_client.post("/me/follows", json=payload)
    await auth_client.post("/me/follows", json=payload)
    # Then: exactly one follow exists
    resp = await auth_client.get("/me/follows")
    assert len(resp.json()["follows"]) == 1


async def test_unfollow(auth_client):
    # Given: authenticated user following a player
    await auth_client.post(
        "/me/follows", json={"player_id": 660271, "full_name": "Shohei Ohtani"}
    )
    # When: unfollowing that player
    resp = await auth_client.delete("/me/follows/660271")
    # Then: returns 204 and follow list is empty
    assert resp.status_code == 204
    follows = (await auth_client.get("/me/follows")).json()["follows"]
    assert follows == []


async def test_unfollow_not_found(auth_client):
    # Given: authenticated user not following this player
    # When: attempting to unfollow a nonexistent follow
    resp = await auth_client.delete("/me/follows/999999")
    # Then: returns 404
    assert resp.status_code == 404


async def test_search_short_query(auth_client):
    # Given: authenticated user
    # When: searching with a single-character query
    resp = await auth_client.get("/players/search?q=S")
    # Then: returns empty list without calling the MLB API
    assert resp.status_code == 200
    assert resp.json() == {"players": []}


async def test_search_returns_players(auth_client, mocker):
    # Given: MLB API returns one active MLB player
    mock_api = AsyncMock()
    mock_api.search_player.return_value = [
        {"id": 660271, "fullName": "Shohei Ohtani", "active": True}
    ]
    mock_api.get_person.return_value = {
        "currentTeam": {"name": "Los Angeles Dodgers"},
        "primaryPosition": {"abbreviation": "DH"},
    }
    mock_mlb = mocker.patch("atbatwatch.http_api.MlbApi")
    mock_mlb.return_value.__aenter__ = AsyncMock(return_value=mock_api)
    mock_mlb.return_value.__aexit__ = AsyncMock(return_value=False)
    # When: searching by name
    resp = await auth_client.get("/players/search?q=Shohei")
    # Then: player is returned with correct fields
    assert resp.status_code == 200
    players = resp.json()["players"]
    assert len(players) == 1
    assert players[0]["player_id"] == 660271
    assert players[0]["full_name"] == "Shohei Ohtani"
    assert players[0]["team"] == "Los Angeles Dodgers"
    assert players[0]["position"] == "DH"


async def test_search_filters_minor_league(auth_client, mocker):
    # Given: MLB API returns a player on a minor-league affiliate
    mock_api = AsyncMock()
    mock_api.search_player.return_value = [
        {"id": 123456, "fullName": "Minor League Guy", "active": True}
    ]
    mock_api.get_person.return_value = {
        "currentTeam": {"name": "Some AAA Team", "parentOrgId": 999},
    }
    mock_mlb = mocker.patch("atbatwatch.http_api.MlbApi")
    mock_mlb.return_value.__aenter__ = AsyncMock(return_value=mock_api)
    mock_mlb.return_value.__aexit__ = AsyncMock(return_value=False)
    # When: searching by name
    resp = await auth_client.get("/players/search?q=Minor")
    # Then: minor-league player is filtered out
    assert resp.status_code == 200
    assert resp.json()["players"] == []
