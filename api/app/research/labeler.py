"""Forward-return labeler — fills the ``labels`` on feature snapshots.

A feature row is written ``label_status='pending'`` with no labels. Once enough
calendar time has passed, this backfills the realised forward return at each
horizon so the validation harness has (feature, outcome) pairs to measure IC on.

  pending  → no horizon has fully elapsed yet
  partial  → some horizons known, the longest not yet
  final    → every horizon's forward window has elapsed and is priced

The forward-return math is a pure function (``forward_return``) mirroring the
exit-pressure replay harness, so it is unit-tested in isolation with no feed.

Research-only.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from ..db import SymbolFeatureSnapshot, get_session as db_session

logger = logging.getLogger("agentic_edge.research")

# Forward horizons in calendar days (match the exit-pressure harness convention).
HORIZONS: tuple[int, ...] = (5, 20, 60)
MAX_HORIZON = max(HORIZONS)


def forward_return(
    series: list[tuple[date, float]], t0: date, horizon_days: int,
) -> tuple[Optional[float], bool]:
    """(forward_return, window_complete) over (t0, t0 + horizon].

    Anchor = last close at or before ``t0``. Forward = last close in the window
    ``(t0, t0+horizon]``. ``window_complete`` is True only when the series
    actually extends to or past ``t0 + horizon`` (so the realised return is
    final, not just the latest-so-far). Returns (None, False) when unlabelable.
    """
    if not series:
        return None, False
    ordered = sorted(series, key=lambda x: x[0])
    at_or_before = [px for d, px in ordered if d <= t0]
    if not at_or_before:
        return None, False
    anchor = at_or_before[-1]
    if anchor <= 0:
        return None, False

    window_end = t0 + timedelta(days=horizon_days)
    window = [px for d, px in ordered if t0 < d <= window_end]
    last_date = ordered[-1][0]
    complete = last_date >= window_end
    if not window:
        return None, complete
    ret = window[-1] / anchor - 1.0
    return round(ret, 5), complete


async def _price_series(symbol: str, lookback_days: int) -> list[tuple[date, float]]:
    """Daily (date, close) for the symbol via the fallback chain. [] on failure."""
    try:
        from tradingagents.dataflows.fallback import get_stock_data_with_fallback
        df = await get_stock_data_with_fallback(
            symbol, date.today() - timedelta(days=lookback_days), date.today(),
        )
    except Exception as e:
        logger.debug("labeler: price feed failed for %s: %s", symbol, e)
        return []
    if df is None or len(df) == 0 or "Close" not in df.columns:
        return []
    out: list[tuple[date, float]] = []
    try:
        for idx, close in zip(df.index, df["Close"].astype(float).tolist()):
            d = idx.date() if hasattr(idx, "date") else idx
            out.append((d, float(close)))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("labeler: series parse failed for %s: %s", symbol, e)
        return []
    return out


async def run_labeler(*, max_symbols: int = 200) -> dict[str, object]:
    """Backfill labels on every non-final snapshot whose window has elapsed.

    Groups un-final snapshots by symbol, fetches each symbol's price series
    once, then labels every horizon that is now computable. Idempotent.
    """
    today = datetime.now(timezone.utc).date()
    summary: dict[str, object] = {"symbols": 0, "rows_updated": 0, "finalised": 0}

    async with db_session() as s:
        pending = (await s.execute(
            select(SymbolFeatureSnapshot)
            .where(SymbolFeatureSnapshot.label_status != "final")
            .order_by(SymbolFeatureSnapshot.symbol, SymbolFeatureSnapshot.as_of)
        )).scalars().all()

    by_symbol: dict[str, list[SymbolFeatureSnapshot]] = {}
    for snap in pending:
        by_symbol.setdefault(snap.symbol, []).append(snap)

    symbols = sorted(by_symbol.keys())[:max_symbols]
    summary["symbols"] = len(symbols)

    rows_updated = 0
    finalised = 0
    for sym in symbols:
        series = await _price_series(sym, lookback_days=MAX_HORIZON + 150)
        if not series:
            continue
        async with db_session() as s:
            for snap in by_symbol[sym]:
                row = await s.get(SymbolFeatureSnapshot, snap.id)
                if row is None:
                    continue
                t0 = row.as_of.date()
                labels = dict(row.labels or {})
                all_complete = True
                changed = False
                for h in HORIZONS:
                    key = f"fwd_ret_{h}d"
                    if key in labels and labels[key] is not None:
                        continue  # already final for this horizon
                    ret, complete = forward_return(series, t0, h)
                    if ret is not None and complete:
                        labels[key] = ret
                        changed = True
                    else:
                        all_complete = False
                if changed:
                    row.labels = labels
                    rows_updated += 1
                new_status = "final" if all_complete else (
                    "partial" if labels else "pending"
                )
                if new_status != row.label_status:
                    row.label_status = new_status
                    if new_status == "final":
                        finalised += 1
            await s.commit()

    summary["rows_updated"] = rows_updated
    summary["finalised"] = finalised
    logger.info("labeler: %d symbols, %d rows updated, %d finalised (as of %s)",
                len(symbols), rows_updated, finalised, today)
    return summary
