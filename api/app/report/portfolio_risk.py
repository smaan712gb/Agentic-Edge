"""Portfolio Risk Engine — the morning risk read on the managed book.

Computes, from the enriched holdings list + latest account snapshot:

  * portfolio health  — value-weighted average of each holding's latest
                        research composite, expressed 0-100
  * average pairwise correlation of 90-day daily returns across holdings
  * effective independent bets — N / (1 + (N-1)·avg_corr): the "you own 20
    stocks but only 5 bets" number
  * exposure by theme (a symbol in k themes contributes 1/k of its weight
    to each, so exposures sum to ~100%)
  * cash percentage and a plain risk label

Read-only decision support. Price history comes through the same fallback
chain the feature store uses; any failure degrades that field to null.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any, Optional

logger = logging.getLogger("agentic_edge.report")

_MAX_CORR_SYMBOLS = 12   # correlation fan-out cap — enough for this book size
_CORR_DAYS = 90


async def portfolio_risk(
    *, holdings: list[dict[str, Any]], account: Optional[dict[str, Any]],
) -> dict[str, Any]:
    cash_pct = (account or {}).get("cash_pct")
    if not holdings:
        return {
            "empty": True,
            "n_holdings": 0,
            "health_pct": None,
            "risk_label": "minimal — all cash",
            "avg_correlation": None,
            "effective_bets": None,
            "cash_pct": cash_pct if cash_pct is not None else 100.0,
            "theme_exposure": [],
        }

    # --- Weights: notional when we have prices, equal-weight otherwise -----
    weights: dict[str, float] = {}
    for h in holdings:
        qty = float(h.get("qty") or 0)
        px = float(h.get("last_price") or 0)
        weights[h["symbol"]] = abs(qty * px) if qty and px else 0.0
    if sum(weights.values()) <= 0:
        weights = {h["symbol"]: 1.0 for h in holdings}
    total_w = sum(weights.values())
    weights = {s: w / total_w for s, w in weights.items()}

    # --- Health: value-weighted latest composite (0-100) --------------------
    scored = [
        (weights[h["symbol"]], float(h["latest_score"]["composite"]))
        for h in holdings if h.get("latest_score")
    ]
    health = (
        round(sum(w * c for w, c in scored) / sum(w for w, _ in scored) * 10, 1)
        if scored else None
    )

    # --- Theme exposure ------------------------------------------------------
    theme_w: dict[str, float] = {}
    for h in holdings:
        themes = h.get("themes") or ["(no theme)"]
        for t in themes:
            theme_w[t] = theme_w.get(t, 0.0) + weights[h["symbol"]] / len(themes)
    exposure = sorted(
        ({"theme": t, "pct": round(w * 100, 1)} for t, w in theme_w.items()),
        key=lambda r: r["pct"], reverse=True,
    )

    # --- Correlation + effective bets ----------------------------------------
    avg_corr: Optional[float] = None
    n_eff: Optional[float] = None
    symbols = [h["symbol"] for h in holdings][:_MAX_CORR_SYMBOLS]
    try:
        returns = await _fetch_returns(symbols)
        avg_corr = _avg_pairwise_correlation(returns)
        if avg_corr is not None:
            n = len(returns)
            n_eff = round(n / (1 + (n - 1) * max(0.0, avg_corr)), 1)
            avg_corr = round(avg_corr, 3)
    except Exception as e:
        logger.debug("portfolio risk: correlation failed: %s", e)

    # --- Risk label ------------------------------------------------------------
    top_theme_pct = exposure[0]["pct"] if exposure else 0.0
    if (avg_corr or 0) >= 0.75 or top_theme_pct >= 50:
        risk = "high — concentrated"
    elif (avg_corr or 0) >= 0.50 or top_theme_pct >= 35:
        risk = "moderate"
    else:
        risk = "low"

    return {
        "empty": False,
        "n_holdings": len(holdings),
        "health_pct": health,
        "risk_label": risk,
        "avg_correlation": avg_corr,
        "effective_bets": n_eff,
        "cash_pct": cash_pct,
        "theme_exposure": exposure[:8],
    }


async def _fetch_returns(symbols: list[str]) -> dict[str, list[float]]:
    from tradingagents.dataflows.fallback import get_stock_data_with_fallback

    async def _one(sym: str) -> tuple[str, list[float]]:
        try:
            df = await get_stock_data_with_fallback(
                sym, date.today() - timedelta(days=_CORR_DAYS + 60), date.today(),
            )
            closes = df["Close"].astype(float).tolist() if df is not None else []
            rets = [
                c1 / c0 - 1.0 for c0, c1 in zip(closes[:-1], closes[1:]) if c0 > 0
            ][-_CORR_DAYS:]
            return sym, rets
        except Exception:
            return sym, []

    rows = await asyncio.gather(*[_one(s) for s in symbols])
    # Align on the shortest usable series; drop symbols with <30 returns.
    usable = {s: r for s, r in rows if len(r) >= 30}
    if len(usable) < 2:
        return {}
    min_len = min(len(r) for r in usable.values())
    return {s: r[-min_len:] for s, r in usable.items()}


def _avg_pairwise_correlation(returns: dict[str, list[float]]) -> Optional[float]:
    syms = list(returns)
    if len(syms) < 2:
        return None
    corrs: list[float] = []
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            c = _pearson(returns[syms[i]], returns[syms[j]])
            if c is not None:
                corrs.append(c)
    return sum(corrs) / len(corrs) if corrs else None


def corr_haircut_factor(avg_corr: Optional[float], *, high: float, med: float) -> float:
    """Sizing multiplier from a candidate's average correlation to the book:
    ≥high → 0.5×, ≥med → 0.75×, else (or unknown) 1.0. Pure + unit-tested;
    consumed by the entry loop's correlation-aware haircut."""
    if avg_corr is None:
        return 1.0
    if avg_corr >= high:
        return 0.5
    if avg_corr >= med:
        return 0.75
    return 1.0


async def candidate_book_correlation(
    candidate: str, book_symbols: list[str],
) -> Optional[float]:
    """Average pairwise 90d daily-return correlation between ``candidate`` and
    each name currently in the book. None when there isn't enough data —
    callers fail open (no haircut)."""
    book = [s for s in dict.fromkeys(book_symbols) if s != candidate][:_MAX_CORR_SYMBOLS]
    if not book:
        return None
    returns = await _fetch_returns([candidate] + book)
    cand = returns.get(candidate)
    if not cand:
        return None
    corrs = [
        c for s, r in returns.items() if s != candidate
        for c in [_pearson(cand, r)] if c is not None
    ]
    return sum(corrs) / len(corrs) if corrs else None


def _pearson(a: list[float], b: list[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return cov / (va ** 0.5 * vb ** 0.5)
