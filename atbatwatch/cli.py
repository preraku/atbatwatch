import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import cast

import click
import questionary

from atbatwatch.api import MlbApi, load_fixture
from atbatwatch.config import PlayerConfig, load_config
from atbatwatch.games import extract_offense_state, get_todays_games
from atbatwatch.notifier import ConsoleNotifier
from atbatwatch.players import AmbiguousPlayerError, resolve_all, resolve_player
from atbatwatch.types import LiveFeedResponse, ScheduleResponse
from atbatwatch.watcher import run, run_fixture


@click.group()
def main():
    pass


# ---------------------------------------------------------------------------
# Fixture capture
# ---------------------------------------------------------------------------


@main.command()
@click.argument("game_pk", type=int)
def capture(game_pk: int):
    """Fetch and save a live game feed to fixtures/live_feed/."""
    asyncio.run(_capture(game_pk))


async def _capture(game_pk: int) -> None:
    async with MlbApi() as api:
        data = await api.get_live_feed(game_pk)
    detailed = data.get("gameData", {}).get("status", {}).get("detailedState", "")
    label = {
        "Final": "final",
        "Warmup": "warmup",
        "Pre-Game": "pre_game",
        "In Progress": "in_progress",
    }.get(detailed, "live")
    out = Path("fixtures") / "live_feed" / f"{label}_game_{game_pk}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(data, indent=2))
    click.echo(f"Saved to {out}")


@main.command("capture-schedule")
def capture_schedule():
    """Fetch and save today's schedule to fixtures/schedule/."""
    asyncio.run(_capture_schedule())


async def _capture_schedule() -> None:
    async with MlbApi() as api:
        data = await api.get_schedule()
    today = date.today().strftime("%Y%m%d")
    out = Path("fixtures") / "schedule" / f"schedule_{today}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(data, indent=2))
    click.echo(f"Saved to {out}")


# ---------------------------------------------------------------------------
# Player lookup
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
def lookup(name: str):
    """Resolve a player name to their MLB player ID."""
    asyncio.run(_lookup(name))


