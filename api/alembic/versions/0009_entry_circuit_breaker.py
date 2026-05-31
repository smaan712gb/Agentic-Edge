"""Entry circuit-breaker state on system_state

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-31

Adds the latch + daily NAV reference for the entry circuit breaker. The
breaker halts NEW entries on a severe account-level breach and never closes
positions (high-beta exits stay signal-driven).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("system_state", sa.Column("entry_breaker_tripped", sa.Boolean,
                                            nullable=False, server_default=sa.false()))
    op.add_column("system_state", sa.Column("entry_breaker_reason", sa.Text, nullable=True))
    op.add_column("system_state", sa.Column("entry_breaker_tripped_at",
                                            sa.DateTime(timezone=True), nullable=True))
    op.add_column("system_state", sa.Column("breaker_nav_ref", sa.Float, nullable=True))
    op.add_column("system_state", sa.Column("breaker_nav_ref_date",
                                            sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("system_state", "breaker_nav_ref_date")
    op.drop_column("system_state", "breaker_nav_ref")
    op.drop_column("system_state", "entry_breaker_tripped_at")
    op.drop_column("system_state", "entry_breaker_reason")
    op.drop_column("system_state", "entry_breaker_tripped")
