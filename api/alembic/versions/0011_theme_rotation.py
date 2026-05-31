"""Theme rotation detector state

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-31

Per-theme rotation state (one row/theme) from leading institutional-rotation
signals. Read by the entry loop (halt new entries) and maintenance loop
(take profit on winners + tighten exit sensitivity).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "theme_rotation",
        sa.Column("theme_id", sa.String(64), sa.ForeignKey("themes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("flagged", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("score", sa.Float, nullable=False, server_default="0"),
        sa.Column("signals_tripped", sa.JSON, nullable=True),
        sa.Column("evidence", sa.JSON, nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("theme_rotation")
