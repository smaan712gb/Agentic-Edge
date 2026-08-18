"""The daily portfolio decision — one instruction, taken on closing data.

Runs once after the close and produces a single instruction for the whole
book: add risk, hold, stop adding, or trim one step. Everything the loops do
intraday is then execution of that decision rather than a fresh opinion.

Deliberately once a day. Repeatedly re-deciding strategic exposure intraday is
how a fund becomes a day trader: the tape gate halting all buying on
2026-08-18 was an intraday read (-4.8% breadth) overriding a weekly picture
that said mid-range, neither extended nor washed out. Intraday information
should inform the NEXT scheduled decision, not pre-empt it.

The instruction is persisted as an ``auto_actions`` row so the loops read a
stored decision all day rather than recomputing one — the same pattern as the
morning brief, and for the same reason: the inputs make provider calls, and a
decision that changes under the loop's feet is not a decision.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

from ..config import get_settings
from ..db import AutoAction, get_session as db_session

logger = logging.getLogger("agentic_edge.portfolio")

DECISION_ACTION_TYPE = "portfolio_decision"


async def load_todays_decision() -> Optional[dict[str, Any]]:
    """Most recent persisted decision, or None if absent or stale.

    Stale is treated as absent for the same reason the rotation flags are: a
    decision taken against a market that has moved on is wrong evidence, not
    weak evidence. Without a current decision the loops fall back to their
    existing per-name behaviour rather than acting on yesterday's exposure
    target.
    """
    s = get_settings()
    if not getattr(s, "PORTFOLIO_LAYER_ENABLED", True):
        return None
    try:
        max_age_h = float(getattr(s, "PORTFOLIO_DECISION_MAX_AGE_HOURS", 30.0))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_h)
        async with db_session() as sess:
            row = (await sess.execute(
                select(AutoAction.payload, AutoAction.timestamp)
                .where(AutoAction.action_type == DECISION_ACTION_TYPE)
                .order_by(AutoAction.timestamp.desc()).limit(1)
            )).first()
        if row is None or not isinstance(row[0], dict):
            return None
        ts = row[1]
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                logger.warning(
                    "portfolio: decision is STALE (%.1fh > %.0fh) — ignoring",
                    (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0, max_age_h)
                return None
        return row[0]
    except Exception as e:  # noqa: BLE001 — never break a loop on this
        logger.debug("portfolio: decision load failed: %s", e)
        return None


# Weekly bars below which there is no decision to make. A 20-week average, a
# 14-period ATR and a pivot structure all need history; under this the gates
# are not being cautious, they are reading noise.
MIN_WEEKS_FOR_DECISION = 30

INSTRUCTION_NO_DECISION = "no_decision"


def evidence_missing(idx: Any) -> list[str]:
    """Reasons the index cannot support a decision at all. Pure.

    The distinction this draws is between weak evidence and absent evidence,
    and it is the difference between a defensive posture and a destructive one.
    Every score in the framework treats an uncomputable input as zero — correct
    when one input is missing, catastrophic when they all are, because a total
    data outage then scores regime 0/4, exhaustion 0 and selling-exhaustion 0,
    which classifies as ``exhaustion_rotation`` with a 60-75% band and returns
    REDUCE for any book above 75%.

    That is not hypothetical: FMP began returning 401 mid-session on
    2026-08-18, and an empty IndexState was verified to produce exactly that
    instruction. A revoked API key would have told the fund to sell.

    'No data' must therefore be its own answer, distinct from 'the evidence
    says be defensive'.
    """
    missing: list[str] = []
    if idx is None:
        return ["no index"]
    if not getattr(idx, "weekly", None):
        missing.append("no weekly series")
    elif len(idx.weekly) < MIN_WEEKS_FOR_DECISION:
        missing.append(f"only {len(idx.weekly)} weekly bars "
                       f"(need {MIN_WEEKS_FOR_DECISION})")
    if getattr(idx, "close", None) is None:
        missing.append("no index close")
    if not getattr(idx, "weekly_atr", None):
        missing.append("no weekly ATR")
    if not getattr(idx, "n_constituents", 0):
        missing.append("no constituents returned bars")
    return missing


def evaluate_index(
    idx: Any, *, exposure_pct: float, previous_state: Optional[str] = None,
) -> dict[str, Any]:
    """Weekly evidence to one instruction. Pure, given an ``IndexState``.

    Split out from ``compute_daily_decision`` so that the backtest evaluates
    THIS function rather than a reimplementation of it. A harness that scores a
    parallel copy of the rules measures the copy: it can pass while production
    fails, and every threshold it endorses is endorsed for code that is not the
    code being run. Point-in-time replay therefore truncates an IndexState and
    calls straight into here.

    Everything the gates need is read off the IndexState's own series, so an
    IndexState carrying only bars up to week *t* yields exactly the decision the
    system would have taken in that week — no lookahead is possible, because
    there is nothing later in the object to look at.
    """
    from .basket_index import sma
    from .gates import (
        accumulation_gate, exhaustion_score, selling_exhaustion_score, trim_gate,
        weekly_regime_score,
    )
    from .state import classify_state, resolve

    # Absent evidence is not defensive evidence. Returning an instruction here
    # would be an opinion manufactured from nothing, and the state machine's
    # worst case is that it manufactures a REDUCE.
    gaps = evidence_missing(idx)
    if gaps:
        return {
            "instruction": INSTRUCTION_NO_DECISION,
            "state": "unknown",
            "target_band": [None, None],
            "degraded": gaps,
            "index": (idx.to_dict() if hasattr(idx, "to_dict") else {}),
            "regime": {"score": None, "max_score": 4, "components": {}, "unknown": gaps},
            "exhaustion": {"score": None, "conditions": {}},
            "selling_exhaustion": {"score": None, "conditions": {}},
            "accumulation_gate": {"action": "hold", "blocked_by": gaps},
            "trim_gate": {"action": "hold", "blocked_by": gaps},
            "portfolio_state": {"state": "unknown", "action": INSTRUCTION_NO_DECISION,
                                "current_exposure": round(exposure_pct, 4),
                                "reasons": ["index unavailable — no decision taken"]},
            "confluence_levels": [],
        }

    wk = idx.weekly

    ma20 = sma(wk, 20)
    ma20_prev = sma(wk[:-4], 20) if len(wk) > 24 else None
    rising = (ma20 > ma20_prev) if (ma20 and ma20_prev) else None
    rs = idx.rs_vs_benchmark()
    bh = idx.breadth_history

    regime = weekly_regime_score(
        close=idx.close, ma_w20=ma20, ma_w20_rising=rising,
        rs_vs_benchmark=rs, breadth_above_w20=idx.breadth_above_w20,
        structure_intact=idx.structure_intact())

    # Down weeks on above-average volume in the recent window.
    avg_v = (sum(b.v for b in wk[-26:]) / len(wk[-26:])) if len(wk) >= 26 else None
    distribution = sum(1 for b in wk[-6:]
                       if b.c < b.o and (avg_v is None or b.v > avg_v))

    eq_lagging_cap = None
    if len(idx.cap_weighted_weekly) > 13 and len(wk) > 13:
        eq_ret = wk[-1].c / wk[-14].c - 1 if wk[-14].c else None
        cap_ret = (idx.cap_weighted_weekly[-1].c / idx.cap_weighted_weekly[-14].c - 1
                   if idx.cap_weighted_weekly[-14].c else None)
        if eq_ret is not None and cap_ret is not None:
            eq_lagging_cap = eq_ret < cap_ret

    exhaustion = exhaustion_score(
        extension_atr=idx.extension_atr(), breadth_divergence=idx.breadth_divergence(),
        rs_vs_benchmark=rs, rs_was_positive=_rs_was_positive(idx),
        distribution_weeks=distribution, equal_lagging_cap=eq_lagging_cap)

    selling = selling_exhaustion_score(
        breadth_washout=(idx.breadth_above_w20 is not None
                         and idx.breadth_above_w20 < 0.20),
        down_volume_spike_fading=_volume_fading(wk),
        correlation_spike=None,
        declines_shrinking=_declines_shrinking(wk),
        stopped_making_lows=(len(wk) > 2 and wk[-1].l > wk[-2].l))

    conf, conf_hits = idx.confluence()
    breadth_stopped = bool(len(bh) > 1 and bh[-1] >= bh[-2])

    accum = accumulation_gate(
        theme_score_positive=True, regime=regime, confluence=conf,
        correction_atr=idx.correction_atr(),
        breadth_deterioration_stopped=breadth_stopped, selling_exhaustion=selling)
    trim = trim_gate(
        confluence_at_resistance=conf, extension_atr=idx.extension_atr(),
        exhaustion=exhaustion, deterioration_persists=bool(rs is not None and rs < 0))

    target_state, why = classify_state(
        theme_broken=False,      # structural break is an operator call, not a gate
        regime_score=regime.score, exhaustion=exhaustion.score,
        selling_exhaustion=selling.score,
        accumulation_ready=(accum.action == "accumulate"),
        trim_ready=(trim.action == "trim"))
    ps = resolve(current_exposure=exposure_pct, target_state=target_state,
                 previous_state=previous_state)
    ps.reasons.extend(why)

    return {
        "instruction": ps.action,
        "state": ps.state,
        "target_band": [ps.target_low, ps.target_high],
        "index": idx.to_dict(),
        "regime": regime.to_dict(),
        "exhaustion": exhaustion.to_dict(),
        "selling_exhaustion": selling.to_dict(),
        "accumulation_gate": accum.to_dict(),
        "trim_gate": trim.to_dict(),
        "portfolio_state": ps.to_dict(),
        "confluence_levels": conf_hits,
    }


async def compute_daily_decision(ib: Any) -> dict[str, Any]:
    """Build the whole picture and resolve it to one instruction."""
    from .basket_index import build_basket_index
    from .exposure import compute_book_exposure

    s = get_settings()
    idx = await build_basket_index(
        lookback_days=int(getattr(s, "PORTFOLIO_INDEX_LOOKBACK_DAYS", 1100)))
    book = await compute_book_exposure(ib)

    prev = await load_todays_decision()
    d = evaluate_index(idx, exposure_pct=book.exposure_pct,
                       previous_state=(prev or {}).get("state"))
    d["as_of"] = datetime.now(timezone.utc).isoformat()
    d["exposure"] = {
        "delta_adjusted_pct": round(book.exposure_pct, 4),
        "premium_pct": round(book.premium_pct, 4),
        "leverage": round(book.leverage, 3),
        "nav": round(book.nav, 2),
        "notional": round(book.notional, 2),
        "sleeves": book.sleeve_pct(),
        "degraded": book.degraded,
    }
    return d


def _rs_was_positive(idx: Any) -> Optional[bool]:
    """Was relative strength positive a quarter ago? Distinguishes a complex
    that has STOPPED leading from one that never led."""
    try:
        if len(idx.weekly) < 27 or len(idx.benchmark_weekly) < 27:
            return None
        a = idx.weekly[-14].c / idx.weekly[-27].c - 1
        b = idx.benchmark_weekly[-14].c / idx.benchmark_weekly[-27].c - 1
        return (a - b) > 0
    except Exception:  # noqa: BLE001
        return None


def _volume_fading(wk: list) -> Optional[bool]:
    """Down-volume spiked and is now contracting — the urgent selling is done."""
    if len(wk) < 4:
        return None
    downs = [b for b in wk[-4:] if b.c < b.o]
    if len(downs) < 2:
        return None
    return downs[-1].v < downs[0].v


def _declines_shrinking(wk: list) -> Optional[bool]:
    """Successive down weeks getting smaller — supply being absorbed."""
    downs = [b for b in wk[-6:] if b.c < b.o]
    if len(downs) < 2:
        return None
    first = abs(downs[0].c / downs[0].o - 1) if downs[0].o else 0
    last = abs(downs[-1].c / downs[-1].o - 1) if downs[-1].o else 0
    return last < first


async def dispatch_daily_decision(ib: Any) -> dict[str, Any]:
    """Compute, persist and alert. Called by the post-close cron."""
    from ..autotrade.alerts import alert
    from ..autotrade.auto_gate import AutoGateResult, record_auto_action

    d = await compute_daily_decision(ib)
    async with db_session() as s:
        await record_auto_action(
            s, loop="portfolio", action_type=DECISION_ACTION_TYPE,
            gate_result=AutoGateResult(passed=True, failures=[]),
            payload=d, outcome=d["instruction"])

    idx, exp = d["index"], d["exposure"]

    # A decision that could not be taken is an incident, not a decision. It is
    # alerted at critical because the underlying cause is always a broken data
    # path, and the fund spends the next day with no portfolio-level opinion.
    if d["instruction"] == INSTRUCTION_NO_DECISION:
        gaps = ", ".join(d.get("degraded") or []) or "unknown"
        await alert(
            level="critical",
            title="Portfolio decision UNAVAILABLE — index could not be built",
            body=(f"No portfolio-level instruction today: {gaps}. The loops fall "
                  f"back to per-name behaviour; no exposure target is in force. "
                  f"Check the market-data provider."))
        logger.error("portfolio decision UNAVAILABLE — %s", gaps)
        return d

    await alert(
        level="info",
        title=f"Portfolio decision — {d['instruction'].upper()} ({d['state']})",
        body=(f"exposure {exp['delta_adjusted_pct']:.0%} (target "
              f"{d['target_band'][0]:.0%}-{d['target_band'][1]:.0%}), "
              f"leverage {exp['leverage']}x. Regime {d['regime']['score']}/4, "
              f"exhaustion {d['exhaustion']['score']}, selling-exh "
              f"{d['selling_exhaustion']['score']}. Index ext "
              f"{idx['extension_atr']} ATR, correction {idx['correction_atr']} ATR, "
              f"confluence {idx['confluence']}."))
    logger.info("portfolio decision: %s (%s) exposure=%.1f%% target=%.0f-%.0f%%",
                d["instruction"], d["state"], exp["delta_adjusted_pct"] * 100,
                d["target_band"][0] * 100, d["target_band"][1] * 100)
    return d
