"""Bearish institutional overlay — notable-short detection.

Symmetric to the bullish ``conviction`` layer: where that reads tracked long
managers' 13F holdings as buy-side conviction, this reads NOTABLE SHORT-SELLERS'
bearish positioning and turns it into a signal that (a) BLOCKS new pullback-adds
on a name a famous bear is short, and (b) BUMPS its exit pressure.

Two sources, because a short from today's news won't hit a 13F for ~45 days:

  1. ``NOTABLE_SHORTS`` — an operator/news-maintained registry of known current
     shorts (edit here, restart to apply). Handles ETF shorts (e.g. Burry short
     SOXX) that pressure a whole theme cluster, not just one ticker.
  2. 13F PUT positions held by ``tier="bear"`` managers (e.g. Scion Asset Mgmt),
     ingested automatically by the EDGAR poller into fund_holdings
     (put_call_flag='P'). This confirms/extends the registry once filings land.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select

from ..config import get_settings
from ..db import FundHolding, HedgeFundManager, get_session as db_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Known-now shorts (news/operator-sourced; the 13F path confirms later).
# ``target`` is a ticker; kind "etf" means the short pressures every name in the
# listed ``themes`` (an ETF short is a bet against the whole basket).
# ---------------------------------------------------------------------------
NOTABLE_SHORTS: list[dict[str, Any]] = [
    {"manager": "Michael Burry — Scion", "target": "AMAT", "kind": "stock",
     "themes": ["advanced-packaging", "ai-test-metrology"],
     "date": "2026-07-01", "source": "news",
     "note": "Burry disclosed short AMAT (semi-cap equipment)."},
    {"manager": "Michael Burry — Scion", "target": "SOXX", "kind": "etf",
     "themes": ["ai-memory-wall", "custom-silicon-supply", "ai-interconnect",
                "optical-networking", "advanced-packaging", "ai-test-metrology",
                "silicon-photonics", "on-device-ai"],
     "date": "2026-07-01", "source": "news",
     "note": "Burry short SOXX (semiconductor ETF) — pressures the whole semi complex."},
]


async def notable_short_pressure(symbol: str,
                                 themes: Optional[list[str]] = None) -> tuple[bool, dict[str, Any]]:
    """(is_short, meta) — is a notable bear short this name, directly or via an
    ETF short covering one of its themes, or via a bear-manager 13F put?

    ``themes`` = the theme_ids the symbol belongs to (for ETF-short mapping).
    Fail-open to (False, ...) so a lookup error never blocks the caller."""
    if not get_settings().NOTABLE_SHORT_TRACKING_ENABLED:
        return False, {"enabled": False}
    sym = (symbol or "").upper()
    theme_set = {t for t in (themes or []) if t}
    hits: list[dict[str, Any]] = []

    # 1. Registry — direct ticker short OR ETF short covering one of its themes.
    for e in NOTABLE_SHORTS:
        tgt = str(e.get("target", "")).upper()
        if tgt == sym:
            hits.append({"src": "registry", "manager": e["manager"], "target": tgt,
                         "kind": e.get("kind", "stock"), "note": e.get("note")})
        elif e.get("kind") == "etf":
            via = theme_set & set(e.get("themes", []))
            if via:
                hits.append({"src": "registry", "manager": e["manager"], "target": tgt,
                             "kind": "etf", "via_theme": sorted(via), "note": e.get("note")})

    # 2. 13F puts held by tier="bear" managers (automated, when filings land).
    try:
        async with db_session() as s:
            rows = (await s.execute(
                select(HedgeFundManager.name, FundHolding.value_usd, FundHolding.period_end)
                .join(HedgeFundManager, HedgeFundManager.id == FundHolding.manager_id)
                .where(HedgeFundManager.tier == "bear")
                .where(FundHolding.put_call_flag == "P")
                .where(FundHolding.ticker == sym)
            )).all()
        for name, val, period in rows:
            hits.append({"src": "13F_put", "manager": name, "target": sym,
                         "value_usd": val, "period_end": str(period)})
    except Exception as e:  # pragma: no cover
        logger.debug("notable_short 13F lookup failed for %s: %s", sym, e)

    return bool(hits), {"short": bool(hits), "hits": hits}


def notable_short_exit_delta(is_short: bool) -> float:
    """Exit-pressure BUMP when a notable bear is short the name — raises the
    score (tightens exits) but is bounded so it informs rather than force-dumps
    (the multi-signal guardrail + no-drawdown rule still apply)."""
    return float(get_settings().NOTABLE_SHORT_EXIT_DELTA) if is_short else 0.0
