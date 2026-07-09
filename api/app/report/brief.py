"""Plain-English daily brief — the report translated for a non-financial reader.

The research agents produce professional output (composites, exit-pressure
bands, gamma, price targets). This module turns the assembled Morning Report
into four short plain-language sections a person with no finance background
can read and act on:

    Where we stand · Our money · The plan for today · What we're watching

The narrative is written by the quick research model, grounded ONLY in the
report JSON it is given (the prompt forbids outside facts and invented
numbers). If the model is unreachable the deterministic fallback composes the
same four sections from the data directly — the brief NEVER blocks or breaks
the report.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("agentic_edge.report.brief")

_BRIEF_TIMEOUT_S = 20.0

_SYS_PROMPT = (
    "You are the plain-language voice of a professional portfolio management "
    "team. You are given a JSON snapshot of today's research output. Write a "
    "short morning brief for the account owner, who has NO finance background.\n"
    "Rules:\n"
    "- Use ONLY facts present in the JSON. Never invent numbers, names, or news.\n"
    "- Plain everyday words. If a finance term is unavoidable, explain it in "
    "the same sentence in parentheses.\n"
    "- Never mention any software, data vendor, model, or product name.\n"
    "- Four sections, each 1-3 short sentences, with EXACTLY these headers:\n"
    "  ## Where we stand\n  ## Our money\n  ## The plan for today\n  ## What we're watching\n"
    "- Be direct about what the system will and won't do today (e.g. if new "
    "buying is paused, say so and why in simple terms).\n"
    "- 200 words maximum. No greeting, no sign-off."
)


async def plain_english_brief(report: dict[str, Any]) -> dict[str, Any]:
    """Return {'text', 'source': 'agent'|'fallback', 'generated_at'}."""
    context = _brief_context(report)
    text: Optional[str] = None
    source = "fallback"
    try:
        text = await asyncio.wait_for(_llm_brief(context), timeout=_BRIEF_TIMEOUT_S)
        if text:
            source = "agent"
    except asyncio.TimeoutError:
        logger.warning("plain-english brief timed out at %.0fs — using fallback", _BRIEF_TIMEOUT_S)
    except Exception as e:
        logger.warning("plain-english brief failed (%s) — using fallback", e)
    if not text:
        text = _fallback_brief(context)
    return {
        "text": text,
        "source": source,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _brief_context(report: dict[str, Any]) -> dict[str, Any]:
    """Compact, grounded snapshot of the report — the ONLY facts the model
    may use. Keeping it small also keeps the call fast and cheap."""
    ideas = report.get("top_ideas")
    ideas = ideas if isinstance(ideas, list) else []
    holdings = report.get("holdings")
    holdings = holdings if isinstance(holdings, list) else []
    alerts = report.get("alerts")
    alerts = alerts if isinstance(alerts, dict) and "entry_breaker" in alerts else {}
    health = report.get("theme_health")
    health = health if isinstance(health, list) else []

    actions = [
        {"symbol": h["symbol"], "action": h["suggested_action"]}
        for h in holdings if not str(h.get("suggested_action", "")).startswith("Hold")
    ]
    return {
        "date": report.get("as_of"),
        "account": report.get("account"),
        "n_buy_ideas": len(ideas),
        "top_ideas": [
            {
                "symbol": i["symbol"], "theme": i.get("theme"),
                "score_out_of_10": i.get("composite"),
                "institutional_read": (i.get("institutional_read") or {}).get("label"),
                "target_upside_pct": (i.get("analyst") or {}).get("upside_pct"),
                "rotation_blocked": i.get("rotation_flagged"),
            }
            for i in ideas[:5]
        ],
        "n_holdings": len(holdings),
        "holdings_needing_action": actions,
        "entry_breaker_tripped": bool((alerts.get("entry_breaker") or {}).get("tripped")),
        "themes_rotation_flagged": [r.get("theme_id") for r in (alerts.get("rotation_flagged") or [])],
        "stake_reductions_on_holdings": [
            {"symbol": r.get("ticker"), "change": r.get("change_type")}
            for r in (alerts.get("holding_stake_reductions") or [])
        ],
        "strongest_themes": [
            {"theme": t["theme"], "score_out_of_10": t["composite"]} for t in health[:3]
        ],
        "weakest_themes": [
            {"theme": t["theme"], "score_out_of_10": t["composite"]} for t in health[-3:]
        ] if len(health) > 3 else [],
    }


async def _llm_brief(context: dict[str, Any]) -> Optional[str]:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    from openai import AsyncOpenAI
    from ..config import get_settings
    settings = get_settings()
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=(settings.DEEPSEEK_BASE_URL or "https://api.deepseek.com"),
    )
    resp = await client.chat.completions.create(
        model=settings.DEEPSEEK_QUICK_MODEL,
        messages=[
            {"role": "system", "content": _SYS_PROMPT},
            {"role": "user", "content": json.dumps(context, default=str)},
        ],
        temperature=0.2,
    )
    text = (resp.choices[0].message.content or "").strip()
    return text or None


_CLASSIFY_TIMEOUT_S = 15.0

_CLASSIFY_SYS_PROMPT = (
    "You assess SEC filings for likely short-term stock-price impact. Input: a "
    "JSON list of filings, each with symbol, form_type, rationale (why our "
    "watcher flagged it), and is_held. Output STRICT JSON: "
    '{"verdicts": [{"impact": "positive"|"negative"|"neutral", '
    '"note": "<plain-words reason, max 20 words, no jargon>"}]} '
    "— one verdict per input, same order. Judge ONLY from the given fields; "
    "if the rationale is not informative, lean on the form type's typical "
    "meaning. Never invent specifics."
)


async def classify_filing_impacts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One batched model call: per-filing impact verdicts. [] on any failure —
    callers keep their deterministic defaults."""
    if not items:
        return []
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return []
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
                    {"role": "system", "content": _CLASSIFY_SYS_PROMPT},
                    {"role": "user", "content": json.dumps(items, default=str)},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            ),
            timeout=_CLASSIFY_TIMEOUT_S,
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        verdicts = parsed.get("verdicts")
        return verdicts if isinstance(verdicts, list) else []
    except Exception as e:
        logger.debug("filing impact classification failed: %s", e)
        return []


