"""Symbol feature snapshots (Quant Research Factory feature store)

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-27

Point-in-time quant feature store — one row per (symbol, as_of). Every feature
value is known AS OF that date (no lookahead); forward-return labels are
backfilled later by research.labeler. The keystone of the Quant Research
Factory (api/app/research/quant_factory.md). Research-only: no entry/exit gate
reads this table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "symbol_feature_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("features", sa.JSON, nullable=False),
        sa.Column("labels", sa.JSON, nullable=True),
        sa.Column("label_status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("symbol", "as_of", name="uq_symfeat_symbol_asof"),
    )
    op.create_index("ix_symfeat_as_of", "symbol_feature_snapshots", ["as_of"])
    op.create_index("ix_symfeat_symbol_asof", "symbol_feature_snapshots", ["symbol", "as_of"])
    op.create_index("ix_symfeat_label_status", "symbol_feature_snapshots", ["label_status"])


def downgrade() -> None:
    op.drop_index("ix_symfeat_label_status", table_name="symbol_feature_snapshots")
    op.drop_index("ix_symfeat_symbol_asof", table_name="symbol_feature_snapshots")
    op.drop_index("ix_symfeat_as_of", table_name="symbol_feature_snapshots")
    op.drop_table("symbol_feature_snapshots")
