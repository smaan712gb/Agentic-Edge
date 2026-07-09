"""Stake filings (subject-company 13D/13G watch)

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-04

Near-real-time institutional-stake layer keyed by SUBJECT company (our
holdings + theme universe), not by tracked manager. Stores Schedule 13D/13G
and amendments filed ABOUT a watched name; the filer is recovered from the
accession's leading CIK so one filer's stake can be tracked across amendments
to detect an abrupt REDUCTION/EXIT (the 13G/A early-exit signal).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stake_filings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subject_ticker", sa.String(12), nullable=True),
        sa.Column("subject_cik", sa.String(10), nullable=False),
        sa.Column("filer_cik", sa.String(10), nullable=False),
        sa.Column("form_type", sa.String(24), nullable=False),
        sa.Column("accession_no", sa.String(32), nullable=False),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("percent_of_class", sa.Float(), nullable=True),
        sa.Column("prior_percent", sa.Float(), nullable=True),
        sa.Column("change_type", sa.String(10), nullable=False, server_default="initial"),
        sa.Column("is_activist", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_holding", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_url", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("accession_no", name="uq_stake_accession"),
    )
    op.create_index("ix_stake_subject_ticker", "stake_filings", ["subject_ticker"])
    op.create_index("ix_stake_subject_filer", "stake_filings", ["subject_cik", "filer_cik"])
    op.create_index("ix_stake_filed_at", "stake_filings", ["filed_at"])


def downgrade() -> None:
    op.drop_index("ix_stake_filed_at", table_name="stake_filings")
    op.drop_index("ix_stake_subject_filer", table_name="stake_filings")
    op.drop_index("ix_stake_subject_ticker", table_name="stake_filings")
    op.drop_table("stake_filings")
