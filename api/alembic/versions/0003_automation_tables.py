"""automation: system_state singleton + auto_actions audit table

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-07

The kill switch (``system_state.autotrade_enabled``) is the runtime
half of the dual gate; the env-level ``AUTOTRADE_ENABLED`` is the
deploy-time half. Either being false disables every automation loop.

``auto_actions`` captures every decision the automation makes — whether
or not it produced an IBKR order. ``trade_audit_log`` (already
existing) captures only the broker-call layer. The two tables answer
different questions: auto_actions = "what did the system decide and
why", trade_audit_log = "what did we send to the broker, what came back".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_state",
        sa.Column("id", sa.Integer(), primary_key=True),  # singleton, always 1
        sa.Column("autotrade_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_kill_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kill_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_system_state_singleton"),
    )

    op.create_table(
        "auto_actions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("loop", sa.String(32), nullable=False),  # entry | maintenance | reconcile
        sa.Column("action_type", sa.String(48), nullable=False),
        sa.Column("symbol", sa.String(10), nullable=True),
        sa.Column("intent_id", sa.String(64),
                  sa.ForeignKey("trade_intents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("gate_status", sa.String(16), nullable=False),  # passed | rejected | error
        sa.Column("gate_failures", sa.JSON(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("outcome", sa.String(48), nullable=True),
        sa.Column("ibkr_order_id", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_auto_actions_timestamp", "auto_actions", ["timestamp"])
    op.create_index("ix_auto_actions_loop_status", "auto_actions", ["loop", "gate_status"])
    op.create_index("ix_auto_actions_symbol", "auto_actions", ["symbol"])

    # Seed the singleton row, autotrade off by default.
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    bind.execute(sa.text(
        "INSERT INTO system_state (id, autotrade_enabled, updated_at, updated_by) "
        "VALUES (1, :enabled, :now, :who)"
    ), {"enabled": False, "now": now, "who": "migration:0003"})


def downgrade() -> None:
    op.drop_index("ix_auto_actions_symbol", table_name="auto_actions")
    op.drop_index("ix_auto_actions_loop_status", table_name="auto_actions")
    op.drop_index("ix_auto_actions_timestamp", table_name="auto_actions")
    op.drop_table("auto_actions")
    op.drop_table("system_state")
