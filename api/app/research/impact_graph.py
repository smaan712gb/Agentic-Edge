"""Supply-chain impact graph — propagate a shock across connected names.

The graph skeleton is free: two symbols that share a theme are adjacent (the
membership matrix from graph_features). Edge weight = number of shared themes,
optionally augmented with NER-extracted directional relations. We then diffuse a
*seed* shock (e.g. recent news sentiment per name) across the graph so a
chokepoint event on one node lights up its neighbours — "ASML EUV slips → who is
exposed?".

The graph build (``build_graph``) and the diffusion (``propagate_impact``) are
pure and unit-tested. The orchestrator seeds from recent NewsMention sentiment.
Research-only — the impact scores never gate a trade.
"""

from __future__ import annotations

import logging
from typing import Mapping, Optional

from .graph_features import build_membership

logger = logging.getLogger("agentic_edge.research")


def build_graph(
    theme_to_symbols: Mapping[str, list[str]],
    extra_edges: Optional[list[dict]] = None,
) -> tuple[list[str], dict[tuple[str, str], float]]:
    """(nodes, {(a,b): weight}) undirected. Weight = shared-theme count (+extra).

    ``extra_edges`` is an optional list of {source, target, weight?} (e.g. from
    NER) folded in additively. Self-loops are dropped; keys are ordered (a<b) so
    each undirected edge appears once.
    """
    membership = build_membership(theme_to_symbols)
    # theme -> members
    theme_members: dict[str, set[str]] = {}
    for sym, themes in membership.items():
        for t in themes:
            theme_members.setdefault(t, set()).add(sym)

    edges: dict[tuple[str, str], float] = {}
    for members in theme_members.values():
        ms = sorted(members)
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                key = (ms[i], ms[j])
                edges[key] = edges.get(key, 0.0) + 1.0

    if extra_edges:
        for e in extra_edges:
            a, b = (e.get("source") or "").upper(), (e.get("target") or "").upper()
            if not a or not b or a == b:
                continue
            key = (a, b) if a < b else (b, a)
            edges[key] = edges.get(key, 0.0) + float(e.get("weight", 1.0))

    nodes = sorted(membership.keys() | {n for k in edges for n in k})
    return nodes, edges


def propagate_impact(
    nodes: list[str], edges: dict[tuple[str, str], float],
    seed: Mapping[str, float], damping: float = 0.5, steps: int = 5,
) -> dict[str, float]:
    """Heat-diffusion of a seed shock across the weighted graph.

    Each step, a node's impact = its own seed + damping * weighted-average of its
    neighbours' current impact. Iterated ``steps`` times. Converges to a bounded,
    distance-decaying spread — a node two hops from a shock feels ~damping^2 of
    it. Pure + deterministic.
    """
    # adjacency: node -> list[(neighbour, weight)]
    adj: dict[str, list[tuple[str, float]]] = {n: [] for n in nodes}
    for (a, b), w in edges.items():
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))

    impact = {n: float(seed.get(n, 0.0)) for n in nodes}
    for _ in range(steps):
        nxt: dict[str, float] = {}
        for n in nodes:
            neigh = adj.get(n, [])
            if neigh:
                wsum = sum(w for _, w in neigh)
                spread = sum(impact[m] * w for m, w in neigh) / wsum if wsum else 0.0
            else:
                spread = 0.0
            nxt[n] = float(seed.get(n, 0.0)) + damping * spread
        impact = nxt
    return {n: round(v, 5) for n, v in impact.items()}


# ---------------------------------------------------------------------------
# Orchestration — seed from recent news sentiment
# ---------------------------------------------------------------------------


async def compute_impact_graph(lookback_days: int = 14, top_edges: int = 200) -> dict:
    """Build the live impact graph seeded by recent chokepoint-news sentiment.

    Returns {nodes:[{symbol, impact, seed, theme_count}], edges:[{source,target,
    weight}], seeded_from}. Reads the universe + NewsMention; no price feed.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from ..db import NewsMention, get_session as db_session
    from .features import _load_universe
    from .graph_features import compute_graph_features

    universe = await _load_universe()
    nodes, edges = build_graph(universe)
    graph_feats = compute_graph_features(universe)

    # Seed = net recent sentiment per ticker (bullish +1, bearish -1), averaged.
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    async with db_session() as s:
        rows = (await s.execute(
            select(NewsMention.ticker, NewsMention.sentiment, NewsMention.conviction)
            .where(NewsMention.captured_at >= since)
        )).all()
    acc: dict[str, list[float]] = {}
    for tkr, sent, conv in rows:
        sym = (tkr or "").upper()
        if not sym:
            continue
        sign = {"bullish": 1.0, "bearish": -1.0}.get(sent or "", 0.0)
        acc.setdefault(sym, []).append(sign * float(conv if conv is not None else 1.0))
    seed = {sym: round(sum(v) / len(v), 4) for sym, v in acc.items() if v}

    impact = propagate_impact(nodes, edges, seed)

    node_dtos = [{
        "symbol": n,
        "impact": impact.get(n, 0.0),
        "seed": seed.get(n, 0.0),
        "theme_count": graph_feats.get(n, {}).get("theme_count", 0),
    } for n in nodes]
    node_dtos.sort(key=lambda d: abs(d["impact"]), reverse=True)

    edge_dtos = [{"source": a, "target": b, "weight": w} for (a, b), w in edges.items()]
    edge_dtos.sort(key=lambda d: d["weight"], reverse=True)

    return {
        "nodes": node_dtos,
        "edges": edge_dtos[:top_edges],
        "seeded_from": f"news sentiment, last {lookback_days}d ({len(seed)} seeded names)",
    }
