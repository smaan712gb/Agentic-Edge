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
