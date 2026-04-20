from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    notification_target_type: Mapped[str] = mapped_column(String(20))
    notification_target_id: Mapped[str] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Player(Base):
    __tablename__ = "players"

    player_id: Mapped[int] = mapped_column(primary_key=True)  # = MLB player ID
    full_name: Mapped[str] = mapped_column(String(255))


class Follow(Base):
    __tablename__ = "follows"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.player_id"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (Index("ix_follows_player_id", "player_id"),)


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"))
    state: Mapped[str] = mapped_column(String(20))
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(20))
    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_notif_log_event_user"),
    )


def make_engine(database_url: str):
    return create_async_engine(database_url, echo=False)


def make_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
