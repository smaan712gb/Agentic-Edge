"""FastAPI routes for the Hedge Fund Signal Tracker.

Read-only decision-support endpoints, mounted under /api by main.py. Mirrors
the existing theme/run route style (repo + to_dto, thin handlers)."""

from __future__ import annotations

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
