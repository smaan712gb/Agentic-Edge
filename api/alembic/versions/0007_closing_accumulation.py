"""Closing-accumulation signal table

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-11

Persists the daily 15:50 ET closing-accumulation sweep. One row per
(symbol, date) — the operator dashboard can query "today's signals"
in one shot.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "closing_accumulation_signals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(10), nullable=False),
        # `date` is a tz-aware DateTime to match models.ClosingAccumulationSignal.
        # The 15:50 ET sweep records a precise instant, not a calendar day —
        # using sa.Date would silently truncate the time-of-day on Postgres.
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("theme_id", sa.String(64), sa.ForeignKey("themes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("setup_passes", sa.Boolean, nullable=False),
        sa.Column("confidence", sa.String(8), nullable=False),    # high|medium|low|none
        sa.Column("gate1_passes", sa.Boolean, nullable=False),
        sa.Column("gate2_passes", sa.Boolean, nullable=False),
        sa.Column("theme_confirmed", sa.Boolean, nullable=True),
        sa.Column("last_30m_rvol", sa.Float, nullable=True),
        sa.Column("day_rvol", sa.Float, nullable=True),
        sa.Column("pct_session_above_vwap", sa.Float, nullable=True),
        sa.Column("ah_print_count", sa.Integer, nullable=True),
        sa.Column("ah_cumulative_volume", sa.Float, nullable=True),
        sa.Column("ah_holds_close", sa.Boolean, nullable=True),
        sa.Column("moc_price", sa.Float, nullable=True),
        sa.Column("vwap", sa.Float, nullable=True),
        sa.Column("entry_recommendation", sa.Text, nullable=True),
        sa.Column("rationale", sa.Text, nullable=True),
        sa.Column("failure_filters", sa.JSON, nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("symbol", "date", name="uq_cba_symbol_date"),
    )
    op.create_index(
        "ix_cba_date_confidence", "closing_accumulation_signals",
        ["date", "confidence"],
    )


def downgrade() -> None:
    op.drop_index("ix_cba_date_confidence", table_name="closing_accumulation_signals")
    op.drop_table("closing_accumulation_signals")
