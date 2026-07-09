"""Manager conviction → entry sizing tilt.

Turns the named-investor smart-money read into a bounded multiplier on entry
size. This is the "align with the legends to stay winner" payoff — but kept
deliberately gentle and one-directional:

  * It only ever *boosts* size for names that tracked managers hold with
    cross-fund confirmation (2+ distinct managers). One fund alone is not
    confirmation and gets no boost.
  * It is **never a gate** — an untracked name returns factor 1.0, so the
    entry behaves exactly as it would without this layer. Consistent with the
    "favor attempting; the walking-limit + abandon is the protection, not a
    tight gate" policy.
  * Bounded by MANAGER_CONVICTION_MAX_FACTOR and re-capped against the
    absolute per-position dollar ceiling in the sizing function, so smart
    money can tilt allocation but never blow past risk limits.

Step = +0.10 of NAV-target per confirming manager beyond the first, e.g.
2 managers → 1.10×, 3 → 1.20×, 4+ → 1.30× (capped).
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from ..db import get_session as db_session
from .repo import HedgeFundRepo

logger = logging.getLogger("agentic_edge.hedge_funds")

_STEP_PER_MANAGER = 0.10


async def manager_conviction(symbol: str) -> tuple[float, dict[str, Any]]:
    """Return (sizing_factor, meta) for a ticker. factor is 1.0 (neutral)
    unless 2+ tracked managers hold the name."""
    settings = get_settings()
    if not settings.MANAGER_CONVICTION_ENABLED:
        return 1.0, {"enabled": False}

    try:
        async with db_session() as s:
            sm = await HedgeFundRepo(s).smart_money_for_symbol(ticker=symbol)
    except Exception as e:
        logger.debug("manager_conviction lookup failed for %s: %s", symbol, e)
        return 1.0, {"error": str(e)}

    managers = sm.get("managers", []) if sm.get("matched") else []
    tier1 = [m for m in managers if m.get("tier") == "tier1"]
    tier2 = [m for m in managers if m.get("tier") == "tier2"]
    # Activists (Elliott/Pershing) are an event watchlist, not holding
    # conviction — excluded from sizing entirely.
    n1, n2 = len(tier1), len(tier2)

    # tier1 drives conviction; tier2 only cross-confirms (counts only when at
    # least one tier1 also holds the name) at half weight. A single tier1 with
    # no confirmation gets no boost (one fund isn't confirmation).
    if n1 == 0:
        return 1.0, {"matched": bool(managers), "tier1": 0, "tier2": n2, "factor": 1.0,
                     "note": "no tier1 holder — tier2 cross-confirm only, no sizing boost"}

    boost = _STEP_PER_MANAGER * (n1 - 1) + (_STEP_PER_MANAGER / 2.0) * n2
    factor = round(min(1.0 + boost, settings.MANAGER_CONVICTION_MAX_FACTOR), 3)
    meta = {
        "matched": True,
        "tier1": n1, "tier2": n2,
        "confirmation": (n1 >= 2) or (n1 >= 1 and n2 >= 1),
        "aggregate_value_usd": sm.get("aggregate_value_usd"),
        "tier1_managers": [m.get("slug") for m in tier1],
        "tier2_managers": [m.get("slug") for m in tier2],
        "factor": factor,
    }
    return factor, meta


async def insider_buy_conviction(symbol: str) -> tuple[float, dict[str, Any]]:
    """Return (sizing_factor, meta) from clustered OPPORTUNISTIC insider buying.

    factor is 1.0 (neutral) unless 2+ officers/directors/10%-owners made
    open-market purchases in the last 30 days (a cluster), in which case it
    returns INSIDER_BUY_CONVICTION_FACTOR. Boost-only, never a gate — the same
    one-directional discipline as manager conviction."""
    settings = get_settings()
    if not settings.INSIDER_BUY_CONVICTION_ENABLED:
        return 1.0, {"enabled": False}
    try:
        from tradingagents.dataflows.providers.fmp import FmpProvider
        bp = await FmpProvider().get_insider_buy_pressure(
            symbol, usd_floor=settings.INSIDER_BUY_MIN_USD,
        )
    except Exception as e:
        logger.debug("insider_buy_conviction lookup failed for %s: %s", symbol, e)
        return 1.0, {"error": str(e)}

    if not bp.get("clustered"):
        return 1.0, {"clustered": False, "n_buyers_30d": bp.get("n_buyers_30d", 0), "factor": 1.0}
    factor = round(float(settings.INSIDER_BUY_CONVICTION_FACTOR), 3)
    return factor, {
        "clustered": True,
        "n_buyers_30d": bp.get("n_buyers_30d"),
        "buys_30d_usd": bp.get("buys_30d_usd"),
        "buyers_30d": bp.get("buyers_30d"),
        "factor": factor,
    }


async def recent_manager_selling(symbol: str) -> tuple[bool, dict[str, Any]]:
    """True when institutional money has RECENTLY moved against the name:

      * a tier-1/tier-2 tracked manager's latest 13F delta on the symbol is a
        ``trim`` or ``exit`` computed within the last 45 days, OR
      * a 13D/13G holder filed a ``reduce``/``exit`` on the symbol in the last
        14 days (the stake watch's abrupt-reduction signal).

    Consumed two ways, both conservative:
      * entry side — CAPS the conviction boost at 1.0 (holdings-based
        conviction is up to a quarter stale; a fresh cut outranks stale levels)
      * exit side — a confirmation-gated exit-pressure bump, exactly the
        notable-short pattern: it AMPLIFIES existing weakness, never stands
        alone, and never counts as an independent guardrail pillar.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from ..db import HedgeFundManager, PositionChange, StakeFiling

    sym = (symbol or "").upper()
    meta: dict[str, Any] = {"fund_cuts": [], "stake_reductions": []}
    hit = False
    try:
        now = datetime.now(timezone.utc)
        async with db_session() as s:
            cuts = (
                await s.execute(
                    select(PositionChange, HedgeFundManager.slug, HedgeFundManager.tier)
                    .join(HedgeFundManager, HedgeFundManager.id == PositionChange.manager_id)
                    .where(PositionChange.ticker == sym)
                    .where(PositionChange.change_type.in_(["trim", "exit"]))
                    .where(PositionChange.computed_at >= now - timedelta(days=45))
                    .where(HedgeFundManager.tier.in_(["tier1", "tier2"]))
                    .where(HedgeFundManager.macro_only.is_(False))
                )
            ).all()
            for c, slug, tier in cuts:
                meta["fund_cuts"].append({"manager": slug, "tier": tier,
                                          "change": c.change_type,
                                          "change_pct": c.change_pct})
            reductions = (
                await s.execute(
                    select(StakeFiling)
                    .where(StakeFiling.subject_ticker == sym)
                    .where(StakeFiling.change_type.in_(["reduce", "exit"]))
                    .where(StakeFiling.ingested_at >= now - timedelta(days=14))
                )
            ).scalars().all()
            for f in reductions:
                meta["stake_reductions"].append({
                    "form": f.form_type, "change": f.change_type,
                    "pct": f.percent_of_class, "prior_pct": f.prior_percent,
                })
        hit = bool(meta["fund_cuts"] or meta["stake_reductions"])
    except Exception as e:
        logger.debug("recent_manager_selling lookup failed for %s: %s", symbol, e)
    return hit, meta


async def accumulation_conviction(symbol: str) -> tuple[float, dict[str, Any]]:
    """Combine manager-13F conviction and clustered-insider-buy conviction into
    a single bounded sizing factor. Both are boost-only; the product is capped
    at MANAGER_CONVICTION_MAX_FACTOR so the two signals can reinforce but never
    blow past the allocation ceiling.

    Freshness override: 13F conviction reads HOLDINGS, which lag up to a
    quarter. If a tracked manager's latest delta on the name is a recent trim/
    exit (or a 13D/G holder just reduced), the boost is capped at neutral 1.0 —
    stale levels must not size UP a name fresh filings say funds are LEAVING.
    Never below 1.0: selling caps the boost, it does not become a penalty gate.
    """
    settings = get_settings()
    m_factor, m_meta = await manager_conviction(symbol)
    i_factor, i_meta = await insider_buy_conviction(symbol)
    combined = round(min(m_factor * i_factor, settings.MANAGER_CONVICTION_MAX_FACTOR), 3)

    selling, sell_meta = await recent_manager_selling(symbol)
    if selling and combined > 1.0:
        logger.info(
            "conviction: %s boost %.2f capped at 1.0 — recent institutional "
            "selling (%d fund cuts, %d stake reductions)",
            symbol, combined, len(sell_meta["fund_cuts"]), len(sell_meta["stake_reductions"]),
        )
        combined = 1.0

    # Surface manager_count for the entry-loop log line, plus both sub-signals.
    matched = bool(m_meta.get("matched")) or bool(i_meta.get("clustered"))
    return combined, {
        "matched": matched,
        "factor": combined,
        "manager_count": (m_meta.get("tier1", 0) or 0) + (m_meta.get("tier2", 0) or 0),
        "manager": m_meta,
        "insider_buy": i_meta,
        "recent_selling": sell_meta if selling else None,
    }
