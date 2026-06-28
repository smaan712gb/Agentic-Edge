"""FastAPI read routes for the Quant Research Factory.

Read-only decision-support endpoints, mounted under /api by main.py. Mirrors
the hedge-fund route style (thin handlers, plain dict DTOs). Manual triggers
(snapshot / label) live behind /api/admin with the admin token.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from ..db import SymbolFeatureSnapshot, get_session as db_session
from .graph_features import compute_graph_features
from .features import _load_universe

router = APIRouter()


@router.get("/api/research/universe-graph")
async def universe_graph(limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    """The 'one graph, not 17 themes' view — every symbol ranked by chokepoint
    centrality (how many themes/bottlenecks it sits in). Computed live from the
    membership matrix; needs no snapshot history and no network."""
    universe = await _load_universe()
    graph = compute_graph_features(universe)
    rows = [{"symbol": sym, **feats} for sym, feats in graph.items()]
    rows.sort(key=lambda r: (r["theme_count"], r["co_membership_degree"]), reverse=True)
    return rows[:limit]


@router.get("/api/research/features/latest")
async def latest_features(
    symbols: Optional[str] = Query(None, description="comma-separated tickers; all if omitted"),
) -> list[dict[str, Any]]:
    """Most-recent feature snapshot per symbol."""
    wanted = {s.strip().upper() for s in symbols.split(",")} if symbols else None
    async with db_session() as s:
        rows = (await s.execute(
            select(SymbolFeatureSnapshot).order_by(
                SymbolFeatureSnapshot.symbol, SymbolFeatureSnapshot.as_of.desc(),
            )
        )).scalars().all()
    latest: dict[str, SymbolFeatureSnapshot] = {}
    for r in rows:
        if wanted is not None and r.symbol not in wanted:
            continue
        latest.setdefault(r.symbol, r)   # first seen = newest (desc order)
    return [_snap_dto(r) for r in latest.values()]


@router.get("/api/research/features/{symbol}")
async def feature_history(
    symbol: str, limit: int = Query(120, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """Point-in-time feature history for one symbol (newest first)."""
    async with db_session() as s:
        rows = (await s.execute(
            select(SymbolFeatureSnapshot)
            .where(SymbolFeatureSnapshot.symbol == symbol.strip().upper())
            .order_by(SymbolFeatureSnapshot.as_of.desc())
            .limit(limit)
        )).scalars().all()
    if not rows:
        raise HTTPException(404, "no snapshots for symbol")
    return [_snap_dto(r) for r in rows]


def _snap_dto(r: SymbolFeatureSnapshot) -> dict[str, Any]:
    return {
        "symbol": r.symbol,
        "as_of": r.as_of.isoformat() if r.as_of else None,
        "features": r.features or {},
        "labels": r.labels or {},
        "label_status": r.label_status,
    }


@router.get("/api/research/ranking")
async def ranking(
    horizon: int = Query(20, ge=5, le=60),
    model: str = Query("ridge", pattern="^(ridge|gbm)$"),
) -> dict[str, Any]:
    """Pooled cross-sectional ML ranking of the universe by predicted forward
    return. Degrades to a transparent heuristic until enough labels accrue."""
    from .ml_ranker import rank_universe
    return await rank_universe(horizon_days=horizon, model=model)


@router.get("/api/research/event-study")
async def event_study() -> dict[str, Any]:
    """Market-adjusted CAR by catalyst type (13D / news / 13F changes)."""
    from dataclasses import asdict
    from .event_study import run_event_study
    return asdict(await run_event_study())


@router.get("/api/research/montecarlo/{symbol}")
async def montecarlo(
    symbol: str,
    horizon: int = Query(20, ge=5, le=120),
    paths: int = Query(10000, ge=1000, le=50000),
) -> dict[str, Any]:
    """Forward outcome distribution + suggested size + exit stress for a name."""
    from dataclasses import asdict
    from .montecarlo import run_montecarlo
    return asdict(await run_montecarlo(symbol, horizon_days=horizon, n_paths=paths))


@router.get("/api/research/impact-graph")
async def impact_graph(lookback_days: int = Query(14, ge=1, le=90)) -> dict[str, Any]:
    """Supply-chain impact graph seeded by recent chokepoint-news sentiment."""
    from .impact_graph import compute_impact_graph
    return await compute_impact_graph(lookback_days=lookback_days)


@router.get("/api/research/personas")
async def personas_meta() -> list[dict[str, str]]:
    """The manager archetypes (identity + description) the factory scores."""
    from .personas import persona_meta
    return persona_meta()


@router.get("/api/research/personas/{symbol}")
async def personas_for_symbol(symbol: str) -> dict[str, Any]:
    """Per-archetype scores for a symbol, read from its latest snapshot
    (computed deterministically from that day's features)."""
    sym = symbol.strip().upper()
    async with db_session() as s:
        row = (await s.execute(
            select(SymbolFeatureSnapshot)
            .where(SymbolFeatureSnapshot.symbol == sym)
            .order_by(SymbolFeatureSnapshot.as_of.desc())
            .limit(1)
        )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "no snapshot for symbol — run a feature snapshot first")
    feats = row.features or {}
    scores = {k.removeprefix("persona_"): v for k, v in feats.items() if k.startswith("persona_")}
    return {"symbol": sym, "as_of": row.as_of.isoformat() if row.as_of else None,
            "archetypes": scores}
