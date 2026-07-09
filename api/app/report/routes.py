"""Morning Report endpoint — read-only, mounted under /api by main.py."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from .morning import build_morning_report

router = APIRouter()


@router.get("/api/report/morning")
async def morning_report(
    refresh: bool = Query(False, description="bypass the 5-minute cache"),
    lookback_hours: int = Query(24, ge=1, le=168),
    top_n: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    """The daily CIO brief: top buy ideas, per-holding exit pressure +
    suggested action, fresh institutional activity, and risk alerts."""
    return await build_morning_report(
        lookback_hours=lookback_hours, top_n=top_n, refresh=refresh,
    )
