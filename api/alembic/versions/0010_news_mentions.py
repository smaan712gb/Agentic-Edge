"""News mentions (Phase 3 — chokepoint news layer)

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-31

Stores chokepoint-relevant, sentiment-tagged news for tracked tickers from
the account's IBKR news providers (free). Deduped by source+article+ticker.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_mentions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source_type", sa.String(24), nullable=False, server_default="ib_news"),
        sa.Column("provider", sa.String(24), nullable=False, server_default=""),
        sa.Column("article_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("ticker", sa.String(12), nullable=False),
        sa.Column("theme_id", sa.String(64), sa.ForeignKey("themes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("headline", sa.Text, nullable=False, server_default=""),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("chokepoint_hits", sa.JSON, nullable=True),
        sa.Column("sentiment", sa.String(8), nullable=True),
        sa.Column("conviction", sa.Float, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_type", "provider", "article_id", "ticker",
                            name="uq_mention_source_article_ticker"),
    )
    op.create_index("ix_mention_ticker_captured", "news_mentions", ["ticker", "captured_at"])
    op.create_index("ix_mention_captured", "news_mentions", ["captured_at"])


def downgrade() -> None:
    op.drop_index("ix_mention_captured", table_name="news_mentions")
    op.drop_index("ix_mention_ticker_captured", table_name="news_mentions")
    op.drop_table("news_mentions")
