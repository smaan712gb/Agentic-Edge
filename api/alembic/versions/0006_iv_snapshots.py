"""IV snapshot table for percentile + IV/realized signal

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-09

Daily snapshot of front-month ATM call implied volatility per symbol,
captured by the maintenance scheduler. Drives the 8th momentum-
exhaustion signal — "short-term call IV at extreme percentile" —
and a complementary IV-vs-realized premium signal that doesn't need
historical data.

Schema is intentionally minimal: one row per (symbol, date). The
captured IV is the front-month (~30 DTE) ATM call's mid-of-bid-ask
implied vol.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "iv_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("atm_call_iv", sa.Float, nullable=False),
        sa.Column("dte_used", sa.Integer, nullable=True),
        sa.Column("strike_used", sa.Float, nullable=True),
        sa.Column("spot_at_capture", sa.Float, nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("symbol", "date", name="uq_iv_snapshots_symbol_date"),
    )
    op.create_index(
        "ix_iv_snapshots_symbol_date", "iv_snapshots",
        ["symbol", "date"],
    )


def downgrade() -> None:
    op.drop_index("ix_iv_snapshots_symbol_date", table_name="iv_snapshots")
    op.drop_table("iv_snapshots")
