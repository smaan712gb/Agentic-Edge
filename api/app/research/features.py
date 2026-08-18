"""Feature snapshot orchestrator — writes one point-in-time row per symbol.

Pulls the whole theme universe, computes every feature family AS OF today, and
upserts into ``symbol_feature_snapshots``. Families:

  * graph        — always on, network-free (graph_features.py)
  * cross-theme  — smart-money confirmation across a name's themes (DB)
  * market       — momentum / MA distance / rel-volume (best-effort price feed)
  * flow         — UW flow imbalance + gamma sign + dark-pool notional (best-eff)
  * z_*          — cross-sectional standardisation of every numeric raw feature
                   across the universe on the snapshot day (history-free rank)

Best-effort means: a provider miss writes ``None`` for that one feature and is
logged at debug — it never fails the snapshot. Graph + cross-theme always carry
signal, so a snapshot is useful even with every external feed down.

Research-only. Nothing here is read by an entry/exit gate.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

from ..db import SymbolFeatureSnapshot, ThemeSymbol, get_session as db_session
from .graph_features import compute_graph_features

logger = logging.getLogger("agentic_edge.research")

# Raw numeric features that get a cross-sectional z_* twin. Lists/strings and
# the z_* outputs themselves are excluded.
_Z_CANDIDATE_KEYS: tuple[str, ...] = (
    "theme_count",
    "theme_centrality",
    "co_membership_degree",
    "avg_theme_size",
    "smartmoney_theme_confirm",
    "momentum_20d",
    "momentum_60d",
    "dist_50dma",
    "rvol",
    "ema_stack_score",
    "pattern_bull_count",
    "accum_dist_balance",
    "cba_confidence_num",
    "flow_imbalance",
    "gamma_sign_num",
    "dark_pool_notional",
    # Directional, and the one that actually carries information: gross
    # off-exchange notional z-scores mostly to market cap, whereas the
    # buy/sell imbalance says whether that size was accumulation or
    # distribution.
    "dark_pool_imbalance",
    "dark_pool_net_notional",
)


def cross_sectional_z(
    rows: dict[str, dict[str, Any]], keys: tuple[str, ...] = _Z_CANDIDATE_KEYS,
) -> None:
    """In-place: add ``z_<key>`` to each row, standardised across the universe.

    For each key, gather the present numeric values across all symbols, compute
    population mean/std, and write ``z_<key>`` per symbol. Degenerate cases
    (fewer than 2 values, or zero spread) write 0.0 so the column stays defined
    and never injects a phantom rank. Symbols missing the raw value get ``None``.
    """
    for key in keys:
        vals = [
            (sym, r[key]) for sym, r in rows.items()
            if isinstance(r.get(key), (int, float)) and not _is_nan(r.get(key))
        ]
        zkey = f"z_{key}"
        if len(vals) < 2:
            for sym in rows:
                rows[sym][zkey] = 0.0 if key in rows[sym] else None
            continue
        nums = [v for _, v in vals]
        mean = sum(nums) / len(nums)
        var = sum((x - mean) ** 2 for x in nums) / len(nums)
        std = math.sqrt(var)
        present = {sym for sym, _ in vals}
        for sym, r in rows.items():
            if sym not in present:
                r[zkey] = None
            elif std == 0.0:
                r[zkey] = 0.0
            else:
                r[zkey] = round((r[key] - mean) / std, 4)


def _is_nan(x: Any) -> bool:
    return isinstance(x, float) and math.isnan(x)


# ---------------------------------------------------------------------------
# Universe + cross-theme (DB)
# ---------------------------------------------------------------------------


async def _load_universe() -> dict[str, list[str]]:
    """{theme_id: [symbols]} across every theme."""
    async with db_session() as s:
        rows = (await s.execute(select(ThemeSymbol.theme_id, ThemeSymbol.symbol))).all()
    universe: dict[str, list[str]] = {}
    for theme_id, sym in rows:
        universe.setdefault(theme_id, []).append((sym or "").upper())
    return universe


async def _smartmoney_theme_confirm(symbol: str, themes: list[str]) -> Optional[int]:
    """In how many of the name's themes is it smart-money-confirmed.

    Best-effort: the hedge-fund repo's confirmation is symbol-level, not
    theme-level, so we read the one symbol-level confirmation and multiply by
    theme breadth — a name confirmed by 2+ managers counts in each theme it
    anchors. Returns None if the tracker layer is unavailable.
    """
    try:
        from ..hedge_funds.repo import HedgeFundRepo
        async with db_session() as s:
            sm = await HedgeFundRepo(s).smart_money_for_symbol(ticker=symbol)
    except Exception as e:
        logger.debug("research: smart-money read failed for %s: %s", symbol, e)
        return None
    confirmed = bool(sm.get("confirmation")) or (sm.get("manager_count") or 0) >= 2
    return len(themes) if confirmed else 0


# ---------------------------------------------------------------------------
# Market (best-effort price feed)
# ---------------------------------------------------------------------------


async def _market_features(symbol: str) -> dict[str, Optional[float]]:
    """momentum_20d / momentum_60d / dist_50dma / rvol / ema_stack_score via the
    fallback chain. The 500-day window exists for the 200 EMA (the trend-stack
    spec: 8/21/50/100/200 alignment); the shorter factors only need 120."""
    out: dict[str, Optional[float]] = {
        "momentum_20d": None, "momentum_60d": None, "dist_50dma": None, "rvol": None,
        "ema_stack_score": None, "pattern_bull_count": None, "accum_dist_balance": None,
    }
    try:
        from datetime import date
        from tradingagents.dataflows.fallback import get_stock_data_with_fallback
        df = await get_stock_data_with_fallback(
            symbol, date.today() - timedelta(days=500), date.today(),
        )
    except Exception as e:
        logger.debug("research: price feed failed for %s: %s", symbol, e)
        return out
    try:
        if df is None or len(df) < 21 or "Close" not in df.columns:
            return out
        closes = df["Close"].astype(float).tolist()
        last = closes[-1]
        if last <= 0:
            return out
        if len(closes) >= 21:
            out["momentum_20d"] = round(last / closes[-21] - 1.0, 5)
        if len(closes) >= 61:
            out["momentum_60d"] = round(last / closes[-61] - 1.0, 5)
        if len(closes) >= 50:
            ma50 = sum(closes[-50:]) / 50
            if ma50 > 0:
                out["dist_50dma"] = round(last / ma50 - 1.0, 5)
        if "Volume" in df.columns:
            vols = df["Volume"].astype(float).tolist()
            if len(vols) >= 21:
                base = sum(vols[-21:-1]) / 20
                if base > 0:
                    out["rvol"] = round(vols[-1] / base, 4)
        from .trend import ema_stack
        stack = ema_stack(closes)
        if stack is not None:
            out["ema_stack_score"] = stack["score"]
        # Pattern + structure labels → IC-validatable scalars.
        from .patterns import bull_pattern_count, detect_patterns, market_structure
        highs = (df["High"] if "High" in df.columns else df["Close"]).astype(float).tolist()
        lows = (df["Low"] if "Low" in df.columns else df["Close"]).astype(float).tolist()
        vols_full = (df["Volume"] if "Volume" in df.columns else df["Close"] * 0).astype(float).tolist()
        pats = detect_patterns(highs=highs, lows=lows, closes=closes, volumes=vols_full)
        out["pattern_bull_count"] = float(bull_pattern_count(pats))
        ms = market_structure(highs=highs, lows=lows, closes=closes, volumes=vols_full)
        if ms is not None:
            out["accum_dist_balance"] = float(
                ms["accumulation_days_25"] - ms["distribution_days_25"])
    except Exception as e:
        logger.debug("research: market feature calc failed for %s: %s", symbol, e)
    return out


# ---------------------------------------------------------------------------
# Flow (best-effort UW)
# ---------------------------------------------------------------------------


async def _flow_features(symbol: str) -> dict[str, Optional[float]]:
    """flow_imbalance + gamma_sign_num (reuse the proven UW context) and
    dark_pool_notional (wires the previously-unused dark-pool method)."""
    out: dict[str, Optional[float]] = {
        "flow_imbalance": None, "gamma_sign_num": None, "dark_pool_notional": None,
        "dark_pool_net_notional": None, "dark_pool_imbalance": None,
        "dark_pool_prints": None,
    }
    # Reuse the same UW context the regime read uses — proven path.
    try:
        from tradingagents.signals.sector_regime import _fetch_uw_context
        ctx = await _fetch_uw_context(symbol)
        call_p = float(getattr(ctx, "flow_premium_24h_call", 0.0) or 0.0)
        put_p = float(getattr(ctx, "flow_premium_24h_put", 0.0) or 0.0)
        denom = call_p + put_p
        if denom > 0:
            out["flow_imbalance"] = round((call_p - put_p) / denom, 4)
        sign = getattr(ctx, "gamma_sign", None)
        out["gamma_sign_num"] = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}.get(sign)
    except Exception as e:
        logger.debug("research: UW flow context failed for %s: %s", symbol, e)
    # Dark-pool prints — SIGNED off-exchange pressure in the last 24h.
    #
    # This used to sum gross notional, which says only that size traded
    # off-exchange. A name can print enormous volume while being distributed;
    # only the side tells you which. dark_pool_pressure classifies each print
    # against the NBBO at execution (at/above ask = buy, at/below bid = sell)
    # and returns a normalised -1..+1 imbalance.
    #
    # It was also reading a route that never existed (404 on every call), so
    # this feature was None for every symbol and the quant overlay dropped
    # z_dark_pool_notional as zero-variance. Both are fixed.
    try:
        from tradingagents.dataflows.providers.unusual_whales import UnusualWhalesProvider
        uw = UnusualWhalesProvider()
        dp = await uw.dark_pool_pressure(symbol, hours=24)
        out["dark_pool_notional"] = dp.get("total_notional")
        out["dark_pool_net_notional"] = dp.get("net_notional")
        out["dark_pool_imbalance"] = dp.get("imbalance")
        out["dark_pool_prints"] = dp.get("n_prints")
    except Exception as e:
        logger.debug("research: dark-pool read failed for %s: %s", symbol, e)
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _cba_features(symbols: list[str], as_of: datetime) -> dict[str, float]:
    """Numeric closing-bell-accumulation read for today: confidence of a
    passing setup (high=2, med=1, low=0.5), 0 when no setup. One bounded DB
    query — the first decision-side consumer of the CBA sweep's output."""
    from ..db import ClosingAccumulationSignal
    out: dict[str, float] = {}
    try:
        async with db_session() as s:
            rows = (
                await s.execute(
                    select(ClosingAccumulationSignal)
                    .where(ClosingAccumulationSignal.date == as_of)
                    .where(ClosingAccumulationSignal.symbol.in_(symbols))
                )
            ).scalars().all()
        conf_num = {"high": 2.0, "med": 1.0, "medium": 1.0, "low": 0.5}
        for r in rows:
            out[r.symbol] = (
                conf_num.get((r.confidence or "").lower(), 0.5)
                if r.setup_passes else 0.0
            )
    except Exception as e:
        logger.debug("research: cba feature read failed: %s", e)
    return out


