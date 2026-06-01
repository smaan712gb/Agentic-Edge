"""Cross-fund overlap — which names multiple tracked managers hold.

Aggregates each manager's *latest* 13F holdings by security (CUSIP) and ranks
by how many managers hold it and the aggregate dollar exposure. Two-or-more
managers on the same name is the cross-fund *confirmation* signal the alert
engine doubles weight on.

Theme filtering matches on ``ticker`` against the theme's symbol set, so it
only catches holdings whose CUSIP has been resolved to a ticker (Phase-1
enrichment is best-effort — see CusipTickerMap). The unfiltered overlap is
always complete because it keys on CUSIP + issuer name.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select

from ..db import FundHolding, HedgeFundManager, ThemeSymbol, get_session as db_session


async def cross_fund_overlap(
    *,
    theme_id: Optional[str] = None,
    min_managers: int = 1,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Ranked overlap rows. Long underlying only (put_call_flag == '')."""
    async with db_session() as s:
        # Latest period per manager.
        max_period = (
            select(FundHolding.manager_id, func.max(FundHolding.period_end).label("mp"))
            .group_by(FundHolding.manager_id)
            .subquery()
        )
        q = (
            select(FundHolding, HedgeFundManager.slug, HedgeFundManager.name)
            .join(max_period, (FundHolding.manager_id == max_period.c.manager_id)
                  & (FundHolding.period_end == max_period.c.mp))
            .join(HedgeFundManager, HedgeFundManager.id == FundHolding.manager_id)
            .where(FundHolding.put_call_flag == "")
            # Activists (Elliott/Pershing) are an event watchlist, not holding
            # conviction — keep them out of the cross-fund overlap.
            .where(HedgeFundManager.tier != "activist")
        )

        theme_tickers: Optional[set[str]] = None
        if theme_id:
            rows = (await s.execute(
                select(ThemeSymbol.symbol).where(ThemeSymbol.theme_id == theme_id)
            )).scalars().all()
            theme_tickers = {(r or "").upper() for r in rows if r}

        results = (await s.execute(q)).all()

    # Aggregate by CUSIP in Python (small N — handful of managers × ~hundreds).
    # Managers are keyed by slug so a manager filing under multiple CIKs counts
    # ONCE (Situational Awareness has two CIKs) — otherwise the cross-fund
    # confirmation count is inflated.
    by_cusip: dict[str, dict[str, Any]] = {}
    for holding, slug, name in results:
        if theme_tickers is not None:
            if not holding.ticker or holding.ticker.upper() not in theme_tickers:
                continue
        row = by_cusip.setdefault(holding.cusip, {
            "cusip": holding.cusip,
            "issuer_name": holding.issuer_name,
            "ticker": holding.ticker,
            "_managers": {},  # slug -> aggregated entry
        })
        m = row["_managers"].setdefault(slug, {
            "slug": slug, "name": name, "value_usd": 0.0, "shares": 0.0,
            "pct_of_portfolio": holding.pct_of_portfolio,
        })
        m["value_usd"] += holding.value_usd
        m["shares"] += holding.shares
        if holding.ticker and not row["ticker"]:
            row["ticker"] = holding.ticker

    rows = []
    for r in by_cusip.values():
        managers = list(r["_managers"].values())
        if len(managers) < min_managers:
            continue
        rows.append({
            "cusip": r["cusip"], "issuer_name": r["issuer_name"], "ticker": r["ticker"],
            "managers": managers,
            "aggregate_value_usd": sum(m["value_usd"] for m in managers),
            "aggregate_shares": sum(m["shares"] for m in managers),
            "manager_count": len(managers),
            "confirmation": len(managers) >= 2,
        })
    rows.sort(key=lambda r: (r["manager_count"], r["aggregate_value_usd"]), reverse=True)
    return rows[:limit]
