"""CUSIP <-> ticker enrichment.

13F holdings carry CUSIP + issuer name but never a ticker; the flow overlay
and the smart-money-by-symbol read both key on ticker. We bridge the two by
resolving the CUSIP of each symbol in YOUR theme universe (via FMP, cached a
week — CUSIPs are static), storing it in cusip_ticker_map, then backfilling
ticker onto any holdings whose CUSIP now resolves.

Scoping to theme symbols is deliberate: those are the names you actually watch
and the ones most likely to overlap with manager books. Names no manager-or-
theme cares about stay ticker-less (and simply don't get a flow overlay).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update

from ..db import CusipTickerMap, FundHolding, PositionChange, ThemeSymbol, get_session as db_session
from .repo import HedgeFundRepo

logger = logging.getLogger("agentic_edge.hedge_funds")


def _get_fmp() -> Any | None:
    try:
        from tradingagents.dataflows.providers.registry import get_provider
        return get_provider("fmp")
    except Exception as e:
        logger.info("hedge_funds: FMP unavailable for CUSIP enrichment (%s)", e)
        return None


async def resolve_theme_cusips() -> dict[str, int]:
    """Resolve CUSIP for each distinct theme symbol and upsert the map.

    Cheap on repeat runs — FMP's get_cusip is week-cached. Returns counts."""
    fmp = _get_fmp()
    if fmp is None:
        return {"resolved": 0, "skipped": 0, "reason": 1}

    async with db_session() as s:
        symbols = sorted({(r or "").upper() for r in
                          (await s.execute(select(ThemeSymbol.symbol))).scalars().all() if r})

    resolved = skipped = 0
    for sym in symbols:
        try:
            cusip = await fmp.get_cusip(sym)
        except Exception as e:
            logger.debug("cusip resolve failed for %s: %s", sym, e)
            cusip = None
        if not cusip:
            skipped += 1
            continue
        async with db_session() as s:
            await HedgeFundRepo(s).upsert_cusip_ticker(
                cusip=cusip, ticker=sym, resolved_via="fmp",
            )
        resolved += 1
    logger.info("hedge_funds: theme-CUSIP enrichment — %d resolved, %d unmapped", resolved, skipped)
    return {"resolved": resolved, "skipped": skipped}


async def backfill_holding_tickers() -> int:
    """Fill ticker on holdings / position_changes whose CUSIP is now mapped.

    Idempotent — only touches rows where ticker is still NULL."""
    async with db_session() as s:
        mapping = {
            row.cusip: row.ticker
            for row in (await s.execute(
                select(CusipTickerMap).where(CusipTickerMap.ticker.is_not(None))
            )).scalars().all()
        }
    if not mapping:
        return 0

    updated = 0
    async with db_session() as s:
        for cusip, ticker in mapping.items():
            res = await s.execute(
                update(FundHolding)
                .where(FundHolding.cusip == cusip, FundHolding.ticker.is_(None))
                .values(ticker=ticker)
            )
            updated += res.rowcount or 0
            await s.execute(
                update(PositionChange)
                .where(PositionChange.cusip == cusip, PositionChange.ticker.is_(None))
                .values(ticker=ticker)
            )
    logger.info("hedge_funds: backfilled ticker on %d holding row(s)", updated)
    return updated
