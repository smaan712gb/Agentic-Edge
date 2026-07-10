"""Intraday Pulse — the opening-bell / mid-session market read, grounded.

Answers, from live data, the question a PM asks all day: what KIND of day is
this — institutional accumulation, rotation within the complex, distribution,
or consolidation? Everything is computed before any narrative is written:

  * live change vs prior close for every theme-universe symbol (one batch
    quote call), rolled up per theme
  * breadth (fraction of the universe up), average move, dispersion across
    themes (the rotation tell)
  * rotation flags + risk posture already computed by the system

The day-type classification is DETERMINISTIC (rules below); the language
model only narrates the numbers it is given — same grounding discipline as
the morning brief, same fallback if the model is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..db import Theme, ThemeRotation, ThemeSymbol, get_session as db_session

logger = logging.getLogger("agentic_edge.report")

_PULSE_TIMEOUT_S = 20.0
_CACHE_TTL_S = 180.0          # intraday — refresh every 3 minutes at most
_cache: Optional[tuple[float, dict[str, Any]]] = None

# Benchmarks quoted alongside the universe: the semis-rotation read is the
# universe's move RELATIVE to the broad tape, not its absolute move.
_BENCHMARKS = ("QQQ", "SPY", "SMH")

# Catalysts refresh slower than quotes — 15-minute cache, independent of the
# quote cache so a pulse refresh doesn't re-run the news fan-out every time.
_CATALYST_TTL_S = 900.0
_catalyst_cache: Optional[tuple[float, list[dict[str, Any]]]] = None


async def build_intraday_pulse(*, refresh: bool = False) -> dict[str, Any]:
    global _cache
    if not refresh and _cache is not None and (time.monotonic() - _cache[0]) < _CACHE_TTL_S:
        return _cache[1]

    now_et = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))

    # --- Universe + themes -------------------------------------------------
    async with db_session() as s:
        rows = (await s.execute(
            select(ThemeSymbol.theme_id, ThemeSymbol.symbol, Theme.name)
            .join(Theme, Theme.id == ThemeSymbol.theme_id)
        )).all()
        rot = (await s.execute(select(ThemeRotation))).scalars().all()
    theme_syms: dict[str, list[str]] = {}
    theme_names: dict[str, str] = {}
    for tid, sym, name in rows:
        if tid.startswith("smoke"):
            continue
        theme_syms.setdefault(tid, []).append((sym or "").upper())
        theme_names[tid] = name
    symbols = sorted({s for syms in theme_syms.values() for s in syms})
    flagged = {r.theme_id for r in rot if r.flagged}

    # --- Live quotes (batch), benchmarks included ---------------------------
    changes = await _batch_change_pct(symbols + list(_BENCHMARKS))
    bench = {b: changes.pop(b, None) for b in _BENCHMARKS}

    # --- Facts ---------------------------------------------------------------
    vals = [c for c in changes.values() if c is not None]
    n = len(vals)
    breadth_up = (sum(1 for c in vals if c > 0) / n) if n else 0.0
    avg_chg = (sum(vals) / n) if n else 0.0
    theme_chg: list[dict[str, Any]] = []
    for tid, syms in theme_syms.items():
        tvals = [changes[s] for s in syms if changes.get(s) is not None]
        if not tvals:
            continue
        theme_chg.append({
            "theme_id": tid,
            "theme": theme_names.get(tid, tid),
            "avg_change_pct": round(sum(tvals) / len(tvals), 2),
            "n": len(tvals),
            "rotation_flagged": tid in flagged,
        })
    theme_chg.sort(key=lambda r: r["avg_change_pct"], reverse=True)
    theme_avgs = [t["avg_change_pct"] for t in theme_chg]
    dispersion = (max(theme_avgs) - min(theme_avgs)) if len(theme_avgs) >= 2 else 0.0

    day_type, day_note = _classify_day(
        breadth_up=breadth_up, avg_chg=avg_chg, dispersion=dispersion,
    )
    rotation_dir, rotation_note = _classify_semis_rotation(
        universe_avg=avg_chg, breadth_up=breadth_up, dispersion=dispersion,
        qqq=bench.get("QQQ"), smh=bench.get("SMH"),
    )
    # 0-10 status: breadth and average move, clipped. Deterministic + auditable.
    status = round(max(0.0, min(10.0, 5.0 + breadth_up * 4.0 - (1 - breadth_up) * 4.0
                                 + max(-1.5, min(1.5, avg_chg)))), 1)

    catalysts = await _top_catalysts()

    facts = {
        "as_of_et": now_et.strftime("%H:%M ET"),
        "n_symbols_quoted": n,
        "breadth_up_pct": round(breadth_up * 100, 1),
        "avg_change_pct": round(avg_chg, 2),
        "theme_dispersion_pct": round(dispersion, 2),
        "benchmarks_pct": {b: bench.get(b) for b in _BENCHMARKS},
        "day_type": day_type,
        "day_note": day_note,
        "semis_rotation": rotation_dir,
        "semis_rotation_note": rotation_note,
        "status_0_10": status,
        "leaders": theme_chg[:3],
        "laggards": theme_chg[-3:][::-1] if len(theme_chg) > 3 else [],
        "themes_rotation_flagged": sorted(theme_names.get(t, t) for t in flagged),
        "catalysts": catalysts,
    }

    narrative, source = await _narrate(facts)
    pulse = {**facts, "narrative": narrative, "narrative_source": source,
             "themes": theme_chg,
             "generated_at": datetime.now(timezone.utc).isoformat()}
    _cache = (time.monotonic(), pulse)
    return pulse


def _classify_semis_rotation(
    *, universe_avg: float, breadth_up: float, dispersion: float,
    qqq: Optional[float], smh: Optional[float],
) -> tuple[str, str]:
    """Where is institutional money moving relative to the AI/semis complex?

      into_semis    — the universe is OUTPERFORMING the broad tape with real
                      breadth (money coming in)
      out_of_semis  — the universe is UNDERPERFORMING the broad tape by a
                      clear margin (money leaving — e.g. index green while
                      the complex is red)
      within_semis  — relative performance roughly flat but themes widely
                      dispersed (money shuffling between memory/equipment/
                      optical, not leaving)
      neutral       — no clear relative signal

    Computed vs QQQ (the tape most of this book trades against); SMH is
    reported as corroborating context.
    """
    if qqq is None:
        return "neutral", "benchmark quote unavailable"
    rel = universe_avg - qqq
    smh_note = f"; SMH {smh:+.1f}%" if smh is not None else ""
    if rel >= 0.5 and breadth_up >= 0.55:
        return "into_semis", (f"universe {universe_avg:+.1f}% vs QQQ {qqq:+.1f}% "
                              f"with {breadth_up*100:.0f}% breadth{smh_note}")
    if rel <= -0.75:
        return "out_of_semis", (f"universe {universe_avg:+.1f}% vs QQQ {qqq:+.1f}% — "
                                f"the complex lags the tape{smh_note}")
    if dispersion >= 2.5:
        return "within_semis", (f"themes spread {dispersion:.1f}% apart while the "
                                f"complex tracks the tape (QQQ {qqq:+.1f}%){smh_note}")
    return "neutral", f"universe tracking the tape (QQQ {qqq:+.1f}%){smh_note}"


async def _top_catalysts() -> list[dict[str, Any]]:
    """The 2-3 headlines that matter for an AI-infrastructure book today,
    each with why-it-matters and the bull/bear debate — extracted by the
    quick model from REAL headlines (general market news + universe stock
    news), never invented. Falls back to the raw top headlines."""
    global _catalyst_cache
    if _catalyst_cache is not None and (time.monotonic() - _catalyst_cache[0]) < _CATALYST_TTL_S:
        return _catalyst_cache[1]

    headlines: list[dict[str, str]] = []
    try:
        from tradingagents.dataflows.providers.fmp import FmpProvider
        fmp = FmpProvider()
        general = await fmp._http.get_json(
            "/stable/news/general-latest",
            params={"limit": 30, "apikey": fmp._api_key})
        stock = await fmp.get_stock_news("NVDA,MU,TSM,AVGO,AMD,ANET,VRT,CRDO", limit=30)
        for r in (general if isinstance(general, list) else []):
            t = str(r.get("title") or "")
            if t:
                headlines.append({"headline": t[:160],
                                  "published": str(r.get("publishedDate") or "")[:16]})
        for r in stock:
            t = str(r.get("headline") or "")
            if t:
                headlines.append({"headline": t[:160],
                                  "published": str(r.get("time") or "")[:16]})
    except Exception as e:
        logger.warning("pulse: catalyst news fetch failed: %s", e)
    # Dedupe, newest first, cap the prompt size.
    seen: set[str] = set()
    uniq = []
    for h in sorted(headlines, key=lambda x: x["published"], reverse=True):
        k = h["headline"].lower()[:80]
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    uniq = uniq[:40]
    if not uniq:
        _catalyst_cache = (time.monotonic(), [])
        return []

    catalysts: list[dict[str, Any]] = []
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if api_key:
        try:
            from openai import AsyncOpenAI
            from ..config import get_settings
            settings = get_settings()
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=(settings.DEEPSEEK_BASE_URL or "https://api.deepseek.com"))
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=settings.DEEPSEEK_QUICK_MODEL,
                    messages=[
                        {"role": "system", "content": (
                            "You are a hedge-fund morning-meeting analyst. From the "
                            "given headlines pick the 2-3 CATALYSTS that matter most "
                            "today for an AI-infrastructure/semiconductor portfolio. "
                            'STRICT JSON: {"catalysts": [{"title": str, '
                            '"why_it_matters": str (<=25 plain words), '
                            '"bull_case": str (<=20 words), "bear_case": str (<=20 words)}]}. '
                            "Base every field ONLY on the given headlines — no outside "
                            "facts, no invented numbers, no vendor/software names.")},
                        {"role": "user", "content": json.dumps(uniq)},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                ),
                timeout=_PULSE_TIMEOUT_S,
            )
            parsed = json.loads(resp.choices[0].message.content or "{}")
            got = parsed.get("catalysts")
            if isinstance(got, list):
                catalysts = [c for c in got if isinstance(c, dict) and c.get("title")][:3]
        except Exception as e:
            logger.warning("pulse: catalyst extraction failed: %s", e)
    if not catalysts:
        catalysts = [{"title": h["headline"], "why_it_matters": None,
                      "bull_case": None, "bear_case": None} for h in uniq[:3]]
    _catalyst_cache = (time.monotonic(), catalysts)
    return catalysts


def _classify_day(*, breadth_up: float, avg_chg: float, dispersion: float) -> tuple[str, str]:
    """Deterministic day-type call, IBD-style but computed:

      accumulation   — broad buying: ≥65% of the universe up, avg ≥ +0.4%
      distribution   — broad selling: ≤35% up, avg ≤ −0.4%
      rotation       — leadership shifting: overall roughly flat but themes
                       spread ≥ 2.5% apart (money moving WITHIN the complex)
      consolidation  — everything else (digesting, no strong signal)
    """
    if breadth_up >= 0.65 and avg_chg >= 0.4:
        return "accumulation", "broad institutional buying across the universe"
    if breadth_up <= 0.35 and avg_chg <= -0.4:
        return "distribution", "broad selling pressure across the universe"
    if dispersion >= 2.5 and abs(avg_chg) < 0.6:
        return "rotation", "money is moving between themes, not leaving the complex"
    return "consolidation", "digesting recent moves — no decisive institutional signal"


async def _batch_change_pct(symbols: list[str]) -> dict[str, Optional[float]]:
    """Live change-vs-prior-close for the universe in chunked batch calls."""
    out: dict[str, Optional[float]] = {s: None for s in symbols}
    try:
        from tradingagents.dataflows.providers.fmp import FmpProvider
        fmp = FmpProvider()

        async def _chunk(chunk: list[str]) -> None:
            # NOTE: /stable/quote silently returns [] for comma lists —
            # /stable/batch-quote (plural `symbols`) is the batch endpoint.
            body = await fmp._http.get_json(
                "/stable/batch-quote",
                params={"symbols": ",".join(chunk), "apikey": fmp._api_key},
            )
            if isinstance(body, list):
                for r in body:
                    sym = str(r.get("symbol") or "").upper()
                    ch = r.get("changePercentage", r.get("changesPercentage"))
                    try:
                        if sym in out and ch is not None:
                            out[sym] = float(ch)
                    except (TypeError, ValueError):
                        pass

        chunks = [symbols[i:i + 40] for i in range(0, len(symbols), 40)]
        await asyncio.gather(*[_chunk(c) for c in chunks])
    except Exception as e:
        logger.warning("pulse: batch quotes failed: %s", e)
    return out


_PULSE_SYS_PROMPT = (
    "You are the intraday voice of a portfolio management team. You get a JSON "
    "of COMPUTED market facts (breadth, average move, day type, leading and "
    "lagging themes, rotation flags). Write a short intraday read for the "
    "account owner (no finance background).\n"
    "Rules: ONLY facts in the JSON — never invent numbers, tickers, holdings, "
    "or probabilities. Never mention software or data vendors. Three sections "
    "with EXACTLY these headers:\n"
    "## What kind of day is this\n## Leadership\n## What would change the picture\n"
    "In the last section, state observable conditions from the given facts "
    "(e.g. breadth crossing thresholds, a lagging theme turning up) — not "
    "predictions. 130 words maximum."
)


async def _narrate(facts: dict[str, Any]) -> tuple[str, str]:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if api_key:
        try:
            from openai import AsyncOpenAI
            from ..config import get_settings
            settings = get_settings()
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=(settings.DEEPSEEK_BASE_URL or "https://api.deepseek.com"),
            )
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=settings.DEEPSEEK_QUICK_MODEL,
                    messages=[
                        {"role": "system", "content": _PULSE_SYS_PROMPT},
                        {"role": "user", "content": json.dumps(facts, default=str)},
                    ],
                    temperature=0.2,
                ),
                timeout=_PULSE_TIMEOUT_S,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return text, "agent"
        except Exception as e:
            logger.warning("pulse narrative failed (%s) — using fallback", e)
    return _fallback_narrative(facts), "fallback"


def _fallback_narrative(f: dict[str, Any]) -> str:
    lines = ["## What kind of day is this"]
    lines.append(
        f"As of {f['as_of_et']}, this looks like a {f['day_type']} day — {f['day_note']}. "
        f"{f['breadth_up_pct']:.0f}% of the {f['n_symbols_quoted']} names we track are up, "
        f"with an average move of {f['avg_change_pct']:+.1f}%."
    )
    rot = f.get("semis_rotation")
    if rot and rot != "neutral":
        label = {"into_semis": "money moving INTO the chip complex",
                 "out_of_semis": "money moving OUT of the chip complex",
                 "within_semis": "money shuffling WITHIN the chip complex"}.get(rot, rot)
        lines.append(f"Relative to the broad market: {label} ({f.get('semis_rotation_note')}).")
    lines.append("## Leadership")
    if f["leaders"]:
        lead = ", ".join(f"{t['theme']} ({t['avg_change_pct']:+.1f}%)" for t in f["leaders"])
        lines.append(f"Strongest areas right now: {lead}.")
    if f["laggards"]:
        lag = ", ".join(f"{t['theme']} ({t['avg_change_pct']:+.1f}%)" for t in f["laggards"])
        lines.append(f"Lagging: {lag}.")
    lines.append("## What would change the picture")
    lines.append(
        "Watch whether breadth holds above 65% (broad buying) or slips under 35% "
        "(broad selling), and whether any lagging area turns positive — that is "
        "how leadership rotation shows up."
    )
    return "\n".join(lines)
