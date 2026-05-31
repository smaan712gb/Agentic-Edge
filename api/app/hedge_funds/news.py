"""Phase 3 — chokepoint news layer (free, IB Gateway sourced).

Pipeline (sweep, same shape as the EDGAR sweep):
  watched symbols (theme universe + held portfolio)
    -> IBKR historical news per symbol (account's free news providers)
    -> chokepoint-keyword filter (only supply-chain-relevant items kept)
    -> DeepSeek sentiment/summary extraction (best-effort)
    -> news_mentions store (deduped) + alert (louder when smart money holds it)

Deliberately high-signal: we only store/alert articles that hit the chokepoint
dictionary, so this is a bottleneck radar on your names — not a news firehose.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from ..config import get_settings
from ..db import NewsMention, ThemeSymbol, get_session as db_session

logger = logging.getLogger("agentic_edge.hedge_funds")

# Chokepoint dictionary — the supply-chain bottlenecks the whole thesis turns
# on. Lowercased substring match against headline + body. Grouped only for
# readability; matched as a flat set.
CHOKEPOINT_KEYWORDS: list[str] = [
    # packaging / memory
    "cowos", "advanced packaging", "hybrid bonding", "hbm", "hbm3e", "hbm4",
    "nand", "enterprise ssd", "dram shortage", "memory shortage",
    # cooling / power / grid
    "liquid cooling", "immersion cooling", "direct-to-chip", "transformer shortage",
    "grid capacity", "switchgear", "interconnect queue", "power constraint",
    "megawatt", "substation",
    # optics / interconnect
    "silicon photonics", "co-packaged optics", "optical interconnect", "retimer",
    "800g", "1.6t",
    # nuclear / energy
    "small modular reactor", "smr", "power purchase agreement", "ppa", "uranium",
    # materials / substrates — spelled out to avoid substring noise ("gan"
    # in "began"/"Morgan"); "gallium nitride"/"gan power" are unambiguous.
    "gallium nitride", "gan power", "indium phosphide", "silicon carbide",
    "substrate shortage", "rare earth",
    # yield / test — "wafer yield" not bare "yield" (financial-yield noise)
    "metrology", "burn-in", "wafer yield", "yield rate",
    # scarcity signals — qualified so generic "allocation"/"capital allocation"
    # earnings boilerplate doesn't trip it
    "sold out", "on allocation", "supply allocation", "lead time",
    "capacity constraint", "fully booked", "supply constrained",
]

# Word-boundary matchers — \b stops "gan" matching inside "began"/"Morgan"
# while still catching standalone tokens and multi-word phrases.
_CHOKEPOINT_RE: list[tuple[str, "re.Pattern[str]"]] = [
    (kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE))
    for kw in CHOKEPOINT_KEYWORDS
]


def match_chokepoints(text: str) -> list[str]:
    """Return the chokepoint keywords present as whole words/phrases."""
    if not text:
        return []
    return [kw for kw, rx in _CHOKEPOINT_RE if rx.search(text)]


_NEWS_SYS_PROMPT = (
    "You are a hedge-fund analyst. Given a news headline and body about ONE "
    "ticker, respond with strict JSON: {\"sentiment\": \"bullish|bearish|neutral\", "
    "\"conviction\": 0.0-1.0, \"summary\": \"one sentence, <=200 chars, why it "
    "matters for the AI supply-chain thesis\"}. conviction = how materially this "
    "moves the thesis, not how confident you are."
)


async def extract_news_signal(headline: str, body: Optional[str], symbol: str) -> dict[str, Any]:
    """Best-effort DeepSeek sentiment/summary. Returns {} on any failure (the
    keyword hit alone is still a usable signal)."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return {}
    text = (headline or "")
    if body:
        text += "\n\n" + body[:6000]   # headline + lead; enough for sentiment
    try:
        from openai import AsyncOpenAI
        settings = get_settings()
        client = AsyncOpenAI(api_key=api_key,
                             base_url=(settings.DEEPSEEK_BASE_URL or "https://api.deepseek.com"))
        resp = await client.chat.completions.create(
            model=settings.DEEPSEEK_QUICK_MODEL,
            messages=[{"role": "system", "content": _NEWS_SYS_PROMPT},
                      {"role": "user", "content": f"Ticker: {symbol}\n\n{text}"}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        return {
            "sentiment": str(parsed.get("sentiment") or "").lower()[:8] or None,
            "conviction": float(parsed["conviction"]) if parsed.get("conviction") is not None else None,
            "summary": (parsed.get("summary") or "")[:400] or None,
        }
    except Exception as e:
        logger.debug("news LLM extraction failed for %s: %s", symbol, e)
        return {}


async def _watched_symbols(ib: Any) -> dict[str, Optional[str]]:
    """symbol -> a theme_id (first match, for tagging). Theme universe plus
    any held stock positions."""
    out: dict[str, Optional[str]] = {}
    async with db_session() as s:
        rows = (await s.execute(select(ThemeSymbol.symbol, ThemeSymbol.theme_id))).all()
    for sym, theme_id in rows:
        out.setdefault((sym or "").upper(), theme_id)
    # Held stock positions (so portfolio names not in a theme are still watched).
    try:
        for p in await ib.get_positions():
            if str(p.get("secType", "")).upper() == "STK":
                out.setdefault((p.get("symbol") or "").upper(), None)
    except Exception as e:
        logger.debug("news: positions fetch for watchlist failed: %s", e)
    out.pop("", None)
    return out


async def run_news_sweep(
    *, lookback_hours: int = 6, max_per_symbol: int = 3,
    max_symbols: int = 60, emit_alerts: bool = True,
) -> dict[str, Any]:
    """Sweep IB news for the watchlist, keep chokepoint-relevant items, tag
    sentiment, store + alert. Idempotent via the dedup unique key."""
    settings = get_settings()
    summary: dict[str, Any] = {"symbols": 0, "articles": 0, "hits": 0, "stored": 0, "alerts": 0,
                               "skipped_reason": None}
    if not getattr(settings, "NEWS_SWEEP_ENABLED", True):
        summary["skipped_reason"] = "NEWS_SWEEP_ENABLED=false"
        return summary

    try:
        from api.app.positions import _ibkr
        ib = await _ibkr()
    except Exception as e:
        summary["skipped_reason"] = f"ibkr unavailable: {e}"
        return summary

    watch = await _watched_symbols(ib)
    symbols = list(watch.keys())[:max_symbols]
    summary["symbols"] = len(symbols)

    for sym in symbols:
        try:
            articles = await ib.get_historical_news(
                sym, lookback_hours=lookback_hours, max_results=max_per_symbol, fetch_body=True,
            )
        except Exception as e:
            logger.debug("news: fetch failed for %s: %s", sym, e)
            continue
        summary["articles"] += len(articles)

        for art in articles:
            hits = match_chokepoints((art.get("headline") or "") + " " + (art.get("body") or ""))
            if not hits:
                continue
            summary["hits"] += 1
            provider = art.get("provider") or ""
            article_id = str(art.get("article_id") or "")

            # Dedup.
            async with db_session() as s:
                exists = (await s.execute(
                    select(NewsMention.id)
                    .where(NewsMention.source_type == "ib_news")
                    .where(NewsMention.provider == provider)
                    .where(NewsMention.article_id == article_id)
                    .where(NewsMention.ticker == sym)
                )).first()
            if exists:
                continue

            signal = await extract_news_signal(art.get("headline") or "", art.get("body"), sym)
            published = _parse_time(art.get("time"))

            async with db_session() as s:
                s.add(NewsMention(
                    source_type="ib_news", provider=provider, article_id=article_id,
                    ticker=sym, theme_id=watch.get(sym),
                    headline=(art.get("headline") or "")[:1000],
                    chokepoint_hits=hits,
                    sentiment=signal.get("sentiment"),
                    conviction=signal.get("conviction"),
                    summary=signal.get("summary"),
                    published_at=published,
                ))
            summary["stored"] += 1

            if emit_alerts:
                await _alert_mention(sym, art.get("headline") or "", hits, signal)
                summary["alerts"] += 1

    logger.info("news sweep: %d symbols, %d articles, %d chokepoint hits, %d stored",
                summary["symbols"], summary["articles"], summary["hits"], summary["stored"])
    return summary


def _parse_time(t: Any) -> Optional[datetime]:
    if isinstance(t, datetime):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return None


async def _alert_mention(symbol: str, headline: str, hits: list[str], signal: dict[str, Any]) -> None:
    from ..autotrade.alerts import alert
    # Louder when tracked managers hold the name — chokepoint news on a
    # smart-money-confirmed name is the highest-value signal.
    smart = ""
    try:
        from .repo import HedgeFundRepo
        async with db_session() as s:
            sm = await HedgeFundRepo(s).smart_money_for_symbol(ticker=symbol)
        if sm.get("manager_count"):
            smart = f" · held by {sm['manager_count']} tracked manager(s)"
    except Exception:
        pass
    sent = signal.get("sentiment")
    level = "warning" if sent in ("bullish", "bearish") else "info"
    await alert(
        level=level,
        title=f"Chokepoint news: {symbol} [{', '.join(hits[:3])}]{smart}",
        body=(f"{headline[:160]}"
              + (f" · {sent} ({signal.get('conviction')})" if sent else "")
              + (f" — {signal.get('summary')}" if signal.get("summary") else "")),
    )
