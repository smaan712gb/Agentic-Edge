"""EMA trend-stack engine — the Trend pillar of the technical spec.

Five exponential moving averages (8 / 21 / 34 not included — the operator's
spec is 8, 21, 50, 100, 200) define the trend. Perfect bullish alignment

    EMA8 > EMA21 > EMA50 > EMA100 > EMA200

earns the maximum score. Scoring is per adjacent pair — 25 points each of
the four orderings — so a stack that is "mostly right" degrades gracefully
instead of falling off a cliff:

    100  perfect alignment (institutional uptrend)
     75  one pair out of order
     50  mixed
     25  mostly inverted
      0  perfect INVERSE alignment (institutional downtrend)

Pure math on a list of closes — deterministic, testable offline, and emitted
into the point-in-time feature store as ``ema_stack_score`` so the IC harness
measures whether the stack actually predicts forward returns before any
scoring weight leans on it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("agentic_edge.research")

EMA_SPANS = (8, 21, 50, 100, 200)

# EMAs converge to the true value as history grows; below ~span+40 bars the
# 200 EMA is still SMA-seeded noise. We require 240 closes for a full stack.
_MIN_BARS_FULL_STACK = 240


def ema(closes: list[float], span: int) -> Optional[float]:
    """Standard EMA: SMA-seeded over the first ``span`` closes, then the
    usual alpha = 2/(span+1) recursion over the rest."""
    if len(closes) < span or span <= 0:
        return None
    alpha = 2.0 / (span + 1.0)
    value = sum(closes[:span]) / span
    for c in closes[span:]:
        value = value + alpha * (c - value)
    return value


def ema_stack(closes: list[float]) -> Optional[dict[str, Any]]:
    """Compute the 8/21/50/100/200 stack + alignment score from daily closes
    (oldest first). None when there isn't enough history for the full stack."""
    closes = [float(c) for c in closes if c is not None and c > 0]
    if len(closes) < _MIN_BARS_FULL_STACK:
        return None
    emas = {span: ema(closes, span) for span in EMA_SPANS}
    if any(v is None for v in emas.values()):
        return None

    pairs: list[dict[str, Any]] = []
    aligned = 0
    inverted = 0
    for fast, slow in zip(EMA_SPANS, EMA_SPANS[1:]):
        ok = emas[fast] > emas[slow]  # type: ignore[operator]
        pairs.append({"pair": f"{fast}>{slow}", "ok": ok})
        if ok:
            aligned += 1
        else:
            inverted += 1

    score = aligned * 25.0
    if score == 100.0:
        label = "perfect alignment"
    elif score >= 75.0:
        label = "mostly aligned"
    elif score >= 50.0:
        label = "mixed"
    elif score >= 25.0:
        label = "mostly inverted"
    else:
        label = "downtrend stack"

    price = closes[-1]
    return {
        "score": score,
        "label": label,
        "pairs": pairs,
        "emas": {f"ema{span}": round(emas[span], 4) for span in EMA_SPANS},  # type: ignore[arg-type]
        "price": round(price, 4),
        "price_above_8ema": price > emas[8],  # type: ignore[operator]
    }


async def compute_ema_stack(symbol: str) -> Optional[dict[str, Any]]:
    """Fetch ~2 years of daily closes via the standard fallback chain and
    compute the stack. None on any data failure — callers treat the trend
    pillar as best-effort."""
    try:
        from datetime import date, timedelta
        from tradingagents.dataflows.fallback import get_stock_data_with_fallback
        df = await get_stock_data_with_fallback(
            symbol, date.today() - timedelta(days=500), date.today(),
        )
        if df is None or "Close" not in df.columns:
            return None
        return ema_stack(df["Close"].astype(float).tolist())
    except Exception as e:
        logger.debug("trend: ema stack failed for %s: %s", symbol, e)
        return None
