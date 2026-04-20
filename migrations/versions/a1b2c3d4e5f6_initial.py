"""initial

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-04-22

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("notification_target_type", sa.String(20), nullable=False),
        sa.Column("notification_target_id", sa.String(1024), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "players",
        sa.Column("player_id", sa.Integer(), primary_key=True),
        sa.Column("full_name", sa.String(255), nullable=False),
    )
    op.create_table(
        "follows",
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.user_id"), primary_key=True
        ),
        sa.Column(
            "player_id",
            sa.Integer(),
            sa.ForeignKey("players.player_id"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_follows_player_id", "follows", ["player_id"])
    op.create_table(
        "notification_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=False
        ),
        sa.Column(
            "player_id",
            sa.Integer(),
            sa.ForeignKey("players.player_id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.UniqueConstraint(
            "event_id", "user_id", name="uq_notification_log_event_user"
        ),
    )


def downgrade() -> None:
    op.drop_table("notification_log")
    op.drop_index("ix_follows_player_id", table_name="follows")
    op.drop_table("follows")
    op.drop_table("players")
    op.drop_table("users")
