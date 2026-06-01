"""Manager tier (tier1 / tier2 / activist)

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-01

Tiers the tracked managers: tier1 = primary conviction, tier2 = cross-
confirmation (half weight), activist = event watchlist (excluded from
sizing; 13D filings fire Tier-1 instant alerts).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("hedge_fund_managers",
                  sa.Column("tier", sa.String(12), nullable=False, server_default="tier1"))


def downgrade() -> None:
    op.drop_column("hedge_fund_managers", "tier")
