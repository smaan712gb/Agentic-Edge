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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

from ..config import get_settings
from ..db import ThemeRotation, ThemeSymbol, get_session as db_session
from .alerts import alert

logger = logging.getLogger("agentic_edge.rotation")

_BREADTH_SYMBOL_CAP = 12   # per theme, by insertion order

# Signal taxonomy. The distinction is the whole point of the detector:
#
#   PRICE signals are technical reads over the theme's own names. ANY ordinary
#   pullback produces them, and they are not independent of each other —
#   rs_breakdown (ETFs vs 50d MA + 20d momentum) and breadth_deterioration
#   (% of names below their 20d MA) measure the same downtrend twice.
#
#   INSTITUTIONAL signals are evidence of money actually moving: options-flow
#   distribution, tracked funds trimming/exiting (13F + 13D/G), and clustered
#   bearish news. A pullback does not manufacture these.
#
# Flagging requires at least one INSTITUTIONAL signal, so price weakness alone
# can never call rotation. Before this split the effective rule was "one price
# signal plus flow_distribution", and flow_distribution was trivially true —
# it tripped on 15 of 17 themes (2026-08-08) because it counted near-universal
# negative dealer gamma and compared it against a `bullish` count that was 0
# everywhere. That is how a routine dip halted 17 entries on 2026-08-17.
_PRICE_SIGNALS = frozenset({"rs_breakdown", "breadth_deterioration"})
_INSTITUTIONAL_SIGNALS = frozenset(
    {"flow_distribution", "institutional_selling", "news_negative",
     "dark_pool_distribution"})


