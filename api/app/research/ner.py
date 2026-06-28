"""Named-entity recognition over chokepoint news — the domain edge.

Two layers, by design:

  * DETERMINISTIC core (``extract_entities``) — pure, no network, unit-tested.
    Matches the chokepoint vocabulary (the same dictionary the news sweep uses)
    and the live ticker universe as whole-word entities. Always available.
  * LLM enrichment (``extract_relations_llm``) — best-effort DeepSeek pass that
    pulls directional supplier→customer / company→chokepoint relations from the
    text. Degrades to ``[]`` on any failure; the deterministic layer still
    yields usable entities.

The extracted entities + relations feed impact_graph.py: a chokepoint hit on one
node propagates to its supply-chain neighbours. Research-only.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Iterable

logger = logging.getLogger("agentic_edge.research")


def _chokepoint_patterns() -> list[tuple[str, "re.Pattern[str]"]]:
    """Reuse the news sweep's chokepoint dictionary as the entity vocabulary."""
    from ..hedge_funds.news import CHOKEPOINT_KEYWORDS
    return [(kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE))
            for kw in CHOKEPOINT_KEYWORDS]


def extract_entities(text: str, universe_symbols: Iterable[str]) -> dict[str, list[str]]:
    """Deterministic entity extraction: {chokepoints, tickers}.

    Tickers are matched as whole upper-case tokens against the supplied universe
    so '$MU' / 'MU' hit but 'museum' does not. Pure + offline.
    """
    if not text:
        return {"chokepoints": [], "tickers": []}
    chokepoints = [kw for kw, rx in _chokepoint_patterns() if rx.search(text)]

    universe = {s.upper() for s in universe_symbols}
    tokens = set(re.findall(r"\b[A-Z]{1,6}\b", text))
    tickers = sorted(universe & tokens)
    return {"chokepoints": chokepoints, "tickers": tickers}


_REL_SYS_PROMPT = (
    "You extract supply-chain relations from a news snippet about the AI/"
    "semiconductor buildout. Respond with strict JSON: {\"relations\": "
    "[{\"source\": str, \"target\": str, \"type\": \"supplies|competes|"
    "depends_on|invests_in\", \"chokepoint\": str|null}]}. Use ticker symbols "
    "when a company is clearly identifiable, else its short name. Keep it to "
    "relations actually stated; empty list if none."
)


async def extract_relations_llm(text: str, symbol_hint: str | None = None) -> list[dict[str, Any]]:
    """Best-effort directional relations via DeepSeek. [] on any failure."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key or not text:
        return []
    try:
        from openai import AsyncOpenAI
        from ..config import get_settings
        settings = get_settings()
        client = AsyncOpenAI(api_key=api_key,
                             base_url=(settings.DEEPSEEK_BASE_URL or "https://api.deepseek.com"))
        user = (f"Symbol context: {symbol_hint}\n\n" if symbol_hint else "") + text[:4000]
        resp = await client.chat.completions.create(
            model=settings.DEEPSEEK_QUICK_MODEL,
            messages=[{"role": "system", "content": _REL_SYS_PROMPT},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        rels = parsed.get("relations") or []
        out: list[dict[str, Any]] = []
        for r in rels:
            if isinstance(r, dict) and r.get("source") and r.get("target"):
                out.append({
                    "source": str(r["source"]).upper(),
                    "target": str(r["target"]).upper(),
                    "type": str(r.get("type") or "depends_on"),
                    "chokepoint": r.get("chokepoint"),
                })
        return out
    except Exception as e:
        logger.debug("ner: LLM relation extraction failed: %s", e)
        return []
