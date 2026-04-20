from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atbatwatch.db import Follow, Player, User


async def get_followers(player_id: int, session: AsyncSession) -> list[User]:
    result = await session.execute(
        select(User)
        .join(Follow, User.user_id == Follow.user_id)
        .where(Follow.player_id == player_id)
    )
    return list(result.scalars())


async def upsert_player(player_id: int, full_name: str, session: AsyncSession) -> None:
    existing = await session.get(Player, player_id)
    if existing is None:
        session.add(Player(player_id=player_id, full_name=full_name))
    else:
        existing.full_name = full_name
    await session.commit()


async def create_user(email: str, webhook_url: str, session: AsyncSession) -> User:
    user = User(
        email=email,
        notification_target_type="discord",
        notification_target_id=webhook_url,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def add_follow(user_id: int, player_id: int, session: AsyncSession) -> None:
    existing = await session.get(Follow, (user_id, player_id))
    if existing is None:
        session.add(Follow(user_id=user_id, player_id=player_id))
        await session.commit()


async def get_user_by_email(email: str, session: AsyncSession) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def remove_follow(user_id: int, player_id: int, session: AsyncSession) -> bool:
    existing = await session.get(Follow, (user_id, player_id))
    if existing is None:
        return False
    await session.delete(existing)
    await session.commit()
    return True
