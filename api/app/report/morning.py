"""Morning Report assembly.

One function — ``build_morning_report()`` — reads the artifacts every
always-on sweep already persists and returns the daily brief:

  * top buy ideas        — latest completed run per theme, Buy decisions,
                           ranked by composite (the same candidate set the
                           entry loop consumes)
  * holdings             — every symbol the system manages, with the latest
                           Exit Pressure Score/band, freshest scorecard,
                           rotation state, smart-money overlap, and a
                           suggested action derived from the pressure band
  * institutional activity — fresh 13D/13G stake filings, 13F quarter-over-
                           quarter moves, insider + material-filing alerts
  * risk alerts          — entry breaker, flagged theme rotation, abrupt
                           institutional reductions on names we hold
  * theme health         — per-theme composite from its latest run

Every section is best-effort: a failure in one becomes an ``error`` field on
that section, never a 500 for the whole report. The report itself never
trades and is not read by any gate — it is the operator's brief, assembled
from the same rows the automation writes for its own audit trail.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..db import (
    AutoAction,
    EquitySnapshot,
    HedgeFundManager,
    NewsMention,
    Position,
    PositionChange,
    Run,
    StakeFiling,
    SymbolFeatureSnapshot,
    SystemState,
    Theme,
    ThemeRotation,
    ThemeSymbol,
    TickerScore,
    TradeIntent,
    get_session as db_session,
)

logger = logging.getLogger("agentic_edge.report")

# Intent states that mean "the system owns this position" — mirrors the
# maintenance loop's open-intent query so the report sees the same book.
_OPEN_INTENT_STATUSES = ("filled", "submitted")
_OPEN_POSITION_STATES = ("pmcc_full", "leap_pending", "leap_open", "leap_open_naked", "closing")

# Stock snapshot rows older than this are ignored: the positions table is
# upsert-on-read, so a closed position's last row lingers forever. 72h spans
# a weekend (Friday close → Monday pre-market) without resurrecting a book
# that was flattened weeks ago. Open intents need no freshness check — they
# are the system's live book of record, closed out by the maintenance loop.
_SNAPSHOT_FRESH_HOURS = 72

# Pressure band → operator-facing suggested action. The report never executes;
# these mirror the maintenance loop's own graded policy so the brief and the
# automation always tell the same story.
_BAND_ACTION = {
    "aggressive": "Exit",
    "trim_heavy": "Trim",
    "trim_light": "Trim light",
    "hold":       "Hold",
}

# In-process cache: the report reads ~10 tables; a 5-minute TTL keeps the
# dashboard snappy without a staleness risk (every input is itself on a
# 15-min-or-slower cadence). `refresh=1` bypasses.
_CACHE_TTL_S = 300.0
_cache: Optional[tuple[float, dict[str, Any]]] = None


async def build_morning_report(
    *, lookback_hours: int = 24, top_n: int = 10, refresh: bool = False,
) -> dict[str, Any]:
    global _cache
    if not refresh and _cache is not None and (time.monotonic() - _cache[0]) < _CACHE_TTL_S:
        return _cache[1]

    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))

    theme_names = await _section(_theme_names)
    if isinstance(theme_names, dict) and theme_names.get("error"):
        theme_names = {}

    book = await _section(_holding_symbols)
    if not isinstance(book, dict) or book.get("error"):
        book = {"managed": [], "off_universe": []}
    holdings_symbols = book["managed"]
    held = {h["symbol"] for h in holdings_symbols}

    rotation = await _section(_rotation_rows)
    flagged_themes = (
        {r["theme_id"] for r in rotation if r.get("flagged")}
        if isinstance(rotation, list) else set()
    )

    report: dict[str, Any] = {
        "as_of": now_et.date().isoformat(),
        "generated_at": now_utc.isoformat(),
        "account": await _section(_account_snapshot),
        "top_ideas": await _section(
            _top_ideas, lookback_hours=lookback_hours, top_n=top_n,
            theme_names=theme_names, held=held, flagged_themes=flagged_themes,
        ),
        "holdings": await _section(
            _holdings_detail, holdings=holdings_symbols,
            theme_names=theme_names, flagged_themes=flagged_themes,
        ),
        "off_universe_positions": book["off_universe"],
    }
    from .portfolio_risk import portfolio_risk
    report["portfolio_risk"] = await _section(
        portfolio_risk,
        holdings=report["holdings"] if isinstance(report["holdings"], list) else [],
        account=report["account"] if isinstance(report["account"], dict) else None,
    )
    report.update({
        "institutional_activity": await _section(_institutional_activity),
        "alerts": await _section(_risk_alerts, rotation=rotation, held=held),
        "theme_health": await _section(
            _theme_health, theme_names=theme_names, flagged_themes=flagged_themes,
        ),
    })
    report["posture"] = compute_posture(
        theme_health=report["theme_health"] if isinstance(report["theme_health"], list) else [],
        n_themes_flagged=len(flagged_themes),
        n_themes=max(len(theme_names), 1),
        breaker_tripped=bool(
            isinstance(report["alerts"], dict)
            and (report["alerts"].get("entry_breaker") or {}).get("tripped")
        ),
    )
    # Plain-English narrative LAST — it is written from the assembled report.
    from .brief import plain_english_brief
    report["brief"] = await _section(plain_english_brief, report=report)
    _cache = (time.monotonic(), report)
    return report


def compute_posture(
    *, theme_health: list[dict[str, Any]], n_themes_flagged: int,
    n_themes: int, breaker_tripped: bool,
) -> dict[str, Any]:
    """Risk-appetite gauge (0 = full defense, 100 = full risk-on), computed
    from the system's own signals — a fear/greed dial grounded in what the
    automation actually acts on, not sentiment surveys:

      * 40%  average theme health (composite of each theme's latest run)
      * 40%  rotation calm — the fraction of themes institutions are NOT
             rotating out of
      * 20%  buy breadth — the fraction of scored names earning a Buy

    A tripped entry breaker caps the dial at 20: whatever the signals say,
    the system itself is refusing new risk. Deterministic and auditable —
    the components ship with the score.
    """
    if theme_health:
        avg_health = sum(t.get("composite", 0.0) for t in theme_health) / len(theme_health)
        n_scored = sum(t.get("n_scored", 0) for t in theme_health)
        n_buys = sum(t.get("n_buys", 0) for t in theme_health)
    else:
        avg_health, n_scored, n_buys = 0.0, 0, 0
    health_pts = max(0.0, min(1.0, avg_health / 10.0)) * 100
    rotation_calm = max(0.0, 1.0 - (n_themes_flagged / n_themes)) * 100
    breadth = (n_buys / n_scored * 100) if n_scored else 0.0

    score = 0.4 * health_pts + 0.4 * rotation_calm + 0.2 * breadth
    capped = False
    if breaker_tripped and score > 20:
        score, capped = 20.0, True
    score = round(score, 1)

    if score < 25:
        label = "defensive"
    elif score < 45:
        label = "cautious"
    elif score < 65:
        label = "neutral"
    elif score < 80:
        label = "constructive"
    else:
        label = "risk-on"
    return {
        "score": score,
        "label": label,
        "components": {
            "theme_health": round(health_pts, 1),
            "rotation_calm": round(rotation_calm, 1),
            "buy_breadth": round(breadth, 1),
        },
        "breaker_capped": capped,
        "themes_flagged": n_themes_flagged,
        "themes_total": n_themes,
    }


async def _section(fn, **kwargs) -> Any:
    """Run one section builder; degrade to an error stub instead of failing
    the whole report."""
    try:
        return await fn(**kwargs)
    except Exception as e:  # pragma: no cover — defensive per-section guard
        logger.exception("morning report: section %s failed: %s", fn.__name__, e)
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


async def _theme_names() -> dict[str, str]:
    async with db_session() as s:
        rows = (await s.execute(select(Theme.id, Theme.name))).all()
    return {tid: name for tid, name in rows}


async def _account_snapshot() -> Optional[dict[str, Any]]:
    """Latest persisted equity snapshot — deliberately no live broker call so
    the report renders even when the brokerage session is down."""
    async with db_session() as s:
        snap = (
            await s.execute(
                select(EquitySnapshot).order_by(EquitySnapshot.date.desc()).limit(1)
            )
        ).scalars().first()
    if snap is None:
        return None
    equity = float(snap.equity or 0)
    cash = float(snap.cash) if snap.cash is not None else None
    return {
        "equity": equity,
        "cash": cash,
        "notional": float(snap.notional) if snap.notional is not None else None,
        "cash_pct": round(cash / equity * 100, 1) if cash is not None and equity else None,
        "as_of": snap.date.date().isoformat(),
    }


async def _latest_done_runs(max_age: timedelta) -> dict[str, str]:
    """theme_id -> run_id of the latest completed run within ``max_age``."""
    cutoff = datetime.now(timezone.utc) - max_age
    async with db_session() as s:
        rows = (
            await s.execute(
                select(Run.id, Run.theme_id)
                .where(Run.status == "done")
                .where(Run.finished_at >= cutoff)
                .order_by(Run.finished_at.desc())
            )
        ).all()
    latest: dict[str, str] = {}
    for run_id, theme_id in rows:
        latest.setdefault(theme_id, run_id)
    return latest


async def _top_ideas(
    *, lookback_hours: int, top_n: int, theme_names: dict[str, str],
    held: set[str], flagged_themes: set[str],
) -> list[dict[str, Any]]:
    """Buy decisions on the latest run per theme — the entry loop's candidate
    set, deduped to one row per symbol (highest composite wins)."""
    latest = await _latest_done_runs(timedelta(hours=lookback_hours))
    if not latest:
        return []
    run_to_theme = {rid: tid for tid, rid in latest.items()}
    async with db_session() as s:
        rows = (
            await s.execute(
                select(TickerScore)
                .where(TickerScore.run_id.in_(set(run_to_theme)))
                .where(TickerScore.decision == "Buy")
                .order_by(TickerScore.composite.desc())
            )
        ).scalars().all()

    ideas: dict[str, dict[str, Any]] = {}
    for ts in rows:
        if ts.symbol in ideas:
            continue
        theme_id = run_to_theme.get(ts.run_id, "")
        ideas[ts.symbol] = {
            "symbol": ts.symbol,
            "theme_id": theme_id,
            "theme": theme_names.get(theme_id, theme_id),
            "composite": ts.composite,
            "setup": ts.setup,
            "options": ts.options,
            "thesis_fit": ts.thesis_fit,
            "conviction": ts.conviction,
            "drivers": (ts.drivers or [])[:4],
            "risks": (ts.risks or [])[:3],
            "rationale": ts.rationale,
            "run_id": ts.run_id,
            "held": ts.symbol in held,
            "rotation_flagged": theme_id in flagged_themes,
        }
    picked = list(ideas.values())[:top_n]
    await _enrich_ideas(picked)
    return picked


# ---------------------------------------------------------------------------
# Idea enrichment — technical setup, street coverage, research, and the
# synthesized institutional read. Every field is best-effort; a provider
# outage degrades one field to null, never the idea or the report.
# ---------------------------------------------------------------------------

# Hard ceiling on the whole enrichment fan-out so a slow provider can never
# hang the report endpoint. The 5-minute report cache amortizes the cost.
_ENRICH_TIMEOUT_S = 25.0

# Options-flow tilt thresholds on the feature store's flow_imbalance
# ((call premium − put premium) / total, −1..+1).
_FLOW_TILT = 0.2


async def _enrich_ideas(ideas: list[dict[str, Any]]) -> None:
    if not ideas:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*[_enrich_one_idea(i) for i in ideas]),
            timeout=_ENRICH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("morning report: idea enrichment timed out at %.0fs", _ENRICH_TIMEOUT_S)


async def _enrich_one_idea(idea: dict[str, Any]) -> None:
    sym = idea["symbol"]
    technical, research, smart, analyst, price_structure = await asyncio.gather(
        _idea_technical(sym), _idea_research(sym),
        _idea_smart_money(sym), _idea_analyst(sym),
        _idea_price_structure(sym),
        return_exceptions=True,
    )
    idea["technical"] = technical if not isinstance(technical, BaseException) else None
    idea["research"] = research if not isinstance(research, BaseException) else []
    idea["smart_money"] = smart if not isinstance(smart, BaseException) else None
    idea["analyst"] = analyst if not isinstance(analyst, BaseException) else None
    ps = price_structure if not isinstance(price_structure, BaseException) else None
    idea["trend"] = (ps or {}).get("trend")
    idea["patterns"] = (ps or {}).get("patterns") or []
    idea["structure"] = (ps or {}).get("structure")
    idea["institutional_read"] = _institutional_read(idea)
    from .entry_score import compute_entry_score
    idea["entry"] = compute_entry_score(idea)


async def _idea_price_structure(sym: str) -> Optional[dict[str, Any]]:
    """One ~2y daily-bar fetch feeding all three price engines: the 8/21/50/
    100/200 EMA stack (Trend spec), the chart-pattern detectors, and market
    structure (HH/HL, accumulation/distribution days, sweeps, zones)."""
    try:
        from datetime import date
        from tradingagents.dataflows.fallback import get_stock_data_with_fallback
        df = await get_stock_data_with_fallback(
            sym, date.today() - timedelta(days=500), date.today(),
        )
        if df is None or "Close" not in df.columns:
            return None
        from ..research.patterns import detect_patterns, market_structure
        from ..research.trend import ema_stack
        closes = df["Close"].astype(float).tolist()
        highs = (df["High"] if "High" in df.columns else df["Close"]).astype(float).tolist()
        lows = (df["Low"] if "Low" in df.columns else df["Close"]).astype(float).tolist()
        volumes = (df["Volume"] if "Volume" in df.columns else df["Close"] * 0).astype(float).tolist()
        return {
            "trend": ema_stack(closes),
            "patterns": detect_patterns(highs=highs, lows=lows, closes=closes, volumes=volumes),
            "structure": market_structure(highs=highs, lows=lows, closes=closes, volumes=volumes),
        }
    except Exception as e:
        logger.debug("price structure failed for %s: %s", sym, e)
        return None


async def _idea_technical(sym: str) -> Optional[dict[str, Any]]:
    """Latest point-in-time quant features — the deterministic setup facts."""
    async with db_session() as s:
        snap = (
            await s.execute(
                select(SymbolFeatureSnapshot)
                .where(SymbolFeatureSnapshot.symbol == sym)
                .order_by(SymbolFeatureSnapshot.as_of.desc())
                .limit(1)
            )
        ).scalars().first()
    if snap is None:
        return None
    f = snap.features or {}
    gamma = f.get("gamma_sign_num")
    return {
        "momentum_20d": f.get("momentum_20d"),
        "momentum_60d": f.get("momentum_60d"),
        "dist_50dma": f.get("dist_50dma"),
        "rvol": f.get("rvol"),
        "flow_imbalance": f.get("flow_imbalance"),
        "gamma_sign": ("positive" if gamma == 1.0 else
                       "negative" if gamma == -1.0 else
                       "neutral" if gamma == 0.0 else None),
        "as_of": snap.as_of.isoformat() if snap.as_of else None,
    }


async def _idea_research(sym: str) -> list[dict[str, Any]]:
    """Recent sentiment-tagged coverage on the name (last 7 days)."""
    async with db_session() as s:
        rows = (
            await s.execute(
                select(NewsMention)
                .where(NewsMention.ticker == sym)
                .where(NewsMention.captured_at >= datetime.now(timezone.utc) - timedelta(days=7))
                .order_by(NewsMention.captured_at.desc())
                .limit(3)
            )
        ).scalars().all()
    return [
        {
            "headline": m.headline,
            "sentiment": m.sentiment,
            "conviction": m.conviction,
            "summary": m.summary,
            "published_at": m.published_at.isoformat() if m.published_at else None,
        }
        for m in rows
    ]


async def _idea_smart_money(sym: str) -> Optional[dict[str, Any]]:
    from ..hedge_funds.repo import HedgeFundRepo
    async with db_session() as s:
        smart = await HedgeFundRepo(s).smart_money_for_symbol(ticker=sym)
    if not smart or not smart.get("matched"):
        return None
    return {
        "manager_count": smart.get("manager_count", 0),
        "confirmation": smart.get("confirmation", False),
    }


async def _idea_analyst(sym: str) -> Optional[dict[str, Any]]:
    """Street coverage: consensus price target + upside, and the last 30 days
    of grade changes (firm, from → to)."""
    from tradingagents.dataflows.providers.fmp import FmpProvider
    fmp = FmpProvider()

    async def _consensus() -> dict[str, Any]:
        return await fmp.get_price_target_consensus(sym)

    async def _grades() -> list[dict[str, Any]]:
        return await fmp.get_analyst_grade_changes(sym)

    async def _price() -> Optional[float]:
        body = await fmp._http.get_json(
            "/stable/quote", params={"symbol": sym, "apikey": fmp._api_key})
        if isinstance(body, list) and body:
            try:
                return float(body[0].get("price"))
            except (TypeError, ValueError):
                return None
        return None

    consensus, grades, price = await asyncio.gather(
        _consensus(), _grades(), _price(), return_exceptions=True,
    )
    out: dict[str, Any] = {"pt_consensus": None, "pt_high": None, "pt_low": None,
                           "price": None, "upside_pct": None,
                           "grades_30d": [], "n_up_30d": 0, "n_down_30d": 0}
    if isinstance(consensus, dict) and consensus:
        for src, dst in (("targetConsensus", "pt_consensus"),
                         ("targetHigh", "pt_high"), ("targetLow", "pt_low")):
            try:
                out[dst] = float(consensus[src]) if consensus.get(src) is not None else None
            except (TypeError, ValueError):
                pass
    if isinstance(price, float):
        out["price"] = price
        if out["pt_consensus"] and price:
            out["upside_pct"] = round((out["pt_consensus"] / price - 1) * 100, 1)
    if isinstance(grades, list):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        for g in grades:
            if not isinstance(g, dict):
                continue
            date = str(g.get("date") or "")[:10]
            if date and date < cutoff:
                continue
            action = str(g.get("action") or "").lower()
            direction = ("up" if "up" in action else
                         "down" if "down" in action else "neutral")
            out["grades_30d"].append({
                "date": date or None,
                "firm": g.get("gradingCompany") or g.get("company"),
                "from": g.get("previousGrade"),
                "to": g.get("newGrade"),
                "direction": direction,
            })
            if direction == "up":
                out["n_up_30d"] += 1
            elif direction == "down":
                out["n_down_30d"] += 1
        out["grades_30d"] = out["grades_30d"][:5]
    if (out["pt_consensus"] is None and not out["grades_30d"] and out["price"] is None):
        return None
    return out


def _institutional_read(idea: dict[str, Any]) -> dict[str, Any]:
    """Deterministic synthesis: given the street, flow, holders, and coverage,
    are institutions likely to lean in or step back? Reasons are always
    listed so the operator can audit the verdict."""
    bull: list[str] = []
    bear: list[str] = []

    smart = idea.get("smart_money")
    if smart and smart.get("confirmation"):
        bull.append(f"{smart['manager_count']} tracked funds hold it (cross-fund confirmation)")

    analyst = idea.get("analyst")
    if analyst:
        up, down = analyst.get("n_up_30d", 0), analyst.get("n_down_30d", 0)
        if up > down and up > 0:
            bull.append(f"street leaning positive: {up} upgrade(s) vs {down} downgrade(s) in 30d")
        elif down > up and down > 0:
            bear.append(f"street cooling: {down} downgrade(s) vs {up} upgrade(s) in 30d")
        upside = analyst.get("upside_pct")
        if upside is not None:
            if upside >= 10:
                bull.append(f"consensus target {upside:+.0f}% above price")
            elif upside <= -5:
                bear.append(f"price already {abs(upside):.0f}% above consensus target")

    research = idea.get("research") or []
    n_bull = sum(1 for r in research if r.get("sentiment") == "bullish")
    n_bear = sum(1 for r in research if r.get("sentiment") == "bearish")
    if n_bull > n_bear and n_bull > 0:
        bull.append(f"recent coverage skews bullish ({n_bull} of {len(research)} pieces)")
    elif n_bear > n_bull and n_bear > 0:
        bear.append(f"recent coverage skews bearish ({n_bear} of {len(research)} pieces)")

    tech = idea.get("technical")
    flow = (tech or {}).get("flow_imbalance")
    if flow is not None:
        if flow >= _FLOW_TILT:
            bull.append("options flow tilts bullish (call premium dominating)")
        elif flow <= -_FLOW_TILT:
            bear.append("options flow tilts bearish (put premium dominating)")

    if idea.get("rotation_flagged"):
        bear.append("theme rotation flagged — institutions rotating out of the sector")

    if bull and not bear:
        label = "constructive"
    elif bear and not bull:
        label = "cautious"
    elif bull and bear:
        label = "mixed"
    else:
        label = "limited data"
    return {"label": label, "bull": bull, "bear": bear}


async def _holding_symbols() -> dict[str, list[dict[str, Any]]]:
    """The book, split in two:

    * ``managed``      — what THIS system owns or researches: open intents
                         (LEAPS/PMCC) plus account stock rows that belong to
                         the theme universe. This is the Portfolio review.
    * ``off_universe`` — account positions outside the theme universe (manual
                         trades / other strategies sharing the brokerage
                         account). Reported as a count-and-symbols footnote,
                         never reviewed: they carry no theme, no scorecard,
                         and no exit-pressure signal, so a review row would
                         be all dashes — noise, not information.
    """
    managed: dict[str, dict[str, Any]] = {}
    off_universe: list[dict[str, Any]] = []
    async with db_session() as s:
        universe = {
            (r[0] or "").upper()
            for r in (await s.execute(select(ThemeSymbol.symbol).distinct())).all()
        }
        intents = (
            await s.execute(
                select(TradeIntent)
                .where(TradeIntent.status.in_(_OPEN_INTENT_STATUSES))
                .where(TradeIntent.position_state.in_(_OPEN_POSITION_STATES))
                .order_by(TradeIntent.created_at.desc())
            )
        ).scalars().all()
        for i in intents:
            sym = (i.symbol or "").upper()
            if not sym or sym in managed:
                continue
            managed[sym] = {
                "symbol": sym,
                "structure": i.structure,
                "position_state": i.position_state,
                "qty": i.leap_qty if i.leap_qty is not None else i.qty,
                "intent_id": i.id,
            }
        fresh_cutoff = datetime.now(timezone.utc) - timedelta(hours=_SNAPSHOT_FRESH_HOURS)
        stocks = (
            await s.execute(
                select(Position)
                .where(Position.qty != 0)
                .where(Position.captured_at >= fresh_cutoff)
                .order_by(Position.captured_at.desc())
            )
        ).scalars().all()
        seen_off: set[str] = set()
        for p in stocks:
            sym = (p.symbol or "").upper()
            if not sym:
                continue
            if sym not in managed and sym not in universe:
                if sym not in seen_off:
                    seen_off.add(sym)
                    off_universe.append({"symbol": sym, "qty": p.qty, "pnl": p.pnl})
                continue
            row = managed.setdefault(sym, {"symbol": sym, "structure": "stock",
                                           "position_state": "open", "qty": p.qty,
                                           "intent_id": None})
            if "pnl" not in row:  # rows are newest-first; keep the freshest only
                row["pnl"] = p.pnl
                row["last_price"] = p.last_price
                row["avg_price"] = p.avg_price
    off_universe.sort(key=lambda r: r["symbol"])
    return {"managed": list(managed.values()), "off_universe": off_universe}


async def _holdings_detail(
    *, holdings: list[dict[str, Any]], theme_names: dict[str, str],
    flagged_themes: set[str],
) -> list[dict[str, Any]]:
    if not holdings:
        return []
    from ..hedge_funds.repo import HedgeFundRepo

    async def _one(row: dict[str, Any]) -> dict[str, Any]:
        sym = row["symbol"]
        detail = dict(row)

        async with db_session() as s:
            # Latest graded Exit Pressure row the maintenance loop recorded.
            pressure = (
                await s.execute(
                    select(AutoAction)
                    .where(AutoAction.symbol == sym)
                    .where(AutoAction.action_type.like("position_pressure_%"))
                    .order_by(AutoAction.timestamp.desc())
                    .limit(1)
                )
            ).scalars().first()
            # Freshest scorecard for the name, any theme.
            score = (
                await s.execute(
                    select(TickerScore, Run.theme_id)
                    .join(Run, Run.id == TickerScore.run_id)
                    .where(TickerScore.symbol == sym)
                    .order_by(TickerScore.created_at.desc())
                    .limit(1)
                )
            ).first()
            theme_ids = [
                r[0] for r in
                (await s.execute(select(ThemeSymbol.theme_id).where(ThemeSymbol.symbol == sym))).all()
            ]
            try:
                smart = await HedgeFundRepo(s).smart_money_for_symbol(ticker=sym)
            except Exception:
                smart = None

        payload = (pressure.payload or {}) if pressure is not None else {}
        band = payload.get("band")
        detail["exit_pressure"] = (
            {
                "score": payload.get("score"),
                "band": band,
                "rationale": payload.get("rationale"),
                "sub_scores": payload.get("sub_scores"),
                "theme": payload.get("theme"),
                "as_of": pressure.timestamp.isoformat() if pressure else None,
            }
            if pressure is not None else None
        )
        if score is not None:
            ts, theme_id = score
            detail["latest_score"] = {
                "composite": ts.composite,
                "decision": ts.decision,
                "conviction": ts.conviction,
                "theme_id": theme_id,
                "theme": theme_names.get(theme_id, theme_id),
                "as_of": ts.created_at.isoformat() if ts.created_at else None,
            }
        else:
            detail["latest_score"] = None
        detail["themes"] = [theme_names.get(t, t) for t in theme_ids]
        rotation_hit = sorted(set(theme_ids) & flagged_themes)
        detail["rotation_flagged"] = bool(rotation_hit)
        detail["rotation_themes"] = [theme_names.get(t, t) for t in rotation_hit]
        detail["smart_money"] = (
            {
                "manager_count": smart.get("manager_count", 0),
                "confirmation": smart.get("confirmation", False),
                "aggregate_value_usd": smart.get("aggregate_value_usd", 0),
            }
            if smart and smart.get("matched") else None
        )

        action = _BAND_ACTION.get(band or "hold", "Hold")
        if action == "Hold" and rotation_hit:
            action = "Hold — no adds (rotation)"
        detail["suggested_action"] = action
        return detail

    return list(await asyncio.gather(*[_one(r) for r in holdings]))


async def _institutional_activity() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    async with db_session() as s:
        stakes = (
            await s.execute(
                select(StakeFiling)
                .where(StakeFiling.ingested_at >= now - timedelta(hours=48))
                .order_by(StakeFiling.ingested_at.desc())
                .limit(15)
            )
        ).scalars().all()
        moves = (
            await s.execute(
                select(PositionChange, HedgeFundManager.name)
                .join(HedgeFundManager, HedgeFundManager.id == PositionChange.manager_id)
                .where(PositionChange.computed_at >= now - timedelta(hours=48))
                .where(PositionChange.change_type != "hold")
                .order_by(PositionChange.computed_at.desc())
                .limit(15)
            )
        ).all()
        insider = (
            await s.execute(
                select(AutoAction)
                .where(AutoAction.action_type.like("insider%"))
                .where(AutoAction.timestamp >= now - timedelta(hours=72))
                .order_by(AutoAction.timestamp.desc())
                .limit(10)
            )
        ).scalars().all()
        filings = (
            await s.execute(
                select(AutoAction)
                .where(AutoAction.action_type.like("filing_alert%"))
                .where(AutoAction.timestamp >= now - timedelta(hours=48))
                .order_by(AutoAction.timestamp.desc())
                .limit(10)
            )
        ).scalars().all()

    filing_rows = [_filing_row(a) for a in filings]
    await _refine_filing_impacts(filing_rows)

    return {
        "stake_filings": [
            {
                "ticker": f.subject_ticker,
                "form_type": f.form_type,
                "change_type": f.change_type,
                "percent_of_class": f.percent_of_class,
                "prior_percent": f.prior_percent,
                "is_activist": f.is_activist,
                "is_holding": f.is_holding,
                "filed_at": f.filed_at.isoformat() if f.filed_at else None,
            }
            for f in stakes
        ],
        "fund_moves": [
            {
                "manager": name,
                "ticker": c.ticker,
                "issuer_name": c.issuer_name,
                "change_type": c.change_type,
                "change_pct": c.change_pct,
            }
            for c, name in moves
        ],
        "insider_alerts": [_insider_row(a) for a in insider],
        "filing_alerts": filing_rows,
    }


# Plain-words meaning per SEC form family — matched by prefix, most specific
# first. The point is that a non-financial reader learns WHAT the filing is
# without opening it.
_FORM_MEANINGS: tuple[tuple[str, str], ...] = (
    ("NT 10", "late-filing notice — the company missed its report deadline (red flag)"),
    ("10-K", "annual report with audited financials"),
    ("10-Q", "quarterly financial report"),
    ("8-K", "material corporate event — something significant just happened"),
    ("6-K", "material event report (foreign-listed company)"),
    ("S-3", "shelf registration — the company may sell new shares later (possible dilution)"),
    ("S-4", "merger / share-exchange registration"),
    ("424", "share or note sale prospectus — new supply coming to market (dilution)"),
    ("425", "merger-related communication"),
    ("SC 13D", "activist investor disclosed a 5%+ stake and may push for change"),
    ("SC 13G", "large passive investor disclosed a 5%+ stake"),
    ("4", "insider trade report"),
)

# Deterministic impact lean per form family — used as-is when the language
# model is unavailable, refined by it when it is.
_FORM_IMPACT: tuple[tuple[str, str, str], ...] = (
    ("NT 10", "negative", "missed filing deadlines usually spook investors"),
    ("S-3", "negative", "possible share dilution weighs on price"),
    ("424", "negative", "new share supply tends to pressure price short-term"),
    ("SC 13D", "positive", "activist stakes often lift the stock on expectations of change"),
    ("SC 13G", "neutral", "passive stakes rarely move price by themselves"),
    ("S-4", "neutral", "depends on deal terms — read before judging"),
    ("425", "neutral", "depends on deal terms — read before judging"),
    ("8-K", "unclear", "impact depends on what the event is — see the note"),
    ("6-K", "unclear", "impact depends on what the event is — see the note"),
)


def _form_lookup(table: tuple, form_type: str) -> Optional[tuple]:
    ft = (form_type or "").upper().strip()
    for row in table:
        if ft.startswith(row[0]):
            return row
    return None


def _filing_row(a: AutoAction) -> dict[str, Any]:
    p = a.payload or {}
    form = str(p.get("form_type") or "")
    meaning_row = _form_lookup(_FORM_MEANINGS, form)
    impact_row = _form_lookup(_FORM_IMPACT, form)
    return {
        "symbol": a.symbol,
        "form_type": form or None,
        "severity": p.get("severity"),
        "rationale": p.get("rationale"),
        "link": p.get("final_link") or p.get("link"),
        "is_held": bool(p.get("is_held")),
        "meaning": meaning_row[1] if meaning_row else "SEC filing",
        "impact": impact_row[1] if impact_row else "unclear",
        "impact_note": impact_row[2] if impact_row else "not classified — open the filing",
        "timestamp": a.timestamp.isoformat(),
    }


def _insider_row(a: AutoAction) -> dict[str, Any]:
    p = a.payload or {}
    direction = str(p.get("direction") or ("buy" if "buy" in a.action_type else "sale"))
    who = p.get("reporting_name") or "an insider"
    usd = p.get("value_usd")
    headline = f"{who} {'bought' if direction == 'buy' else 'sold'}"
    if isinstance(usd, (int, float)) and usd:
        headline += f" about ${usd:,.0f} of stock"
    if direction == "buy":
        impact, note = "positive", "insiders buying with their own money is one of the strongest bullish tells"
    else:
        impact, note = "neutral", "many insider sales are routine (tax, diversification); clusters of sales are the worry"
    return {
        "symbol": a.symbol,
        "direction": direction,
        "headline": headline,
        "owner_type": p.get("owner_type"),
        "value_usd": usd,
        "filing_date": p.get("filing_date"),
        "impact": impact,
        "impact_note": note,
        "timestamp": a.timestamp.isoformat(),
    }


async def _refine_filing_impacts(rows: list[dict[str, Any]]) -> None:
    """Best-effort language-model pass: given each filing's form type and the
    watcher's rationale, refine impact to positive/negative/neutral with a
    plain one-liner. Deterministic defaults survive any failure."""
    if not rows:
        return
    try:
        from .brief import classify_filing_impacts
        verdicts = await classify_filing_impacts([
            {"symbol": r["symbol"], "form_type": r["form_type"],
             "rationale": r["rationale"], "is_held": r["is_held"]}
            for r in rows
        ])
        for r, v in zip(rows, verdicts):
            if isinstance(v, dict) and v.get("impact") in ("positive", "negative", "neutral"):
                r["impact"] = v["impact"]
                if v.get("note"):
                    r["impact_note"] = str(v["note"])[:200]
    except Exception as e:
        logger.debug("filing impact refinement skipped: %s", e)


async def _rotation_rows() -> list[dict[str, Any]]:
    async with db_session() as s:
        rows = (
            await s.execute(
                select(ThemeRotation).order_by(
                    ThemeRotation.flagged.desc(), ThemeRotation.score.desc())
            )
        ).scalars().all()
    return [
        {
            "theme_id": r.theme_id,
            "flagged": r.flagged,
            "score": r.score,
            "signals_tripped": r.signals_tripped or [],
            "computed_at": r.computed_at.isoformat() if r.computed_at else None,
        }
        for r in rows
    ]


async def _risk_alerts(*, rotation: Any, held: set[str]) -> dict[str, Any]:
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        reductions = (
            await s.execute(
                select(StakeFiling)
                .where(StakeFiling.is_holding.is_(True))
                .where(StakeFiling.change_type.in_(["reduce", "exit"]))
                .where(StakeFiling.ingested_at >= datetime.now(timezone.utc) - timedelta(days=7))
                .order_by(StakeFiling.ingested_at.desc())
                .limit(10)
            )
        ).scalars().all()

    flagged = [r for r in rotation if r.get("flagged")] if isinstance(rotation, list) else []
    return {
        "entry_breaker": {
            "tripped": bool(state and state.entry_breaker_tripped),
            "reason": state.entry_breaker_reason if state else None,
            "tripped_at": (
                state.entry_breaker_tripped_at.isoformat()
                if state and state.entry_breaker_tripped_at else None
            ),
        },
        "autotrade_enabled": bool(state and state.autotrade_enabled),
        "rotation_flagged": flagged,
        "holding_stake_reductions": [
            {
                "ticker": f.subject_ticker,
                "change_type": f.change_type,
                "percent_of_class": f.percent_of_class,
                "prior_percent": f.prior_percent,
                "filed_at": f.filed_at.isoformat() if f.filed_at else None,
                "held": (f.subject_ticker or "") in held,
            }
            for f in reductions
        ],
    }


async def _theme_health(
    *, theme_names: dict[str, str], flagged_themes: set[str],
) -> list[dict[str, Any]]:
    """Per-theme composite from its latest completed run (7-day window) —
    the same derivation theme_health.py uses for the deterioration signal."""
    latest = await _latest_done_runs(timedelta(days=7))
    if not latest:
        return []
    run_to_theme = {rid: tid for tid, rid in latest.items()}
    async with db_session() as s:
        rows = (
            await s.execute(
                select(TickerScore.run_id, TickerScore.composite, TickerScore.decision)
                .where(TickerScore.run_id.in_(set(run_to_theme)))
            )
        ).all()

    agg: dict[str, dict[str, Any]] = {}
    for run_id, composite, decision in rows:
        tid = run_to_theme[run_id]
        a = agg.setdefault(tid, {"sum": 0.0, "n": 0, "buys": 0})
        a["sum"] += float(composite or 0)
        a["n"] += 1
        if decision == "Buy":
            a["buys"] += 1

    out = []
    for tid, a in agg.items():
        avg = a["sum"] / a["n"] if a["n"] else 0.0
        out.append({
            "theme_id": tid,
            "theme": theme_names.get(tid, tid),
            "composite": round(avg, 2),
            "n_scored": a["n"],
            "n_buys": a["buys"],
            "rotation_flagged": tid in flagged_themes,
        })
    out.sort(key=lambda r: r["composite"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Daily dispatch — the 08:45 ET brief pushed through the alert fan-out
# ---------------------------------------------------------------------------


def summarise_for_alert(report: dict[str, Any]) -> str:
    """Compact plain-text digest for the Slack/log alert channel."""
    lines: list[str] = []

    ideas = report.get("top_ideas")
    if isinstance(ideas, list) and ideas:
        tops = ", ".join(
            f"{i['symbol']} {i['composite']:.1f}" for i in ideas[:5]
        )
        lines.append(f"Top ideas: {tops}")

    holdings = report.get("holdings")
    if isinstance(holdings, list):
        actions = [
            f"{h['symbol']} → {h['suggested_action']}"
            for h in holdings
            if h.get("suggested_action") and not str(h["suggested_action"]).startswith("Hold")
        ]
        if actions:
            lines.append("Suggested actions: " + "; ".join(actions))

    alerts = report.get("alerts")
    if isinstance(alerts, dict):
        if alerts.get("entry_breaker", {}).get("tripped"):
            lines.append("⚠ Entry breaker TRIPPED — new entries halted")
        flagged = alerts.get("rotation_flagged") or []
        if flagged:
            lines.append("Rotation flagged: " + ", ".join(r["theme_id"] for r in flagged))
        reductions = alerts.get("holding_stake_reductions") or []
        if reductions:
            lines.append(
                "Stake reductions on holdings: "
                + ", ".join(f"{r['ticker']} ({r['change_type']})" for r in reductions[:5])
            )

    return "\n".join(lines) if lines else "No actionable items this morning."


async def dispatch_morning_report() -> dict[str, Any]:
    """Build fresh and push the digest through the alert fan-out. Called by
    the 08:45 ET cron; safe to call ad hoc."""
    report = await build_morning_report(refresh=True)
    from ..autotrade.alerts import alert
    await alert(
        level="info",
        title=f"Morning report — {report['as_of']}",
        body=summarise_for_alert(report),
    )
    return report
