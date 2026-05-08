"""scheduler state on system_state singleton

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-08

Tracks the daily theme-runner scheduler. Adds:
  scheduler_enabled       — runtime gate (orthogonal to AUTOTRADE_ENABLED)
  scheduler_cron          — cron expression in ET (default: 0 9 * * 1-5)
  scheduler_next_run_at   — informational; computed by the scheduler at startup
  scheduler_last_run_at   — informational; updated when the daily job fires
  scheduler_last_status   — "ok" | "partial" | "error" | None
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite + Postgres both support add_column; some columns get default values
    # so existing rows get a sane scheduler config without manual fixup.
    with op.batch_alter_table("system_state", schema=None) as bop:
        bop.add_column(sa.Column("scheduler_enabled", sa.Boolean(),
                                 nullable=False, server_default=sa.text("false")))
        bop.add_column(sa.Column("scheduler_cron", sa.String(64),
                                 nullable=False, server_default="0 9 * * 1-5"))
        bop.add_column(sa.Column("scheduler_next_run_at", sa.DateTime(timezone=True), nullable=True))
        bop.add_column(sa.Column("scheduler_last_run_at", sa.DateTime(timezone=True), nullable=True))
        bop.add_column(sa.Column("scheduler_last_status", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("system_state", schema=None) as bop:
        bop.drop_column("scheduler_last_status")
        bop.drop_column("scheduler_last_run_at")
        bop.drop_column("scheduler_next_run_at")
        bop.drop_column("scheduler_cron")
        bop.drop_column("scheduler_enabled")