def _mean(vals: list[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


async def is_theme_rotating(theme_id: Optional[str]) -> tuple[bool, float, list[str]]:
    """Read persisted rotation state for a theme. (flagged, score, signals).
    Fail-open to not-rotating so the detector never blocks the loops.

    STALENESS GUARD: a rotation flag is a read of where institutional money is
    moving *right now*. An old row isn't weak evidence — it's wrong evidence: it
    describes a market that no longer exists. After any downtime the persisted
    flags keep halting entries on conditions that have since reversed.

    Observed 2026-08-17: flags computed 2026-08-08 (money leaving the AI complex)
    blocked 17 of the day's highest-conviction candidates — MU, TER, SNDK, STX,
    MRVL, CRDO — on 'breadth_deterioration', while the live tape read +2.4% with
    76% of the universe up and money rotating back INTO semis. The detector had
    no notion of its own freshness, so week-old bearish state silently vetoed a
    bullish day.

    Rows older than ROTATION_MAX_AGE_HOURS are therefore ignored rather than
    trusted. Fail-open matches this module's stated policy: not blocking is the
    low-regret direction, and a genuinely rotating theme is re-flagged by the
    next sweep (every 30 min during RTH) within one entry tick.
    """
    if not theme_id:
        return False, 0.0, []
    try:
        async with db_session() as s:
            row = await s.get(ThemeRotation, theme_id)
        if row is None:
            return False, 0.0, []
        if not row.flagged:
            return False, float(row.score or 0.0), list(row.signals_tripped or [])

        from .entry_loop import rotation_flag_is_fresh
        max_age_h = float(get_settings().ROTATION_MAX_AGE_HOURS)
        if not rotation_flag_is_fresh(row.computed_at, max_age_h):
            computed = row.computed_at
            if computed is not None and computed.tzinfo is None:
                computed = computed.replace(tzinfo=timezone.utc)
            age_h = ((datetime.now(timezone.utc) - computed).total_seconds() / 3600.0
                     if computed else float("inf"))
            logger.warning(
                "rotation: IGNORING stale flag for %s — computed %.1fh ago (max %.0fh). "
                "Stale rotation state describes a market that no longer exists; "
                "failing open until the next sweep refreshes it.",
                theme_id, age_h, max_age_h,
            )
            return False, 0.0, []

        return True, float(row.score or 0.0), list(row.signals_tripped or [])
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


def flow_is_distributing(
    bearish_tilt: int, bullish_tilt: int, negative_gamma: int, n_etfs: int,
) -> bool:
    """Whether options flow shows genuine distribution across a theme's ETFs.

    Pure + testable. Replaces::

        bearish = count(flow_tilt=="bearish" OR gamma_sign=="negative")
        bullish = count(flow_tilt=="bullish")
        trip if bearish >= 1 and bearish > bullish

    which had two defects that together made it a constant (15/17 themes on
    2026-08-08, and `bullish` was 0 on every single theme):

      1. It counted a SINGLE ETF as sufficient, and folded negative dealer
         gamma into the bearish count. Negative gamma is the normal state for
         most equity ETFs most of the time, so `bearish >= 1` was ~always true.
      2. It was asymmetric — `bearish` counted flow-tilt OR gamma while
         `bullish` counted flow-tilt only, so an ETF that was bullish on flow
         AND negative on gamma incremented both sides. The bearish set was
         larger by construction.

    Now: tilt is compared symmetrically (tilt vs tilt), gamma only CORROBORATES
    rather than substitutes, and a lone ETF can no longer carry the signal.
    """
    if n_etfs <= 0 or bearish_tilt <= 0:
        return False
    if bearish_tilt <= bullish_tilt:
        return False
    # A clear bearish majority of the theme's ETFs...
    majority = bearish_tilt * 2 >= n_etfs
    # ...corroborated by dealer positioning, or overwhelming on tilt alone.
    corroborated = negative_gamma >= 1 or bearish_tilt >= 3
    return bool(majority and corroborated and bearish_tilt >= 2)


async def _institutional_selling_signal(
    symbols: list[str], min_fraction: float,
) -> tuple[bool, dict[str, Any]]:
    """Fresh 13F trims/exits or 13D/G reductions across a theme's names.

    This is the signal the hedge-fund tracker was built for and the rotation
    detector never consumed: funds reporting OUT of a cluster of a theme's names
    is rotation by definition, and unlike price it cannot be produced by a dip.
    Fail-open (False) so the tracker can never block the sweep.
    """
    hits: list[str] = []
    evaluated = 0
    try:
        from api.app.hedge_funds.conviction import recent_manager_selling
        for sym in symbols[:_BREADTH_SYMBOL_CAP]:
            try:
                selling, _meta = await recent_manager_selling(sym)
            except Exception:  # noqa: BLE001 — one bad name must not kill the sweep
                continue
            evaluated += 1
            if selling:
                hits.append(sym)
    except Exception as e:  # noqa: BLE001
        logger.debug("rotation: institutional-selling signal unavailable: %s", e)
        return False, {"error": str(e)}
    frac = (len(hits) / evaluated) if evaluated else 0.0
    detail = {"selling": sorted(hits), "evaluated": evaluated, "fraction": round(frac, 2)}
    # Require a real cluster, not one name: a single fund trimming one position
    # is portfolio housekeeping, several across a theme is a rotation.
    return bool(evaluated >= 3 and len(hits) >= 2 and frac >= min_fraction), detail


async def _news_negative_signal(
    theme_id: str, symbols: list[str], min_fraction: float, lookback_days: int,
) -> tuple[bool, dict[str, Any]]:
    """Clustered bearish news across a theme's names in the lookback window.

    The chokepoint/bearish news sweep already writes ``news_mentions``; nothing
    read it for rotation. News is the earliest of the institutional signals —
    a sector downgrade or demand-cut story typically precedes both the 13F
    print and the sustained price break.
    """
    from sqlalchemy import func as _func

    from ..db import NewsMention
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))
        syms = {s.upper() for s in symbols}
        async with db_session() as s:
            rows = (await s.execute(
                select(NewsMention.ticker, NewsMention.sentiment)
                .where(NewsMention.published_at >= cutoff)
                .where(_func.lower(NewsMention.sentiment) == "bearish")
            )).all()
    except Exception as e:  # noqa: BLE001
        logger.debug("rotation: news signal unavailable for %s: %s", theme_id, e)
        return False, {"error": str(e)}

    hit_syms = {(t or "").upper() for t, _ in rows if (t or "").upper() in syms}
    frac = (len(hit_syms) / len(syms)) if syms else 0.0
    detail = {"bearish_names": sorted(hit_syms), "universe": len(syms),
              "fraction": round(frac, 2), "lookback_days": lookback_days}
    return bool(len(syms) >= 3 and len(hit_syms) >= 2 and frac >= min_fraction), detail