def _fallback_brief(c: dict[str, Any]) -> str:
    """Deterministic plain-language composition — same four sections, no LLM."""
    lines: list[str] = []

    lines.append("## Where we stand")
    flagged = c.get("themes_rotation_flagged") or []
    if c.get("entry_breaker_tripped"):
        lines.append("The safety brake is on: no new buying until it is reviewed. "
                     "Positions we already own are still watched and managed.")
    elif flagged:
        lines.append(f"Big investors appear to be moving money out of {len(flagged)} of our "
                     "investment areas, so the system is holding off on new buying there "
                     "and watching existing positions more closely.")
    else:
        lines.append("No warnings today. The system is operating normally.")

    lines.append("## Our money")
    acct = c.get("account") or {}
    if acct.get("equity"):
        cash_pct = acct.get("cash_pct")
        cash_note = f", about {cash_pct:.0f}% of it in cash" if cash_pct is not None else ""
        lines.append(f"The account is worth about ${acct['equity']:,.0f}{cash_note}.")
    n_hold = c.get("n_holdings", 0)
    if n_hold:
        lines.append(f"We hold {n_hold} position{'s' if n_hold != 1 else ''}.")
    else:
        lines.append("We hold no positions right now — the account is fully in cash.")

    lines.append("## The plan for today")
    actions = c.get("holdings_needing_action") or []
    if actions:
        acts = "; ".join(f"{a['symbol']}: {a['action']}" for a in actions[:5])
        lines.append(f"Positions that need a decision — {acts}.")
    ideas = c.get("top_ideas") or []
    if ideas:
        names = ", ".join(i["symbol"] for i in ideas[:5])
        blocked = all(i.get("rotation_blocked") for i in ideas)
        if blocked:
            lines.append(f"The research team's favorite ideas today are {names}, but new "
                         "buying is paused in their areas until the money-flow warning clears.")
        else:
            lines.append(f"The research team's favorite ideas today are {names}.")
    elif not actions:
        lines.append("Nothing needs a decision today.")

    lines.append("## What we're watching")
    reductions = c.get("stake_reductions_on_holdings") or []
    watch: list[str] = []
    if reductions:
        watch.append("large investors trimming stakes in "
                     + ", ".join(str(r["symbol"]) for r in reductions[:3]))
    if flagged:
        watch.append("whether money keeps leaving the flagged areas")
    strongest = c.get("strongest_themes") or []
    if strongest:
        watch.append(f"{strongest[0]['theme']} remains our strongest area")
    lines.append(("We are watching " + "; ".join(watch) + ".") if watch
                 else "Nothing unusual on the radar.")

    return "\n".join(lines)