async def _lookup(name: str) -> None:
    try:
        async with MlbApi() as api:
            player = await resolve_player(PlayerConfig(name=name), api)
        click.echo(f"{player.full_name}  (id={player.player_id})")
    except AmbiguousPlayerError as e:
        from wcwidth import wcswidth

        name_width = max(wcswidth(c.full_name) for c in e.candidates)
        pos_width = max(len(c.position) for c in e.candidates)

        def _fmt(c: object) -> str:
            pad = name_width - wcswidth(c.full_name)  # type: ignore[attr-defined]
            return (
                f"{c.full_name}{' ' * pad}  ·  {c.position:<{pos_width}}  ·  {c.team}"  # type: ignore[attr-defined]
            )

        choices = {_fmt(c): c for c in e.candidates}
        selected_label = await questionary.select(
            f"Multiple active players match '{e.name}'. Select one:",
            choices=list(choices.keys()),
        ).ask_async()
        if selected_label is None:
            sys.exit(0)
        c = choices[selected_label]
        click.echo(f"\n{c.full_name}  (id={c.player_id})")
        click.echo(f"Add to config.toml:  player_id = {c.player_id}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Games / state inspection
# ---------------------------------------------------------------------------


@main.command()
@click.option("--fixture", type=click.Path(exists=True), default=None)
def games(fixture: str | None):
    """List today's games with status."""
    if fixture:
        data = cast(ScheduleResponse, load_fixture(Path(fixture)))
        for date_entry in data.get("dates", []):
            for game in date_entry.get("games", []):
                away = game["teams"]["away"]["team"]["name"]
                home = game["teams"]["home"]["team"]["name"]
                status = game["status"]["abstractGameState"]
                pk = game["gamePk"]
                click.echo(f"{pk}  {away} @ {home}  [{status}]")
    else:
        asyncio.run(_games())


async def _games() -> None:
    async with MlbApi() as api:
        game_list = await get_todays_games(api)
    for g in game_list:
        click.echo(
            f"{g.game_pk}  {g.away_team_name} @ {g.home_team_name}  [{g.status}]"
        )


@main.command()
@click.argument("game_pk", type=int)
@click.option("--fixture", type=click.Path(exists=True), default=None)
def state(game_pk: int, fixture: str | None):
    """Show the current offense state (batter, on-deck, in-hole) for a game."""
    if fixture:
        data = cast(LiveFeedResponse, load_fixture(Path(fixture)))
    else:
        data = asyncio.run(_get_live_feed(game_pk))
    offense = extract_offense_state(data)
    if not offense:
        click.echo(
            "No offense data (team may be on defense, between innings, or game not live)."
        )
        return
    for key, label in [
        ("batter", "AT BAT "),
        ("onDeck", "ON DECK"),
        ("inHole", "IN HOLE"),
    ]:
        player = offense.get(key)  # type: ignore[typeddict-unknown-key]
        if player:
            click.echo(
                f"{label}: {player.get('fullName', '?')}  (id={player.get('id', '?')})"  # type: ignore[union-attr]
            )


async def _get_live_feed(game_pk: int) -> LiveFeedResponse:
    async with MlbApi() as api:
        return await api.get_live_feed(game_pk)


# ---------------------------------------------------------------------------
# Watch
# ---------------------------------------------------------------------------


@main.command()
@click.option("--fixture", type=click.Path(exists=True), default=None)
def watch(fixture: str | None):
    """Watch for at-bat notifications. Ctrl+C to stop."""
    asyncio.run(_watch(fixture))


async def _watch(fixture: str | None) -> None:
    from atbatwatch import settings
    from atbatwatch.db import make_engine, make_session_factory
    from atbatwatch.redis_client import make_redis

    config = load_config()
    redis = make_redis(settings.REDIS_URL)

    if fixture:
        data = cast(LiveFeedResponse, load_fixture(Path(fixture)))
        async with MlbApi() as api:
            resolved = await resolve_all(config.players, api)
        await run_fixture(data, resolved, ConsoleNotifier(), redis)
        await redis.aclose()
        return

    engine = make_engine(settings.DATABASE_URL)
    session_factory = make_session_factory(engine)

    try:
        async with MlbApi() as api:
            resolved = await resolve_all(config.players, api)
            await run(config, resolved, api, redis, session_factory)
    finally:
        await redis.aclose()
        await engine.dispose()


# ---------------------------------------------------------------------------
# HTTP API server
# ---------------------------------------------------------------------------


@main.command("run-api")
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
def run_api_cmd(host: str, port: int):
    """Run the HTTP API server (FastAPI + uvicorn)."""
    import uvicorn

    uvicorn.run("atbatwatch.http_api:app", host=host, port=port)


# ---------------------------------------------------------------------------
# Stream-based worker commands (Phase 2)
# ---------------------------------------------------------------------------


@main.command("run-poller")
def run_poller_cmd():
    """Run the polling loop — fetches live game state and emits transition events."""
    asyncio.run(_run_poller())


async def _run_poller() -> None:
    from atbatwatch import settings
    from atbatwatch.poller import run_poller
    from atbatwatch.redis_client import make_redis

    config = load_config()
    redis = make_redis(settings.REDIS_URL)
    try:
        async with MlbApi() as api:
            await run_poller(config, api, redis)
    finally:
        await redis.aclose()


@main.command("run-fanout")
def run_fanout_cmd():
    """Run the fan-out worker — reads transitions and writes per-user delivery jobs."""
    asyncio.run(_run_fanout())


async def _run_fanout() -> None:
    from atbatwatch import settings
    from atbatwatch.db import make_engine, make_session_factory
    from atbatwatch.fanout_worker import run_fanout
    from atbatwatch.redis_client import make_redis

    redis = make_redis(settings.REDIS_URL)
    engine = make_engine(settings.DATABASE_URL)
    session_factory = make_session_factory(engine)
    try:
        await run_fanout(redis, session_factory)
    finally:
        await redis.aclose()
        await engine.dispose()


@main.command("run-delivery")
def run_delivery_cmd():
    """Run the delivery worker — sends Discord notifications with idempotency."""
    asyncio.run(_run_delivery())


async def _run_delivery() -> None:
    from atbatwatch import settings
    from atbatwatch.db import make_engine, make_session_factory
    from atbatwatch.delivery_worker import run_delivery
    from atbatwatch.redis_client import make_redis

    redis = make_redis(settings.REDIS_URL)
    engine = make_engine(settings.DATABASE_URL)
    session_factory = make_session_factory(engine)
    try:
        await run_delivery(redis, session_factory)
    finally:
        await redis.aclose()
        await engine.dispose()


@main.command("run-all")
def run_all_cmd():
    """Run poller + fanout + delivery as concurrent asyncio tasks (local dev)."""
    asyncio.run(_run_all())


async def _run_all() -> None:
    import asyncio

    from atbatwatch import settings
    from atbatwatch.db import make_engine, make_session_factory
    from atbatwatch.delivery_worker import run_delivery
    from atbatwatch.fanout_worker import run_fanout
    from atbatwatch.poller import run_poller
    from atbatwatch.redis_client import make_redis

    config = load_config()
    redis = make_redis(settings.REDIS_URL)
    engine = make_engine(settings.DATABASE_URL)
    session_factory = make_session_factory(engine)
    try:
        async with MlbApi() as api:
            await asyncio.gather(
                run_poller(config, api, redis),
                run_fanout(redis, session_factory),
                run_delivery(redis, session_factory),
            )
    finally:
        await redis.aclose()
        await engine.dispose()


# ---------------------------------------------------------------------------
# User management (DB commands)
# ---------------------------------------------------------------------------


@main.group()
def user():
    """Manage users in the database."""
    pass


@user.command("create")
@click.option("--email", required=True, help="User email address")
@click.option(
    "--discord-webhook", required=True, help="Discord webhook URL for notifications"
)
def user_create(email: str, discord_webhook: str) -> None:
    """Create a new user with a Discord notification webhook."""
    asyncio.run(_user_create(email, discord_webhook))


async def _user_create(email: str, discord_webhook: str) -> None:
    from atbatwatch import settings
    from atbatwatch.db import make_engine, make_session_factory
    from atbatwatch.repos.follows import create_user

    engine = make_engine(settings.DATABASE_URL)
    session_factory = make_session_factory(engine)
    try:
        async with session_factory() as session:
            u = await create_user(email, discord_webhook, session)
        click.echo(f"Created user {u.user_id}: {u.email}")
    finally:
        await engine.dispose()


@main.command()
@click.option("--email", required=True, help="User email to add the follow for")
@click.option("--player", "player_name", required=True, help="Player name to follow")
def follow(email: str, player_name: str) -> None:
    """Follow a player — resolves the name and stores the follow in the DB."""
    asyncio.run(_follow(email, player_name))


async def _follow(email: str, player_name: str) -> None:
    from atbatwatch import settings
    from atbatwatch.db import make_engine, make_session_factory
    from atbatwatch.repos.follows import (
        add_follow,
        get_user_by_email,
        upsert_player,
    )

    engine = make_engine(settings.DATABASE_URL)
    session_factory = make_session_factory(engine)
    try:
        async with MlbApi() as api:
            player = await resolve_player(PlayerConfig(name=player_name), api)

        async with session_factory() as session:
            u = await get_user_by_email(email, session)
            if u is None:
                click.echo(
                    f"No user found for {email}. Run `atbatwatch user create` first.",
                    err=True,
                )
                sys.exit(1)
            await upsert_player(player.player_id, player.full_name, session)
            await add_follow(u.user_id, player.player_id, session)
        click.echo(
            f"Now following {player.full_name} (id={player.player_id}) for {email}"
        )
    except (AmbiguousPlayerError, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        await engine.dispose()


@main.command()
@click.option(
    "--user", "email", required=True, help="User email to remove the follow for"
)
@click.option("--player", "player_name", required=True, help="Player name to unfollow")
def unfollow(email: str, player_name: str) -> None:
    """Unfollow a player — resolves the name and removes the follow from the DB."""
    asyncio.run(_unfollow(email, player_name))


async def _unfollow(email: str, player_name: str) -> None:
    from atbatwatch import settings
    from atbatwatch.db import make_engine, make_session_factory
    from atbatwatch.repos.follows import get_user_by_email, remove_follow

    engine = make_engine(settings.DATABASE_URL)
    session_factory = make_session_factory(engine)
    try:
        async with MlbApi() as api:
            player = await resolve_player(PlayerConfig(name=player_name), api)

        async with session_factory() as session:
            u = await get_user_by_email(email, session)
            if u is None:
                click.echo(
                    f"No user found for {email}. Run `atbatwatch user create` first.",
                    err=True,
                )
                sys.exit(1)
            removed = await remove_follow(u.user_id, player.player_id, session)
        if removed:
            click.echo(
                f"Unfollowed {player.full_name} (id={player.player_id}) for {email}"
            )
        else:
            click.echo(
                f"{email} was not following {player.full_name} (id={player.player_id})",
                err=True,
            )
            sys.exit(1)
    except (AmbiguousPlayerError, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        await engine.dispose()
