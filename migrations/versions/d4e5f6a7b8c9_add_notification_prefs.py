"""add notification prefs to follows, game_id to notification_log

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "follows",
        sa.Column("notify_at_bat", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column(
        "follows",
        sa.Column("notify_on_deck", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column(
        "notification_log",
        sa.Column("game_id", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_log", "game_id")
    op.drop_column("follows", "notify_on_deck")
    op.drop_column("follows", "notify_at_bat")
