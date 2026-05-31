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

    n = int(sm.get("manager_count") or 0)
    if not sm.get("matched") or n < 2:
        # No cross-fund confirmation → neutral. Single-fund holds get no boost.
        return 1.0, {"matched": bool(sm.get("matched")), "manager_count": n, "factor": 1.0}

    boost = _STEP_PER_MANAGER * min(n - 1, 3)
    factor = round(min(1.0 + boost, settings.MANAGER_CONVICTION_MAX_FACTOR), 3)
    meta = {
        "matched": True,
        "manager_count": n,
        "confirmation": bool(sm.get("confirmation")),
        "aggregate_value_usd": sm.get("aggregate_value_usd"),
        "managers": [m.get("slug") for m in sm.get("managers", [])],
        "factor": factor,
    }
    return factor, meta
