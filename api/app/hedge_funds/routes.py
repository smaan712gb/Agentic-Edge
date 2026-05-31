"""FastAPI routes for the Hedge Fund Signal Tracker.

Read-only decision-support endpoints, mounted under /api by main.py. Mirrors
the existing theme/run route style (repo + to_dto, thin handlers)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from ..db import get_session as db_session
from .overlap import cross_fund_overlap
from .repo import HedgeFundRepo

router = APIRouter()


@router.get("/api/managers")
async def list_managers() -> list[dict[str, Any]]:
    """Manager tiles: identity + latest filing + top-5 13F moves."""
    async with db_session() as s:
        repo = HedgeFundRepo(s)
        managers = await repo.list_managers(active_only=False)
        out = []
        for m in managers:
            dto = HedgeFundRepo.manager_dto(m)
            dto["top_changes"] = [
                HedgeFundRepo.change_dto(c) for c in await repo.top_changes(m.id, limit=5)
            ]
            out.append(dto)
        return out


@router.get("/api/managers/symbol/{symbol}")
async def smart_money_for_symbol(symbol: str) -> dict[str, Any]:
    """Smart-money read for one ticker — who holds it across tracked managers
    and aggregate exposure. The seam the operator overlays on an entry."""
    async with db_session() as s:
        return await HedgeFundRepo(s).smart_money_for_symbol(ticker=symbol)


@router.get("/api/managers/{slug}/holdings")
async def manager_holdings(slug: str, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    async with db_session() as s:
        repo = HedgeFundRepo(s)
        mgr = await repo.get_manager_by_slug(slug)
        if mgr is None:
            raise HTTPException(404, "manager not found")
        holdings = await repo.latest_holdings(mgr.id, limit=limit)
        return {
            **HedgeFundRepo.manager_dto(mgr),
            "holdings": [HedgeFundRepo.holding_dto(h) for h in holdings],
        }


@router.get("/api/overlap")
async def overlap(
    theme: Optional[str] = None,
    min_managers: int = Query(1, ge=1, le=10),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Cross-fund overlap, optionally filtered to a theme's symbols."""
    return await cross_fund_overlap(theme_id=theme, min_managers=min_managers, limit=limit)


@router.get("/api/managers/flow")
async def managers_flow(symbols: str = Query(..., description="comma-separated tickers")) -> dict[str, Any]:
    """Live UW positioning overlay (flow tilt, gamma, max-pain) for a set of
    tickers — the smart-money names you want to see flow on. Reuses the same
    UW context the theme-regime read uses. Capped to keep the fan-out small."""
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:25]
    if not syms:
        return {}
    try:
        from tradingagents.signals.sector_regime import _fetch_uw_context
    except Exception as e:
        raise HTTPException(503, f"flow provider unavailable: {e}")

    async def _one(sym: str) -> tuple[str, dict[str, Any]]:
        try:
            ctx = await _fetch_uw_context(sym)
            return sym, asdict(ctx)
        except Exception as e:
            return sym, {"note": f"flow fetch failed: {e}", "flow_tilt": None, "gamma_sign": None}

    results = await asyncio.gather(*[_one(s) for s in syms])
    return {sym: ctx for sym, ctx in results}


@router.get("/api/rotation")
async def rotation_status() -> list[dict[str, Any]]:
    """Per-theme rotation-detector state (flagged themes first)."""
    from sqlalchemy import select as _select
    from ..db import ThemeRotation
    async with db_session() as s:
        rows = (await s.execute(_select(ThemeRotation).order_by(
            ThemeRotation.flagged.desc(), ThemeRotation.score.desc()))).scalars().all()
    return [{
        "theme_id": r.theme_id, "flagged": r.flagged, "score": r.score,
        "signals_tripped": r.signals_tripped or [], "evidence": r.evidence or {},
        "computed_at": r.computed_at.isoformat() if r.computed_at else None,
    } for r in rows]


@router.get("/api/mentions")
async def mentions(
    symbol: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Recent chokepoint news mentions (Phase 3), newest first, optionally
    filtered to one ticker."""
    from sqlalchemy import select as _select
    from ..db import NewsMention
    async with db_session() as s:
        q = _select(NewsMention).order_by(NewsMention.captured_at.desc()).limit(limit)
        if symbol:
            q = q.where(NewsMention.ticker == symbol.upper())
        rows = (await s.execute(q)).scalars().all()
    return [{
        "ticker": m.ticker, "theme_id": m.theme_id, "headline": m.headline,
        "provider": m.provider, "chokepoint_hits": m.chokepoint_hits or [],
        "sentiment": m.sentiment, "conviction": m.conviction, "summary": m.summary,
        "published_at": m.published_at.isoformat() if m.published_at else None,
        "captured_at": m.captured_at.isoformat() if m.captured_at else None,
    } for m in rows]
