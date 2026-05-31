"""Theme Rotation Detector — catch institutional sector rotation early.

Fuses leading indicators per theme and flags a theme as "rotating out" when
2+ independent signals agree (operator policy: require confirmation):

  1. Relative-strength breakdown — the theme's reference ETFs below their
     50-day MA AND negative 20-day momentum (reuse sector_regime).
  2. Options-flow distribution — UW flow tilt bearish / gamma negative on the
     theme's ETFs (smart money hedging/exiting first).
  3. Breadth deterioration — majority of the theme's names below their 20d MA.

When flagged, the entry loop halts NEW entries into the theme and the
maintenance loop takes profit on winners + tightens exit-pressure
sensitivity. All actions are low-regret (never dump a loser on a down day) —
see the rotation-detector project note.

This module only *detects + persists* state; the loops read it and act.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from ..config import get_settings
from ..db import ThemeRotation, ThemeSymbol, get_session as db_session
from .alerts import alert

logger = logging.getLogger("agentic_edge.rotation")

_BREADTH_SYMBOL_CAP = 12   # per theme, by insertion order


def _mean(vals: list[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


async def is_theme_rotating(theme_id: Optional[str]) -> tuple[bool, float, list[str]]:
    """Read persisted rotation state for a theme. (flagged, score, signals).
    Fail-open to not-rotating so the detector never blocks the loops."""
    if not theme_id:
        return False, 0.0, []
    try:
        async with db_session() as s:
            row = await s.get(ThemeRotation, theme_id)
        if row is None:
            return False, 0.0, []
        return bool(row.flagged), float(row.score or 0.0), list(row.signals_tripped or [])
    except Exception as e:
        logger.debug("rotation state read failed for %s: %s", theme_id, e)
        return False, 0.0, []


async def any_theme_rotating(theme_ids: list[str]) -> tuple[bool, list[str]]:
    """True if ANY of the given themes is flagged rotating (a symbol can live
    in several themes; if any is rotating, treat the name as rotation-exposed)."""
    flagged = []
    for tid in theme_ids:
        f, _, _ = await is_theme_rotating(tid)
        if f:
            flagged.append(tid)
    return bool(flagged), flagged


async def run_rotation_sweep() -> dict[str, Any]:
    """Recompute rotation state for every theme. Alerts on new flags."""
    settings = get_settings()
    summary: dict[str, Any] = {"themes": 0, "flagged": 0, "skipped_reason": None}
    if not getattr(settings, "ROTATION_DETECTOR_ENABLED", True):
        summary["skipped_reason"] = "ROTATION_DETECTOR_ENABLED=false"
        return summary

    min_signals = int(getattr(settings, "ROTATION_MIN_SIGNALS", 2))
    breadth_floor = float(getattr(settings, "ROTATION_BREADTH_BELOW_MA_PCT", 0.60))

    async with db_session() as s:
        rows = (await s.execute(select(ThemeSymbol.theme_id, ThemeSymbol.symbol))).all()
    theme_symbols: dict[str, list[str]] = {}
    for tid, sym in rows:
        if not tid or not sym:
            continue
        theme_symbols.setdefault(tid, [])
        if sym.upper() not in theme_symbols[tid]:
            theme_symbols[tid].append(sym.upper())
    # Skip the smoke/test themes.
    theme_symbols = {t: syms for t, syms in theme_symbols.items() if not t.startswith("smoke")}

    # Per-sweep cache so a symbol in several themes is priced once.
    sig_cache: dict[str, dict] = {}
    from .maint_loop import _compute_daily_signals  # lazy: avoids import cycle

    for theme_id, symbols in theme_symbols.items():
        summary["themes"] += 1
        tripped: list[str] = []
        evidence: dict[str, Any] = {}

        # --- Signal 1 + 2: RS breakdown + flow distribution (sector_regime) ---
        try:
            from tradingagents.signals.sector_regime import get_theme_regime
            regime = await get_theme_regime(theme_id)
            vs50 = _mean(list(regime.vs_50ma_pct.values()))
            mom = _mean(list(regime.momentum_20d_pct.values()))
            evidence["regime"] = {"regime": regime.regime, "mean_vs_50ma_pct": vs50, "mean_momentum_20d_pct": mom}
            if (vs50 is not None and vs50 < 0) and (mom is not None and mom < 0):
                tripped.append("rs_breakdown")

            bearish = sum(1 for u in regime.uw.values()
                          if u.flow_tilt == "bearish" or u.gamma_sign == "negative")
            bullish = sum(1 for u in regime.uw.values() if u.flow_tilt == "bullish")
            evidence["flow"] = {"bearish_etfs": bearish, "bullish_etfs": bullish}
            if bearish >= 1 and bearish > bullish:
                tripped.append("flow_distribution")
        except Exception as e:
            logger.debug("rotation: regime/flow failed for %s: %s", theme_id, e)

        # --- Signal 3: breadth (% of theme names below 20d MA) ---
        try:
            below = total = 0
            for sym in symbols[:_BREADTH_SYMBOL_CAP]:
                sig = sig_cache.get(sym)
                if sig is None:
                    sig = await _compute_daily_signals(sym)
                    sig_cache[sym] = sig
                ma20 = sig.get("ma_20d")
                last = sig.get("prior_close")  # latest close proxy
                if ma20 and last:
                    total += 1
                    if last < ma20:
                        below += 1
            frac = (below / total) if total else 0.0
            evidence["breadth"] = {"below_20dma": below, "evaluated": total, "fraction": round(frac, 2)}
            if total >= 3 and frac >= breadth_floor:
                tripped.append("breadth_deterioration")
        except Exception as e:
            logger.debug("rotation: breadth failed for %s: %s", theme_id, e)

        flagged = len(tripped) >= min_signals
        score = round(len(tripped) / 3.0, 3)

        # Persist + detect transition for alerting.
        async with db_session() as s:
            row = await s.get(ThemeRotation, theme_id)
            was_flagged = bool(row.flagged) if row else False
            if row is None:
                row = ThemeRotation(theme_id=theme_id)
                s.add(row)
            row.flagged = flagged
            row.score = score
            row.signals_tripped = tripped
            row.evidence = evidence
            row.computed_at = datetime.now(timezone.utc)

        if flagged:
            summary["flagged"] += 1
            if not was_flagged:   # new flag → alert
                await alert(
                    level="warning",
                    title=f"Theme rotation flagged: {theme_id} [{', '.join(tripped)}]",
                    body=(f"Institutions appear to be rotating out of {theme_id}. "
                          f"Halting new entries; taking profit on winners + tightening "
                          f"exit sensitivity. Evidence: {evidence}"),
                )

    logger.info("rotation sweep: %d themes, %d flagged", summary["themes"], summary["flagged"])
    return summary
