"""Bearish institutional overlay — notable-short detection.

Reads NOTABLE SHORT-SELLERS' bearish positioning as CONTEXT — deliberately NOT a
standalone signal. A single bear (e.g. Burry short SOXX) is often early/wrong, so
this never gates an entry or triggers an exit on its own. It is (a) LOGGED on a
pullback-add so the operator sees the disagreement (never a veto), and (b) on the
exit side, only AMPLIFIES exit pressure when the name ALREADY shows other bearish
signals — confirmation, not a driver.

Three sources, because a short from today's news won't hit a 13F for ~45 days:

  1. NEWS (automated, same-day) — the news sweep tags stories carrying a named
     short-seller + short language as ``bear:notable_short:<name>``. This is the
     path that catches a headline short (e.g. Burry short NVDA/AMAT/SOXX) the day
     it prints, without anyone hand-editing the registry.
  2. ``NOTABLE_SHORTS`` — an operator/news-maintained registry of known current
     shorts (edit here, restart to apply). Belt-and-suspenders for (1), and the
     only path that fans an ETF short (e.g. SOXX) out across a theme cluster.
  3. 13F PUT positions held by ``tier="bear"`` managers (e.g. Scion Asset Mgmt),
     ingested automatically by the EDGAR poller into fund_holdings
     (put_call_flag='P'). This confirms/extends the above once filings land.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select

from datetime import datetime, timedelta, timezone

from ..config import get_settings
from ..db import FundHolding, HedgeFundManager, NewsMention, get_session as db_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Known-now shorts (news/operator-sourced; the 13F path confirms later).
# ``target`` is a ticker; kind "etf" means the short pressures every name in the
# listed ``themes`` (an ETF short is a bet against the whole basket).
# ---------------------------------------------------------------------------
NOTABLE_SHORTS: list[dict[str, Any]] = [
    {"manager": "Michael Burry — Scion", "target": "NVDA", "kind": "stock",
     "themes": ["custom-silicon-supply", "ai-interconnect", "on-device-ai"],
     "date": "2026-06-30", "source": "news",
     "note": "Burry short NVDA @ $198.09 — 'beginning of the end', SOX at 2000-like extension."},
    {"manager": "Michael Burry — Scion", "target": "AMAT", "kind": "stock",
     "themes": ["advanced-packaging", "ai-test-metrology"],
     "date": "2026-06-30", "source": "news",
     "note": "Burry short AMAT @ $729.40 (semi-cap equipment)."},
    {"manager": "Michael Burry — Scion", "target": "CAT", "kind": "stock",
     "themes": [],
     "date": "2026-06-30", "source": "news",
     "note": "Burry short Caterpillar (industrials/data-center-capex proxy) — flagged only if held."},
    {"manager": "Michael Burry — Scion", "target": "SOXX", "kind": "etf",
     "themes": ["ai-memory-wall", "custom-silicon-supply", "ai-interconnect",
                "optical-networking", "advanced-packaging", "ai-test-metrology",
                "silicon-photonics", "on-device-ai"],
     "date": "2026-06-30", "source": "news",
     "note": "Burry SOXX puts rolled Jan->Mar 2027, strikes low-mid $400s — pressures the whole semi complex."},
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

    # 3. Recent NEWS captures naming a notable bear shorting this name — the
    #    automated path that catches a headline short the day it prints, long
    #    before it reaches a 13F and without hand-editing the registry. The news
    #    sweep tags these ``bear:notable_short:<name>`` in chokepoint_hits.
    try:
        ttl = int(getattr(get_settings(), "NOTABLE_SHORT_NEWS_TTL_DAYS", 21))
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl)
        async with db_session() as s:
            rows = (await s.execute(
                select(NewsMention.headline, NewsMention.chokepoint_hits,
                       NewsMention.published_at, NewsMention.captured_at)
                .where(NewsMention.ticker == sym)
                .where(NewsMention.sentiment == "bearish")
                .order_by(NewsMention.captured_at.desc())
                .limit(25)
            )).all()
        for headline, chits, published, captured in rows:
            tags = chits or []
            if not any(str(t).startswith("bear:notable_short:") for t in tags):
                continue
            ref = captured or published
            if ref is not None:
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=timezone.utc)
                if ref < cutoff:
                    continue   # stale — a short thesis from months ago isn't live context
            who = next((str(t).split(":", 2)[-1] for t in tags
                        if str(t).startswith("bear:notable_short:")), "unknown")
            hits.append({"src": "news", "manager": who, "target": sym,
                         "headline": (headline or "")[:160]})
    except Exception as e:  # pragma: no cover
        logger.debug("notable_short news lookup failed for %s: %s", sym, e)

    return bool(hits), {"short": bool(hits), "hits": hits}


def notable_short_exit_delta(is_short: bool) -> float:
    """Exit-pressure BUMP when a notable bear is short the name — raises the
    score (tightens exits) but is bounded so it informs rather than force-dumps
    (the multi-signal guardrail + no-drawdown rule still apply)."""
    return float(get_settings().NOTABLE_SHORT_EXIT_DELTA) if is_short else 0.0
