"""Hedge Fund Signal Tracker tables

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-31

Phase 1 of the named-manager EDGAR tracker: managers (logical entities),
their CIKs (many-to-one), ingested filings, 13F holdings, quarter-over-
quarter position changes, and a CUSIP→ticker resolution cache.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hedge_fund_managers",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("macro_only", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("primary_themes", sa.JSON, nullable=True),
        sa.Column("weighting_profile", sa.JSON, nullable=True),
        sa.Column("last_filing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("slug", name="uq_hf_manager_slug"),
    )
    op.create_index("ix_hf_manager_active", "hedge_fund_managers", ["active"])

    op.create_table(
        "manager_ciks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("manager_id", sa.Integer, sa.ForeignKey("hedge_fund_managers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cik", sa.String(10), nullable=False),
        sa.Column("entity_name", sa.String(200), nullable=True),
        sa.UniqueConstraint("cik", name="uq_manager_cik"),
    )
    op.create_index("ix_manager_cik_manager", "manager_ciks", ["manager_id"])

    op.create_table(
        "filings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("manager_id", sa.Integer, sa.ForeignKey("hedge_fund_managers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cik", sa.String(10), nullable=False),
        sa.Column("form_type", sa.String(16), nullable=False),
        sa.Column("accession_no", sa.String(32), nullable=False),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_url", sa.Text, nullable=True),
        sa.Column("parsed_json", sa.JSON, nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("accession_no", name="uq_filing_accession"),
    )
    op.create_index("ix_filing_manager_form", "filings", ["manager_id", "form_type"])
    op.create_index("ix_filing_filed_at", "filings", ["filed_at"])

    op.create_table(
        "fund_holdings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("manager_id", sa.Integer, sa.ForeignKey("hedge_fund_managers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filing_id", sa.Integer, sa.ForeignKey("filings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cusip", sa.String(9), nullable=False),
        sa.Column("issuer_name", sa.String(200), nullable=False),
        sa.Column("ticker", sa.String(12), nullable=True),
        sa.Column("value_usd", sa.Float, nullable=False),
        sa.Column("shares", sa.Float, nullable=False),
        sa.Column("put_call_flag", sa.String(1), nullable=False, server_default=""),
        sa.Column("pct_of_portfolio", sa.Float, nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("filing_id", "cusip", "put_call_flag", name="uq_holding_filing_cusip_pc"),
    )
    op.create_index("ix_holding_manager_period", "fund_holdings", ["manager_id", "period_end"])
    op.create_index("ix_holding_ticker", "fund_holdings", ["ticker"])
    op.create_index("ix_holding_cusip", "fund_holdings", ["cusip"])

    op.create_table(
        "position_changes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("manager_id", sa.Integer, sa.ForeignKey("hedge_fund_managers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cusip", sa.String(9), nullable=False),
        sa.Column("ticker", sa.String(12), nullable=True),
        sa.Column("issuer_name", sa.String(200), nullable=True),
        sa.Column("prior_shares", sa.Float, nullable=False, server_default="0"),
        sa.Column("current_shares", sa.Float, nullable=False, server_default="0"),
        sa.Column("change_pct", sa.Float, nullable=True),
        sa.Column("change_type", sa.String(8), nullable=False),
        sa.Column("prior_period", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period", sa.DateTime(timezone=True), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("manager_id", "cusip", "current_period", name="uq_poschg_mgr_cusip_period"),
    )
    op.create_index("ix_poschg_manager", "position_changes", ["manager_id"])
    op.create_index("ix_poschg_ticker", "position_changes", ["ticker"])

    op.create_table(
        "cusip_ticker_map",
        sa.Column("cusip", sa.String(9), primary_key=True),
        sa.Column("ticker", sa.String(12), nullable=True),
        sa.Column("issuer_name", sa.String(200), nullable=True),
        sa.Column("resolved_via", sa.String(32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("cusip_ticker_map")
    op.drop_index("ix_poschg_ticker", table_name="position_changes")
    op.drop_index("ix_poschg_manager", table_name="position_changes")
    op.drop_table("position_changes")
    op.drop_index("ix_holding_cusip", table_name="fund_holdings")
    op.drop_index("ix_holding_ticker", table_name="fund_holdings")
    op.drop_index("ix_holding_manager_period", table_name="fund_holdings")
    op.drop_table("fund_holdings")
    op.drop_index("ix_filing_filed_at", table_name="filings")
    op.drop_index("ix_filing_manager_form", table_name="filings")
    op.drop_table("filings")
    op.drop_index("ix_manager_cik_manager", table_name="manager_ciks")
    op.drop_table("manager_ciks")
    op.drop_index("ix_hf_manager_active", table_name="hedge_fund_managers")
    op.drop_table("hedge_fund_managers")
