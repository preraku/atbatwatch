"""HTTP API contract tests (Phase 3).

All interactions are through external surfaces only: HTTP on localhost:8001 and
direct Postgres queries. No atbatwatch imports.
"""

import uuid

import pytest

# ---------------------------------------------------------------------------
# Auth — signup
# ---------------------------------------------------------------------------


async def test_signup_happy_path_returns_token_and_user_exists_in_db(http, db):
    email = f"happy-{uuid.uuid4()}@example.com"
    resp = await http.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "hunter2",
            "discord_webhook": "http://x.invalid/hook",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "token" in body
    assert isinstance(body["token"], str) and len(body["token"]) > 20

    row = await db.fetchrow("SELECT user_id, email FROM users WHERE email = $1", email)
    assert row is not None, "user should exist in DB after signup"
    assert row["email"] == email


async def test_signup_duplicate_email_returns_409(http, db):
    email = f"dup-{uuid.uuid4()}@example.com"
    payload = {
        "email": email,
        "password": "pass",
        "discord_webhook": "http://x.invalid/hook",
    }
    r1 = await http.post("/auth/signup", json=payload)
    assert r1.status_code == 201

    r2 = await http.post("/auth/signup", json=payload)
    assert r2.status_code == 409


async def test_signup_missing_field_returns_422(http):
    # Missing discord_webhook
    resp = await http.post(
        "/auth/signup",
        json={"email": f"missing-{uuid.uuid4()}@example.com", "password": "pass"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Auth — login
# ---------------------------------------------------------------------------


async def test_login_wrong_password_returns_401(http, db):
    email = f"wrongpw-{uuid.uuid4()}@example.com"
    await http.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "correctpass",
            "discord_webhook": "http://x.invalid/h",
        },
    )

    resp = await http.post(
        "/auth/login", json={"email": email, "password": "wrongpass"}
    )
    assert resp.status_code == 401


async def test_login_unknown_email_returns_401(http):
    resp = await http.post(
        "/auth/login",
        json={"email": f"ghost-{uuid.uuid4()}@example.com", "password": "irrelevant"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Auth guard — /me/follows without a valid token
# ---------------------------------------------------------------------------


async def test_me_follows_without_auth_is_rejected(http):
    # No Authorization header at all
    resp = await http.get("/me/follows")
    assert resp.status_code in (401, 403)


async def test_me_follows_with_bad_token_is_rejected(http):
    resp = await http.get(
        "/me/follows", headers={"Authorization": "Bearer notavalidtoken"}
    )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Follows — CRUD
# ---------------------------------------------------------------------------


async def test_me_follows_empty_for_new_user(http, db):
    email = f"newfollows-{uuid.uuid4()}@example.com"
    r = await http.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "pass",
            "discord_webhook": "http://x.invalid/h",
        },
    )
    token = r.json()["token"]

    resp = await http.get("/me/follows", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"follows": []}


async def test_post_follow_then_get_returns_player(http, db):
    email = f"follow-{uuid.uuid4()}@example.com"
    r = await http.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "pass",
            "discord_webhook": "http://x.invalid/h",
        },
    )
    token = r.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    follow_payload = {
        "player_id": 669016,
        "full_name": "Brandon Marsh",
        "team": "Philadelphia Phillies",
        "position": "CF",
    }
    post_resp = await http.post("/me/follows", json=follow_payload, headers=auth)
    assert post_resp.status_code == 201
    created = post_resp.json()
    assert created["player_id"] == 669016
    assert created["full_name"] == "Brandon Marsh"

    get_resp = await http.get("/me/follows", headers=auth)
    assert get_resp.status_code == 200
    follows = get_resp.json()["follows"]
    assert len(follows) == 1
    assert follows[0]["player_id"] == 669016
    assert follows[0]["full_name"] == "Brandon Marsh"


async def test_post_follow_is_idempotent(http, db):
    email = f"idem-{uuid.uuid4()}@example.com"
    r = await http.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "pass",
            "discord_webhook": "http://x.invalid/h",
        },
    )
    token = r.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    payload = {
        "player_id": 669016,
        "full_name": "Brandon Marsh",
        "team": "PHI",
        "position": "CF",
    }

    r1 = await http.post("/me/follows", json=payload, headers=auth)
    assert r1.status_code == 201

    r2 = await http.post("/me/follows", json=payload, headers=auth)
    assert r2.status_code == 201

    get_resp = await http.get("/me/follows", headers=auth)
    assert len(get_resp.json()["follows"]) == 1


async def test_delete_follow_then_second_delete_is_404(http, db):
    email = f"del-{uuid.uuid4()}@example.com"
    r = await http.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "pass",
            "discord_webhook": "http://x.invalid/h",
        },
    )
    token = r.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    await http.post(
        "/me/follows",
        json={"player_id": 669016, "full_name": "Brandon Marsh"},
        headers=auth,
    )

    del1 = await http.delete("/me/follows/669016", headers=auth)
    assert del1.status_code == 204

    del2 = await http.delete("/me/follows/669016", headers=auth)
    assert del2.status_code == 404


# ---------------------------------------------------------------------------
# Player search
# ---------------------------------------------------------------------------


async def test_players_search_requires_auth(http):
    resp = await http.get("/players/search", params={"q": "Marsh"})
    assert resp.status_code in (401, 403)


@pytest.mark.parametrize("q", ["a", "B"])
async def test_players_search_one_char_returns_empty_list(http, db, q):
    email = f"search-{uuid.uuid4()}@example.com"
    r = await http.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "pass",
            "discord_webhook": "http://x.invalid/h",
        },
    )
    token = r.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    resp = await http.get("/players/search", params={"q": q}, headers=auth)
    assert resp.status_code == 200
    assert resp.json() == {"players": []}
