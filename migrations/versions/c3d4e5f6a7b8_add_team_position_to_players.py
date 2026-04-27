"""add team and position to players

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-26

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("players", sa.Column("team", sa.String(100), nullable=True))
    op.add_column("players", sa.Column("position", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("players", "position")
    op.drop_column("players", "team")
