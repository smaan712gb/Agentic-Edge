"""Wiring the morning brief into the deciding agent.

The Morning Report was designed for a human: read it at 08:45 ET, place the
trades. Now that entries are automated, its judgment has to reach the entry
loop or it is a newsletter the system ignores.

Only TWO of its outputs are wired, and the omission of the rest is deliberate.
``top_ideas``, ``holdings``, rotation state and smart-money overlap are all
assembled from rows the loops already consume — ``build_morning_report`` says
so explicitly ("the same candidate set the entry loop consumes"). Feeding
those back would double-count the same evidence under a second name. What the
brief adds that has no other source in the trading path is:

  * **posture** — a 0-100 risk-appetite dial computed from the system's OWN
    signals (theme health, rotation calm, buy breadth), capped at 20 when the
    entry breaker is latched. The entry loop reads macro (VIX/SPX) and the
    intraday pulse, but nothing that measures the health of its own signal set.

  * **the per-idea street read** — analyst consensus upside, 30-day grade
    momentum, and a deterministic institutional lean. No analyst data reaches
    the trading path at all otherwise.

Both are bounded SIZING TILTS, never gates, consistent with the standing policy
that eligibility stays loose and the walking limit protects the price.

The brief makes provider calls, so it is built ONCE at 08:45 and persisted;
the loops read that stored row all day rather than rebuilding it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

from ..config import get_settings
from ..db import AutoAction, get_session as db_session

logger = logging.getLogger("agentic_edge.report")

BRIEF_ACTION_TYPE = "morning_brief_signals"


def posture_sizing_factor(posture_score: Optional[float], max_tilt: float) -> float:
    """Map the 0-100 posture dial to a bounded sizing multiplier. Pure.

    50 (neutral) -> 1.0; 100 (risk-on) -> 1 + max_tilt; 0 (full defense) ->
    1 - max_tilt. Symmetric and clamped non-negative: posture is a conviction
    dial, not a kill switch. The breaker and the macro regime are what stop
    trading — and since posture is itself capped at 20 whenever the breaker is
    latched, a latched breaker already drags this toward its floor.
    """
    if posture_score is None:
        return 1.0
    try:
        score = float(posture_score)
    except (TypeError, ValueError):
        return 1.0
    score = max(0.0, min(100.0, score))
    return round(max(0.0, 1.0 + (score - 50.0) / 50.0 * float(max_tilt)), 3)


def idea_sizing_factor(idea: Optional[dict[str, Any]], max_tilt: float) -> float:
    """Bounded per-symbol tilt from the brief's street + institutional read.

    Pure. Blends three independent reads, each normalised to [-1, 1], then
    averages over however many are actually PRESENT — so a name carrying only
    analyst coverage is not penalised for lacking an institutional verdict:

      * upside — consensus price target vs spot; +/-50% saturates.
      * grades — the 30-day upgrade/downgrade balance.
      * lean   — the report's deterministic institutional verdict.

    Returns exactly 1.0 for a symbol the brief never covered, so an uncovered
    candidate sizes precisely as it does today.
    """
    if not isinstance(idea, dict):
        return 1.0
    parts: list[float] = []

    up = idea.get("upside_pct")
    if isinstance(up, (int, float)):
        parts.append(max(-1.0, min(1.0, float(up) / 50.0)))

    n_up, n_dn = idea.get("n_up_30d"), idea.get("n_down_30d")
    if isinstance(n_up, int) and isinstance(n_dn, int) and (n_up + n_dn) > 0:
        parts.append((n_up - n_dn) / float(n_up + n_dn))

    lean = str(idea.get("institutional_label") or "").strip().lower()
    if lean:
        if "step" in lean or "back" in lean or "away" in lean:
            parts.append(-1.0)
        elif "lean" in lean or "accumul" in lean:
            parts.append(1.0)
        elif "mixed" in lean or "neutral" in lean:
            parts.append(0.0)

    if not parts:
        return 1.0
    blended = sum(parts) / len(parts)
    return round(max(0.0, 1.0 + blended * float(max_tilt)), 3)


def entry_score_sizing_factor(
    entry_score: Optional[float], max_tilt: float,
) -> float:
    """Map the 0-100 Perfect Entry Score to a bounded sizing multiplier. Pure.

    50 -> 1.0, 100 -> 1 + max_tilt, 0 -> 1 - max_tilt. Never zero and never a
    gate: a weak setup on a strong thesis still gets bought, just smaller. That
    matters for a long-term builder — the cost of skipping a name you want to
    own for years is far higher than the cost of a mediocre entry.
    """
    if entry_score is None:
        return 1.0
    try:
        sc = float(entry_score)
    except (TypeError, ValueError):
        return 1.0
    sc = max(0.0, min(100.0, sc))
    return round(max(0.0, 1.0 + (sc - 50.0) / 50.0 * float(max_tilt)), 3)


def blended_rank_key(
    composite: Optional[float], entry_score: Optional[float], entry_weight: float,
) -> float:
    """Ranking score blending thesis quality with entry timing. Pure.

    The composite (0-10) says WHAT is worth owning; the entry score (0-100) says
    whether NOW is a sane moment to buy it. Ranking on composite alone put
    "extended 20% above the 8 EMA, not ready" ahead of "pullback into the 8/21
    EMA zone, buyable dip" on 2026-08-17.

    Both are normalised to 0-100. A symbol with no entry score keeps its
    composite rank exactly (weight collapses to 0 for that name), so an
    uncovered candidate is never pushed down the queue for missing data it
    was never offered.
    """
    comp = max(0.0, min(100.0, float(composite or 0.0) * 10.0))
    if entry_score is None:
        return round(comp, 3)
    es = max(0.0, min(100.0, float(entry_score)))
    w = max(0.0, min(1.0, float(entry_weight)))
    return round(comp * (1.0 - w) + es * w, 3)


def brief_decision_slice(report: dict[str, Any]) -> dict[str, Any]:
    """The compact, decision-relevant extract worth persisting.

    Deliberately not the whole report: the loops need a small row they can read
    every tick, and storing the narrative + holdings would bloat the audit table
    with text no gate can act on.
    """
    ideas: dict[str, Any] = {}
    raw = report.get("top_ideas")
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol") or "").upper()
            if not sym:
                continue
            analyst = it.get("analyst") if isinstance(it.get("analyst"), dict) else {}
            inst = it.get("institutional") if isinstance(it.get("institutional"), dict) else {}
            entry = it.get("entry") if isinstance(it.get("entry"), dict) else {}
            # Keep only the FAILED checks: on a 10-idea brief the full reason
            # list is hundreds of strings, and what a later audit needs to know
            # is why a setup scored badly, not that its EMA stack was fine.
            failed = [
                str(r.get("text")) for r in (entry.get("reasons") or [])
                if isinstance(r, dict) and not r.get("ok")
            ][:4]
            ideas[sym] = {
                "upside_pct": analyst.get("upside_pct"),
                "pt_consensus": analyst.get("pt_consensus"),
                "n_up_30d": analyst.get("n_up_30d"),
                "n_down_30d": analyst.get("n_down_30d"),
                "institutional_label": inst.get("label"),
                "entry_score": entry.get("score"),
                "entry_label": entry.get("label"),
                "entry_warnings": failed,
                "composite": it.get("composite"),
            }
    posture = report.get("posture") if isinstance(report.get("posture"), dict) else {}
    return {
        "as_of": report.get("as_of"),
        "generated_at": report.get("generated_at"),
        "posture_score": posture.get("score"),
        "posture_label": posture.get("label"),
        "posture_components": posture.get("components"),
        "breaker_capped": posture.get("breaker_capped"),
        "ideas": ideas,
    }


async def persist_brief_signals(report: dict[str, Any]) -> dict[str, Any]:
    """Store the decision slice so the entry loop can read it all day."""
    from ..autotrade.auto_gate import AutoGateResult, record_auto_action

    payload = brief_decision_slice(report)
    async with db_session() as s:
        await record_auto_action(
            s, loop="report", action_type=BRIEF_ACTION_TYPE,
            gate_result=AutoGateResult(passed=True, failures=[]),
            payload=payload, outcome=str(payload.get("posture_label") or "ok"),
        )
    logger.info(
        "morning brief persisted for the entry loop: posture=%s (%s), %d idea read(s)",
        payload.get("posture_score"), payload.get("posture_label"),
        len(payload.get("ideas") or {}),
    )
    return payload


async def load_todays_brief() -> Optional[dict[str, Any]]:
    """Most recent persisted brief slice, or None if absent or STALE.

    Staleness matters here for the same reason it did for the rotation flags: a
    brief from an earlier session describes a market that has moved on, and
    sizing today's entries off it is worse than having no opinion at all. Past
    the age ceiling this returns None and every factor degrades to a neutral
    1.0, so a missed 08:45 run costs nothing rather than applying yesterday's
    posture to today's tape.
    """
    s = get_settings()
    if not getattr(s, "MORNING_BRIEF_WIRED", True):
        return None
    try:
        max_age_h = float(getattr(s, "MORNING_BRIEF_MAX_AGE_HOURS", 30.0))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_h)
        async with db_session() as sess:
            row = (await sess.execute(
                select(AutoAction.payload, AutoAction.timestamp)
                .where(AutoAction.action_type == BRIEF_ACTION_TYPE)
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
                    "morning brief is STALE (%.1fh > %.0fh) — ignoring. Entry sizing "
                    "falls back to neutral rather than applying yesterday's posture.",
                    (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0, max_age_h)
                return None
        return row[0]
    except Exception as e:  # pragma: no cover — never break the entry path
        logger.debug("morning brief load failed: %s", e)
        return None


async def brief_factors(symbol: Optional[str] = None) -> tuple[float, float, dict[str, Any]]:
    """(posture_factor, idea_factor, audit) for the entry loop.

    Fail-neutral in every degraded case — no brief, stale brief, uncovered
    symbol, or wiring switched off all yield (1.0, 1.0), so sizing is exactly
    what it would be without the brief.
    """
    s = get_settings()
    brief = await load_todays_brief()
    if not brief:
        return 1.0, 1.0, {"brief": "unavailable_or_stale"}
    pf = posture_sizing_factor(
        brief.get("posture_score"), float(s.MORNING_POSTURE_MAX_TILT))
    idea = None
    if symbol:
        idea = (brief.get("ideas") or {}).get(symbol.strip().upper())
    inf = idea_sizing_factor(idea, float(s.MORNING_IDEA_MAX_TILT))
    esf = 1.0
    if getattr(s, "MORNING_ENTRY_SCORE_WIRED", True) and isinstance(idea, dict):
        esf = entry_score_sizing_factor(
            idea.get("entry_score"), float(s.MORNING_ENTRY_SCORE_MAX_TILT))
    # Fold the entry-timing tilt into the per-idea factor so callers apply one
    # boost term; both are bounded and multiplicative.
    combined = round(inf * esf, 3)
    return pf, combined, {
        "as_of": brief.get("as_of"),
        "posture_score": brief.get("posture_score"),
        "posture_label": brief.get("posture_label"),
        "posture_factor": pf,
        "street_factor": inf,
        "entry_score": (idea or {}).get("entry_score") if isinstance(idea, dict) else None,
        "entry_label": (idea or {}).get("entry_label") if isinstance(idea, dict) else None,
        "entry_warnings": (idea or {}).get("entry_warnings") if isinstance(idea, dict) else None,
        "entry_factor": esf,
        "idea_factor": combined,
        "idea": idea,
    }


async def entry_rank_map() -> dict[str, Any]:
    """{SYMBOL: entry_score} from today's persisted brief, for candidate ranking.

    Empty when the brief is missing or stale, in which case ranking falls back
    to the composite exactly as before.
    """
    s = get_settings()
    if not getattr(s, "MORNING_ENTRY_SCORE_WIRED", True):
        return {}
    brief = await load_todays_brief()
    if not brief:
        return {}
    out: dict[str, Any] = {}
    for sym, idea in (brief.get("ideas") or {}).items():
        if isinstance(idea, dict) and idea.get("entry_score") is not None:
            out[str(sym).upper()] = idea.get("entry_score")
    return out