async def build_snapshot(
    *, as_of: Optional[datetime] = None, with_market: bool = True, with_flow: bool = True,
    max_symbols: int = 200,
) -> dict[str, Any]:
    """Compute + persist a feature snapshot for the whole universe.

    Idempotent per (symbol, as_of): re-running the same day overwrites that
    day's features (labels, once attached, are preserved). Returns a summary.
    """
    as_of = (as_of or datetime.now(timezone.utc)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    summary: dict[str, Any] = {
        "as_of": as_of.isoformat(), "symbols": 0, "written": 0,
        "with_market": with_market, "with_flow": with_flow,
    }

    universe = await _load_universe()
    graph = compute_graph_features(universe)        # {symbol: {graph features}}
    symbols = sorted(graph.keys())[:max_symbols]
    summary["symbols"] = len(symbols)
    if not symbols:
        summary["skipped_reason"] = "no theme symbols"
        return summary

    # Assemble raw feature rows. Graph + cross-theme always; market/flow best-eff.
    rows: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        rows[sym] = dict(graph[sym])  # theme_count, centrality, degree, avg_size, themes

    # Cross-theme confirmation (cheap DB reads; gather bounded).
    confirms = await _gather_bounded(
        [_smartmoney_theme_confirm(s, graph[s]["themes"]) for s in symbols], limit=8,
    )
    for sym, c in zip(symbols, confirms):
        rows[sym]["smartmoney_theme_confirm"] = c

    if with_market:
        mkt = await _gather_bounded([_market_features(s) for s in symbols], limit=6)
        for sym, m in zip(symbols, mkt):
            rows[sym].update(m or {})

    if with_flow:
        flow = await _gather_bounded([_flow_features(s) for s in symbols], limit=6)
        for sym, f in zip(symbols, flow):
            rows[sym].update(f or {})

    # Closing-bell accumulation (today's CBA sweep row) — wires the previously
    # unread closing_accumulation_signals table into the IC-validated pipeline.
    cba = await _cba_features(symbols, as_of)
    for sym in symbols:
        rows[sym]["cba_confidence_num"] = cba.get(sym, 0.0)

    # Cross-sectional standardisation across the universe (history-free rank).
    cross_sectional_z(rows)

    # Manager-archetype persona scores — deterministic functions of the same-day
    # z-features (no lookahead). Folded in as their own persona_* family.
    from .personas import score_all
    for sym in symbols:
        for persona, scr in score_all(rows[sym]).items():
            rows[sym][f"persona_{persona}"] = scr

    # Upsert one row per symbol for this as_of.
    written = 0
    async with db_session() as s:
        for sym in symbols:
            existing = (await s.execute(
                select(SymbolFeatureSnapshot)
                .where(SymbolFeatureSnapshot.symbol == sym)
                .where(SymbolFeatureSnapshot.as_of == as_of)
            )).scalar_one_or_none()
            if existing is None:
                s.add(SymbolFeatureSnapshot(
                    symbol=sym, as_of=as_of, features=rows[sym], label_status="pending",
                ))
            else:
                existing.features = rows[sym]   # labels + status untouched
            written += 1
        await s.commit()

    summary["written"] = written
    logger.info("feature snapshot %s: %d symbols written", as_of.date(), written)
    return summary


async def ensure_todays_snapshot() -> dict[str, Any]:
    """Guarantee a feature snapshot exists for today before the scorecard reads
    it — closes the freshness gap between the 09:00 theme run and the 16:30
    snapshot cron. Idempotent: if today's snapshot already exists, it's a no-op.

    Called at the head of the theme run so the quant overlay injects *same-day*
    features into the scorecard's decision. Best-effort: on any failure the run
    proceeds without fresh features (the overlay just uses the last snapshot or
    none)."""
    as_of = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with db_session() as s:
        existing = (await s.execute(
            select(SymbolFeatureSnapshot.id)
            .where(SymbolFeatureSnapshot.as_of == as_of)
            .limit(1)
        )).first()
    if existing is not None:
        return {"as_of": as_of.isoformat(), "built": False, "reason": "already exists"}
    return await build_snapshot(as_of=as_of)


async def _gather_bounded(coros: list, *, limit: int) -> list:
    """Run coroutines with bounded concurrency; failures become None."""
    sem = asyncio.Semaphore(limit)

    async def _run(c):
        async with sem:
            try:
                return await c
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("research: bounded task failed: %s", e)
                return None

    return await asyncio.gather(*[_run(c) for c in coros])
