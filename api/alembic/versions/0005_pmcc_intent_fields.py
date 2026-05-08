"""PMCC fields on trade_intents

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-08

Adds the columns the PMCC strategy + executor need to persist a multi-leg
intent through its lifecycle:

  structure          — "stock" | "pmcc" | "pmcc_sequenced"
  position_state     — pending | leap_pending | leap_open_naked | short_pending | pmcc_full | closing | closed | abandoned
  leap_*             — long leg (deep ITM call)
  short_call_*       — short leg (OTM weekly/monthly call)
  net_debit_*        — capital actually deployed
  walking_config     — execution policy snapshot at submission
  trigger_*          — for sequenced legging (Phase B-2 follow-up)

All columns nullable so the existing stock-style intents in the table
keep working unchanged.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("trade_intents", schema=None) as bop:
        bop.add_column(sa.Column("structure",       sa.String(24), nullable=False, server_default="stock"))
        bop.add_column(sa.Column("position_state",  sa.String(24), nullable=False, server_default="pending"))

        # Long leg (LEAP)
        bop.add_column(sa.Column("leap_expiry",         sa.String(10), nullable=True))   # YYYY-MM-DD
        bop.add_column(sa.Column("leap_strike",         sa.Float(),    nullable=True))
        bop.add_column(sa.Column("leap_delta_target",   sa.Float(),    nullable=True))
        bop.add_column(sa.Column("leap_delta_actual",   sa.Float(),    nullable=True))
        bop.add_column(sa.Column("leap_iv",             sa.Float(),    nullable=True))
        bop.add_column(sa.Column("leap_open_interest",  sa.Integer(),  nullable=True))
        bop.add_column(sa.Column("leap_qty",            sa.Integer(),  nullable=True))
        bop.add_column(sa.Column("leap_filled_at",      sa.DateTime(timezone=True), nullable=True))
        bop.add_column(sa.Column("leap_fill_price",     sa.Float(),    nullable=True))

        # Short leg (covered call)
        bop.add_column(sa.Column("short_call_expiry",        sa.String(10), nullable=True))
        bop.add_column(sa.Column("short_call_strike",        sa.Float(),    nullable=True))
        bop.add_column(sa.Column("short_call_delta_target",  sa.Float(),    nullable=True))
        bop.add_column(sa.Column("short_call_delta_actual",  sa.Float(),    nullable=True))
        bop.add_column(sa.Column("short_call_iv",            sa.Float(),    nullable=True))
        bop.add_column(sa.Column("short_call_open_interest", sa.Integer(),  nullable=True))
        bop.add_column(sa.Column("short_call_qty",           sa.Integer(),  nullable=True))
        bop.add_column(sa.Column("short_call_filled_at",     sa.DateTime(timezone=True), nullable=True))
        bop.add_column(sa.Column("short_call_fill_price",    sa.Float(),    nullable=True))

        # Combo financials at submission
        bop.add_column(sa.Column("net_debit_target",  sa.Float(),    nullable=True))   # mid at submit
        bop.add_column(sa.Column("net_debit_cap",     sa.Float(),    nullable=True))   # walking-limit cap
        bop.add_column(sa.Column("net_debit_filled",  sa.Float(),    nullable=True))   # actual
        bop.add_column(sa.Column("max_loss",          sa.Float(),    nullable=True))   # = net_debit_filled
        bop.add_column(sa.Column("walking_config",    sa.JSON(),     nullable=True))

        # Sequenced-legging fields (Phase B-2)
        bop.add_column(sa.Column("entry_strategy",     sa.String(24), nullable=True))  # combo | sequence_long_first
        bop.add_column(sa.Column("support_reference",  sa.Float(),    nullable=True))
        bop.add_column(sa.Column("trigger_conditions", sa.JSON(),     nullable=True))
        bop.add_column(sa.Column("trigger_status",     sa.JSON(),     nullable=True))

        # IBKR linkage for the combo
        bop.add_column(sa.Column("ibkr_combo_conid",  sa.String(64), nullable=True))


def downgrade() -> None:
    cols = [
        "ibkr_combo_conid", "trigger_status", "trigger_conditions", "support_reference",
        "entry_strategy", "walking_config", "max_loss", "net_debit_filled",
        "net_debit_cap", "net_debit_target",
        "short_call_fill_price", "short_call_filled_at", "short_call_qty",
        "short_call_open_interest", "short_call_iv", "short_call_delta_actual",
        "short_call_delta_target", "short_call_strike", "short_call_expiry",
        "leap_fill_price", "leap_filled_at", "leap_qty", "leap_open_interest",
        "leap_iv", "leap_delta_actual", "leap_delta_target", "leap_strike", "leap_expiry",
        "position_state", "structure",
    ]
    with op.batch_alter_table("trade_intents", schema=None) as bop:
        for c in cols:
            bop.drop_column(c)
