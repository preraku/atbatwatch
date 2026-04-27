import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from atbatwatch import settings
from atbatwatch.api import MlbApi
from atbatwatch.db import make_engine, make_session_factory
from atbatwatch.repos.follows import (
    add_follow,
    create_user,
    get_follows_for_user,
    get_user_by_email,
    remove_follow,
    upsert_player,
)

app = FastAPI(title="atbatwatch API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_pwd = CryptContext(schemes=["argon2"], deprecated="auto")
_bearer = HTTPBearer()
_ALGORITHM = "HS256"
_TOKEN_EXPIRE_DAYS = 30

_engine = make_engine(settings.DATABASE_URL)
_sessions = make_session_factory(_engine)


async def _db() -> AsyncSession:  # type: ignore[return]
    async with _sessions() as session:
        yield session


def _make_token(user_id: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(days=_TOKEN_EXPIRE_DAYS)
    return jwt.encode(
        {"sub": str(user_id), "exp": exp}, settings.JWT_SECRET, algorithm=_ALGORITHM
    )


async def _current_user_id(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> int:
    try:
        payload = jwt.decode(
            creds.credentials, settings.JWT_SECRET, algorithms=[_ALGORITHM]
        )
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class SignupRequest(BaseModel):
    email: str
    password: str
    discord_webhook: str


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/auth/signup", status_code=201)
async def signup(req: SignupRequest, db: AsyncSession = Depends(_db)):
    existing = await get_user_by_email(req.email, db)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Email already registered")
    password_hash = _pwd.hash(req.password)
    user = await create_user(
        req.email, req.discord_webhook, db, password_hash=password_hash
    )
    return {"token": _make_token(user.user_id)}


@app.post("/auth/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(_db)):
    user = await get_user_by_email(req.email, db)
    if user is None or not _pwd.verify(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"token": _make_token(user.user_id)}


# ---------------------------------------------------------------------------
# Follows
# ---------------------------------------------------------------------------


class FollowRequest(BaseModel):
    player_id: int
    full_name: str
    team: str | None = None
    position: str | None = None


@app.get("/me/follows")
async def list_follows(
    user_id: int = Depends(_current_user_id),
    db: AsyncSession = Depends(_db),
):
    players = await get_follows_for_user(user_id, db)
    return {
        "follows": [
            {
                "player_id": p.player_id,
                "full_name": p.full_name,
                "team": p.team,
                "position": p.position,
            }
            for p in players
        ]
    }


@app.post("/me/follows", status_code=201)
async def follow_player(
    req: FollowRequest,
    user_id: int = Depends(_current_user_id),
    db: AsyncSession = Depends(_db),
):
    await upsert_player(
        req.player_id, req.full_name, db, team=req.team, position=req.position
    )
    await add_follow(user_id, req.player_id, db)
    return {
        "player_id": req.player_id,
        "full_name": req.full_name,
        "team": req.team,
        "position": req.position,
    }


@app.delete("/me/follows/{player_id}", status_code=204)
async def unfollow_player(
    player_id: int,
    user_id: int = Depends(_current_user_id),
    db: AsyncSession = Depends(_db),
):
    removed = await remove_follow(user_id, player_id, db)
    if not removed:
        raise HTTPException(status_code=404, detail="Follow not found")


# ---------------------------------------------------------------------------
# Player search (proxies MLB API)
# ---------------------------------------------------------------------------


@app.get("/players/search")
async def search_players(q: str, _: int = Depends(_current_user_id)):
    if len(q) < 2:
        return {"players": []}
    async with MlbApi() as api:
        results = await api.search_player(q)
        active = [r for r in results if r.get("active", True)]
        if not active:
            return {"players": []}
        details = await asyncio.gather(
            *[api.get_person(r["id"], hydrate="currentTeam") for r in active]
        )
    players = []
    for r, detail in zip(active, details):
        team = detail.get("currentTeam", {})
        if "parentOrgId" in team:
            continue  # minor/winter league — skip
        players.append(
            {
                "player_id": r["id"],
                "full_name": r["fullName"],
                "team": team.get("name"),
                "position": detail.get("primaryPosition", {}).get("abbreviation"),
            }
        )
    return {"players": players}
