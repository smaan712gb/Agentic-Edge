"""initial schema — themes, runs, scores, positions, trades

Revision ID: 0001
Revises:
Create Date: 2026-05-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # pgvector — only on Postgres. Safe-IF-NOT-EXISTS so re-running is a no-op.
    if is_postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "themes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("thesis", sa.Text(), nullable=False, server_default=""),
        sa.Column("chokepoint", sa.Text(), nullable=False, server_default=""),
        sa.Column("weights", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "id", name="uq_themes_user_id"),
    )
    op.create_index("ix_themes_user_id", "themes", ["user_id"])

    op.create_table(
        "theme_symbols",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("theme_id", sa.String(64),
                  sa.ForeignKey("themes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("theme_id", "symbol", name="uq_theme_symbol"),
    )
    op.create_index("ix_theme_symbols_symbol", "theme_symbols", ["symbol"])

    op.create_table(
        "runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("theme_id", sa.String(64),
                  sa.ForeignKey("themes.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("best_positioned", sa.JSON(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
    )
    op.create_index("ix_runs_user_id", "runs", ["user_id"])
    op.create_index("ix_runs_theme_id_started_at", "runs", ["theme_id", "started_at"])
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index("ix_runs_idempotency_key", "runs", ["idempotency_key"])

    op.create_table(
        "run_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64),
                  sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.String(48), nullable=False),
        sa.Column("symbol", sa.String(10), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_events_run_id_id", "run_events", ["run_id", "id"])

    # pgvector embedding column on run_events for cross-ticker analogy lookup.
    if is_postgres:
        op.execute(
            "ALTER TABLE run_events ADD COLUMN embedding vector(1536) NULL"
        )

    op.create_table(
        "ticker_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64),
                  sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("setup", sa.Float(), nullable=False),
        sa.Column("options", sa.Float(), nullable=False),
        sa.Column("thesis_fit", sa.Float(), nullable=False),
        sa.Column("composite", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("conviction", sa.Integer(), nullable=True),
        sa.Column("drivers", sa.JSON(), nullable=False),
        sa.Column("risks", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("agent_reports", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "symbol", name="uq_ticker_scores_run_symbol"),
    )
    op.create_index("ix_ticker_scores_symbol", "ticker_scores", ["symbol"])

    if is_postgres:
        op.execute(
            "ALTER TABLE ticker_scores ADD COLUMN embedding vector(1536) NULL"
        )

    op.create_table(
        "theme_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64),
                  sa.ForeignKey("runs.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("theme_id", sa.String(64),
                  sa.ForeignKey("themes.id"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("ranking", sa.JSON(), nullable=False),
        sa.Column("best_positioned", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column("avg_price", sa.Float(), nullable=False),
        sa.Column("last_price", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("pnl", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "account_id", "symbol",
                            name="uq_positions_user_account_symbol"),
    )
    op.create_index("ix_positions_user_id", "positions", ["user_id"])

    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=True),
        sa.Column("notional", sa.Float(), nullable=True),
        sa.UniqueConstraint("user_id", "account_id", "date",
                            name="uq_equity_user_account_date"),
    )
    op.create_index("ix_equity_user_id_date", "equity_snapshots", ["user_id", "date"])

    op.create_table(
        "trade_intents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("run_id", sa.String(64),
                  sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column("order_type", sa.String(8), nullable=False, server_default="LMT"),
        sa.Column("limit_px", sa.Float(), nullable=True),
        sa.Column("stop_px", sa.Float(), nullable=True),
        sa.Column("target_px", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("ibkr_order_id", sa.String(64), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trade_intents_user_id_status", "trade_intents", ["user_id", "status"])
    op.create_index("ix_trade_intents_run_id", "trade_intents", ["run_id"])

    op.create_table(
        "trade_audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("intent_id", sa.String(64),
                  sa.ForeignKey("trade_intents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("ibkr_account", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trade_audit_log_user_id_ts", "trade_audit_log", ["user_id", "timestamp"])


def downgrade() -> None:
    for tbl in (
        "trade_audit_log",
        "trade_intents",
        "equity_snapshots",
        "positions",
        "theme_reports",
        "ticker_scores",
        "run_events",
        "runs",
        "theme_symbols",
        "themes",
    ):
        op.drop_table(tbl)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP EXTENSION IF EXISTS vector")