async def _dark_pool_signal(
    symbols: list[str], min_fraction: float, min_imbalance: float,
) -> tuple[bool, dict[str, Any]]:
    """Clustered off-exchange DISTRIBUTION across a theme's names.

    Block prints are where institutions move size without showing it on the
    lit book, so a theme being quietly distributed off-exchange is rotation in
    its most literal form — and it typically precedes the price breakdown that
    rs_breakdown and breadth_deterioration only see afterwards.

    Uses the SIGNED imbalance, not gross notional: a name can print enormous
    off-exchange volume while being accumulated. Measured 2026-08-18 — FN
    +0.59 (accumulation) against ONTO -1.00 and AAOI -0.40 (distribution) on
    the same session; gross volume would have ranked all three together.

    Fail-open: the feed being unavailable must never manufacture a rotation
    call, so an error yields False.
    """
    hits: list[str] = []
    evaluated = 0
    detail: dict[str, Any] = {}
    try:
        from tradingagents.dataflows.providers.unusual_whales import UnusualWhalesProvider
        uw = UnusualWhalesProvider()
        for sym in symbols[:_BREADTH_SYMBOL_CAP]:
            try:
                dp = await uw.dark_pool_pressure(sym, hours=24)
            except Exception:  # noqa: BLE001 — one bad name must not kill the sweep
                continue
            imb = dp.get("imbalance")
            if imb is None:
                continue
            evaluated += 1
            detail[sym] = round(float(imb), 3)
            if float(imb) <= -abs(min_imbalance):
                hits.append(sym)
    except Exception as e:  # noqa: BLE001
        logger.debug("rotation: dark-pool signal unavailable: %s", e)
        return False, {"error": str(e)}

    frac = (len(hits) / evaluated) if evaluated else 0.0
    out = {"distributed": sorted(hits), "evaluated": evaluated,
           "fraction": round(frac, 2), "imbalances": detail,
           "min_imbalance": min_imbalance}
    # Require a genuine cluster: one distributed name is a single seller, a
    # majority of the theme is the theme being rotated out of.
    return bool(evaluated >= 3 and len(hits) >= 2 and frac >= min_fraction), out


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

    summary["unmeasured"] = 0

    for theme_id, symbols in theme_symbols.items():
        summary["themes"] += 1
        tripped: list[str] = []
        evidence: dict[str, Any] = {}
        # Did we actually OBSERVE anything this pass? Every signal below sits in
        # its own try/except, so a data-layer outage makes them all fail quietly
        # and `tripped` stays empty — which is indistinguishable from a healthy
        # "nothing is rotating". Persisting that as a verdict WITH a fresh
        # computed_at would both clear genuine flags and defeat the staleness
        # guard in is_theme_rotating (the row would look current, so nothing
        # would ignore it). Track measurement success explicitly.
        measured_price = False
        measured_inst = False

        # --- Signal 1 + 2: RS breakdown + flow distribution (sector_regime) ---
        try:
            from tradingagents.signals.sector_regime import get_theme_regime
            regime = await get_theme_regime(theme_id)
            vs50 = _mean(list(regime.vs_50ma_pct.values()))
            mom = _mean(list(regime.momentum_20d_pct.values()))
            evidence["regime"] = {"regime": regime.regime, "mean_vs_50ma_pct": vs50, "mean_momentum_20d_pct": mom}
            if (vs50 is not None and vs50 < 0) and (mom is not None and mom < 0):
                tripped.append("rs_breakdown")

            bearish = sum(1 for u in regime.uw.values() if u.flow_tilt == "bearish")
            bullish = sum(1 for u in regime.uw.values() if u.flow_tilt == "bullish")
            neg_gamma = sum(1 for u in regime.uw.values() if u.gamma_sign == "negative")
            n_etfs = len(regime.uw)
            evidence["flow"] = {"bearish_tilt": bearish, "bullish_tilt": bullish,
                                "negative_gamma": neg_gamma, "n_etfs": n_etfs}
            if flow_is_distributing(bearish, bullish, neg_gamma, n_etfs):
                tripped.append("flow_distribution")
            # 'unknown' regime means the ETF read itself came back empty.
            if regime.regime != "unknown" or n_etfs > 0:
                measured_price = True
                measured_inst = measured_inst or n_etfs > 0
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
                # Latest close, NOT prior_close: prior_close is one session
                # stale while ma_20d includes the latest bar, which biased
                # breadth bearish on rally days (14/17 false flags 2026-07-09).
                last = sig.get("last_close") or sig.get("prior_close")
                if ma20 and last:
                    total += 1
                    if last < ma20:
                        below += 1
            frac = (below / total) if total else 0.0
            evidence["breadth"] = {"below_20dma": below, "evaluated": total, "fraction": round(frac, 2)}
            if total >= 3:
                measured_price = True
            if total >= 3 and frac >= breadth_floor:
                tripped.append("breadth_deterioration")
        except Exception as e:
            logger.debug("rotation: breadth failed for %s: %s", theme_id, e)

        # --- Signal 4: fresh institutional selling across the theme's names ---
        try:
            sell_hit, sell_detail = await _institutional_selling_signal(
                symbols, float(getattr(settings, "ROTATION_INSTITUTIONAL_SELL_FRAC", 0.25)))
            evidence["institutional_selling"] = sell_detail
            if int(sell_detail.get("evaluated") or 0) >= 3:
                measured_inst = True
            if sell_hit:
                tripped.append("institutional_selling")
        except Exception as e:
            logger.debug("rotation: institutional-sell signal failed for %s: %s", theme_id, e)

        # --- Signal 6: clustered off-exchange distribution -------------------
        try:
            dp_hit, dp_detail = await _dark_pool_signal(
                symbols,
                float(getattr(settings, "ROTATION_DARKPOOL_FRAC", 0.5)),
                float(getattr(settings, "ROTATION_DARKPOOL_MIN_IMBALANCE", 0.25)))
            evidence["dark_pool"] = dp_detail
            if int(dp_detail.get("evaluated") or 0) >= 3:
                measured_inst = True
            if dp_hit:
                tripped.append("dark_pool_distribution")
        except Exception as e:
            logger.debug("rotation: dark-pool signal failed for %s: %s", theme_id, e)

        # --- Signal 5: clustered bearish news across the theme's names -------
        try:
            news_hit, news_detail = await _news_negative_signal(
                theme_id, symbols,
                float(getattr(settings, "ROTATION_NEWS_BEARISH_FRAC", 0.25)),
                int(getattr(settings, "ROTATION_NEWS_LOOKBACK_DAYS", 7)))
            evidence["news"] = news_detail
            if news_hit:
                tripped.append("news_negative")
        except Exception as e:
            logger.debug("rotation: news signal failed for %s: %s", theme_id, e)

        # --- Decision -------------------------------------------------------
        # Two independent conditions, both required:
        #   (a) enough signals overall, and
        #   (b) at least one INSTITUTIONAL signal — price weakness alone is a
        #       pullback, not a rotation, no matter how many price reads agree.
        institutional = [t for t in tripped if t in _INSTITUTIONAL_SIGNALS]
        require_inst = bool(getattr(settings, "ROTATION_REQUIRE_INSTITUTIONAL", True))
        meets_count = len(tripped) >= min_signals
        meets_kind = (not require_inst) or bool(institutional)
        candidate = meets_count and meets_kind

        # Persistence: rotation is a multi-day move, so demand the same call on
        # N consecutive sweeps. A single noisy reading can no longer halt
        # entries; a genuine rotation still confirms within a few hours.
        confirm_needed = max(1, int(getattr(settings, "ROTATION_CONFIRM_SWEEPS", 2)))

        # A sweep that measured NOTHING has no verdict to offer. Leave the prior
        # row — including its old computed_at — completely untouched, so
        # is_theme_rotating's staleness guard keeps ageing it out normally. The
        # alternative (writing flagged=False with a fresh timestamp) would
        # publish a confident all-clear built on zero observations, and make it
        # look freshly measured. Skipping is what lets the guard do its job.
        if not (measured_price or measured_inst):
            summary["unmeasured"] += 1
            logger.warning(
                "rotation: %s NOT MEASURED this sweep (no ETF/breadth/institutional "
                "data) — leaving prior state and timestamp untouched rather than "
                "recording a false 'not rotating'", theme_id,
            )
            continue

        # Persist + detect transition for alerting.
        async with db_session() as s:
            row = await s.get(ThemeRotation, theme_id)
            was_flagged = bool(row.flagged) if row else False
            prior_ev = (row.evidence or {}) if row is not None else {}
            streak = int(prior_ev.get("candidate_streak") or 0) if isinstance(prior_ev, dict) else 0
            streak = streak + 1 if candidate else 0
            flagged = candidate and streak >= confirm_needed
            # Score reflects the 5-signal set now, not the old 3.
            score = round(len(tripped) / 6.0, 3)
            evidence["candidate_streak"] = streak
            evidence["confirm_needed"] = confirm_needed
            evidence["institutional_signals"] = institutional
            evidence["decision"] = {
                "candidate": candidate, "meets_count": meets_count,
                "has_institutional": bool(institutional), "flagged": flagged,
                "measured_price": measured_price, "measured_institutional": measured_inst,
            }
            if row is None:
                row = ThemeRotation(theme_id=theme_id)
                s.add(row)
            row.flagged = flagged
            row.score = score
            row.signals_tripped = tripped
            row.evidence = evidence
            row.computed_at = datetime.now(timezone.utc)

        if candidate and not flagged:
            logger.info(
                "rotation: %s is a CANDIDATE (%s) — awaiting confirmation "
                "(%d/%d consecutive sweeps); entries NOT halted yet",
                theme_id, ", ".join(tripped), streak, confirm_needed,
            )
        elif meets_count and not meets_kind:
            logger.info(
                "rotation: %s has price weakness (%s) but NO institutional "
                "signal — treating as a pullback, not a rotation",
                theme_id, ", ".join(tripped),
            )

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

    if summary["unmeasured"]:
        # A sweep that could not measure most of the universe is an outage, not
        # an all-clear. Say so loudly: the book is running WITHOUT rotation
        # protection until a real sweep lands.
        await alert(
            level="warning",
            title=f"Rotation sweep degraded — {summary['unmeasured']}/{summary['themes']} themes unmeasured",
            body=("No ETF/breadth/institutional data for these themes; their prior "
                  "state was left untouched rather than overwritten with a false "
                  "'not rotating'. Entries are un-gated for them until a real "
                  "sweep completes. Check the data layer / broker connection."),
        )
    logger.info("rotation sweep: %d themes, %d flagged, %d unmeasured",
                summary["themes"], summary["flagged"], summary["unmeasured"])
    return summary
