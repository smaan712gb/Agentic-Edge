"""Repository for the hedge-fund tracker tables.

Mirrors the ``ThemeRepo`` shape (session-bound, ``to_dto`` statics). Owns all
reads/writes for managers, filings, holdings, and position changes so the
poller, routes, and overlap calc share one query surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.db import (
    CusipTickerMap,
    Filing,
    FundHolding,
    HedgeFundManager,
    ManagerCik,
    PositionChange,
)


@dataclass
class AggHolding:
    """A per-manager position aggregated across that manager's CIKs/filings
    for one period. Quacks like FundHolding for the DTO/reporting paths."""
    cusip: str
    put_call_flag: str
    issuer_name: str
    ticker: Optional[str]
    value_usd: float
    shares: float
    pct_of_portfolio: Optional[float]
    period_end: Optional[datetime]


class HedgeFundRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # -- managers ----------------------------------------------------

    async def list_managers(self, active_only: bool = True) -> list[HedgeFundManager]:
        q = select(HedgeFundManager).order_by(HedgeFundManager.name.asc())
        if active_only:
            q = q.where(HedgeFundManager.active.is_(True))
        return list((await self.s.execute(q)).scalars().all())

    async def get_manager_by_slug(self, slug: str) -> Optional[HedgeFundManager]:
        return (
            await self.s.execute(select(HedgeFundManager).where(HedgeFundManager.slug == slug))
        ).scalar_one_or_none()

    async def upsert_manager(
        self,
        *,
        slug: str,
        name: str,
        ciks: list[tuple[str, Optional[str]]],
        macro_only: bool = False,
        active: bool = True,
        tier: str = "tier1",
        primary_themes: Optional[list[str]] = None,
        weighting_profile: Optional[dict[str, float]] = None,
    ) -> HedgeFundManager:
        """Create or update a manager from config. CIKs are reconciled to the
        config list (added if missing); existing CIKs are never deleted here
        so historical filings keep their FK."""
        mgr = await self.get_manager_by_slug(slug)
        if mgr is None:
            mgr = HedgeFundManager(slug=slug)
            self.s.add(mgr)
        mgr.name = name
        mgr.macro_only = macro_only
        mgr.active = active
        mgr.tier = tier
        mgr.primary_themes = primary_themes
        mgr.weighting_profile = weighting_profile
        await self.s.flush()

        existing = {c.cik for c in (await self.s.execute(
            select(ManagerCik).where(ManagerCik.manager_id == mgr.id)
        )).scalars().all()}
        for cik, entity_name in ciks:
            if cik and cik not in existing:
                self.s.add(ManagerCik(manager_id=mgr.id, cik=cik, entity_name=entity_name))
        await self.s.flush()
        await self.s.refresh(mgr, attribute_names=["ciks"])
        return mgr

    # -- filings -----------------------------------------------------

    async def filing_exists(self, accession_no: str) -> bool:
        return (await self.s.execute(
            select(func.count()).select_from(Filing).where(Filing.accession_no == accession_no)
        )).scalar_one() > 0

    async def latest_13f_periods(self, manager_id: int, limit: int = 2) -> list[datetime]:
        """The most recent distinct 13F period_ends for a manager (for Q/Q
        delta). Newest first."""
        rows = (await self.s.execute(
            select(FundHolding.period_end)
            .where(FundHolding.manager_id == manager_id)
            .where(FundHolding.period_end.is_not(None))
            .group_by(FundHolding.period_end)
            .order_by(FundHolding.period_end.desc())
            .limit(limit)
        )).scalars().all()
        return list(rows)

    async def holdings_for_period(self, manager_id: int, period_end: datetime) -> list["AggHolding"]:
        """Holdings for a manager at a period, **aggregated per security**.

        A manager can file under several CIKs (Situational Awareness has two),
        each producing its own 13F for the same quarter — so the raw rows
        double-count a name. We sum value/shares by (cusip, put_call_flag) so
        every downstream read (top-20, Q/Q deltas, overlap, smart-money) sees
        one true per-manager position. pct_of_portfolio is recomputed against
        the aggregated total."""
        rows = (await self.s.execute(
            select(
                FundHolding.cusip,
                FundHolding.put_call_flag,
                func.max(FundHolding.issuer_name),
                func.max(FundHolding.ticker),
                func.sum(FundHolding.value_usd),
                func.sum(FundHolding.shares),
            )
            .where(FundHolding.manager_id == manager_id)
            .where(FundHolding.period_end == period_end)
            .group_by(FundHolding.cusip, FundHolding.put_call_flag)
        )).all()
        total = sum((r[4] or 0.0) for r in rows) or 1.0
        out = [
            AggHolding(
                cusip=r[0], put_call_flag=r[1] or "", issuer_name=r[2] or "",
                ticker=r[3], value_usd=r[4] or 0.0, shares=r[5] or 0.0,
                pct_of_portfolio=round(100.0 * (r[4] or 0.0) / total, 3),
                period_end=period_end,
            )
            for r in rows
        ]
        out.sort(key=lambda h: h.value_usd, reverse=True)
        return out

    async def latest_holdings(self, manager_id: int, limit: int = 100) -> list[FundHolding]:
        periods = await self.latest_13f_periods(manager_id, limit=1)
        if not periods:
            return []
        rows = await self.holdings_for_period(manager_id, periods[0])
        return rows[:limit]

    # -- position changes -------------------------------------------

    async def top_changes(self, manager_id: int, limit: int = 5) -> list[PositionChange]:
        """Most recent period's changes, ranked by absolute share delta."""
        latest = (await self.s.execute(
            select(func.max(PositionChange.current_period)).where(PositionChange.manager_id == manager_id)
        )).scalar_one_or_none()
        if latest is None:
            return []
        rows = (await self.s.execute(
            select(PositionChange)
            .where(PositionChange.manager_id == manager_id)
            .where(PositionChange.current_period == latest)
            .where(PositionChange.change_type != "hold")
        )).scalars().all()
        rows = sorted(rows, key=lambda c: abs((c.current_shares or 0) - (c.prior_shares or 0)), reverse=True)
        return rows[:limit]

    # -- smart-money read for one symbol ----------------------------

    async def smart_money_for_symbol(self, *, ticker: Optional[str] = None,
                                     cusip: Optional[str] = None) -> dict[str, Any]:
        """Which tracked managers hold this name in their latest 13F, with
        aggregate exposure. The seam the operator overlays on an entry."""
        if not ticker and not cusip:
            return {"matched": False, "managers": [], "aggregate_value_usd": 0.0}

        # Latest holdings per manager are those at each manager's max period.
        # Subquery: max period_end per manager.
        max_period = (
            select(FundHolding.manager_id, func.max(FundHolding.period_end).label("mp"))
            .group_by(FundHolding.manager_id)
            .subquery()
        )
        q = (
            select(FundHolding, HedgeFundManager)
            .join(max_period, (FundHolding.manager_id == max_period.c.manager_id)
                  & (FundHolding.period_end == max_period.c.mp))
            .join(HedgeFundManager, HedgeFundManager.id == FundHolding.manager_id)
        )
        if ticker:
            q = q.where(func.upper(FundHolding.ticker) == ticker.upper())
        else:
            q = q.where(FundHolding.cusip == (cusip or "").upper())

        rows = (await self.s.execute(q)).all()
        # Dedupe by manager (a manager's multiple CIKs each file a 13F, so the
        # same name shows up once per CIK) — sum within a manager, then count
        # DISTINCT managers so the cross-fund confirmation isn't inflated.
        by_mgr: dict[str, dict[str, Any]] = {}
        for holding, mgr in rows:
            m = by_mgr.setdefault(mgr.slug, {
                "slug": mgr.slug, "name": mgr.name, "tier": mgr.tier,
                "value_usd": 0.0, "shares": 0.0,
                "put_call_flag": holding.put_call_flag,
                "pct_of_portfolio": holding.pct_of_portfolio,
                "period_end": holding.period_end.isoformat() if holding.period_end else None,
            })
            m["value_usd"] += holding.value_usd
            m["shares"] += holding.shares
        managers = list(by_mgr.values())
        agg_value = sum(m["value_usd"] for m in managers)
        agg_shares = sum(m["shares"] for m in managers)
        return {
            "matched": bool(managers),
            "ticker": ticker.upper() if ticker else None,
            "cusip": cusip.upper() if cusip else None,
            # Cross-fund confirmation: ≥2 managers on the same name.
            "confirmation": len(managers) >= 2,
            "manager_count": len(managers),
            "aggregate_value_usd": agg_value,
            "aggregate_shares": agg_shares,
            "managers": sorted(managers, key=lambda m: m["value_usd"], reverse=True),
        }

    # -- cusip→ticker map -------------------------------------------

    async def resolve_ticker(self, cusip: str) -> Optional[str]:
        row = await self.s.get(CusipTickerMap, cusip.upper())
        return row.ticker if row else None

    async def upsert_cusip_ticker(self, *, cusip: str, ticker: str,
                                  issuer_name: Optional[str] = None,
                                  resolved_via: str = "form4") -> None:
        cusip = cusip.upper()
        row = await self.s.get(CusipTickerMap, cusip)
        if row is None:
            row = CusipTickerMap(cusip=cusip)
            self.s.add(row)
        if ticker:
            row.ticker = ticker.upper()
        if issuer_name:
            row.issuer_name = issuer_name
        row.resolved_via = resolved_via

    # -- DTOs --------------------------------------------------------

    @staticmethod
    def manager_dto(m: HedgeFundManager) -> dict[str, Any]:
        return {
            "slug": m.slug,
            "name": m.name,
            "tier": m.tier,
            "macro_only": m.macro_only,
            "active": m.active,
            "primary_themes": m.primary_themes or [],
            "ciks": [c.cik for c in m.ciks],
            "last_filing_at": m.last_filing_at.isoformat() if m.last_filing_at else None,
        }

    @staticmethod
    def holding_dto(h: FundHolding) -> dict[str, Any]:
        return {
            "issuer_name": h.issuer_name,
            "ticker": h.ticker,
            "cusip": h.cusip,
            "value_usd": h.value_usd,
            "shares": h.shares,
            "put_call_flag": h.put_call_flag,
            "pct_of_portfolio": h.pct_of_portfolio,
            "period_end": h.period_end.isoformat() if h.period_end else None,
        }

    @staticmethod
    def change_dto(c: PositionChange) -> dict[str, Any]:
        return {
            "ticker": c.ticker,
            "cusip": c.cusip,
            "issuer_name": c.issuer_name,
            "prior_shares": c.prior_shares,
            "current_shares": c.current_shares,
            "change_pct": c.change_pct,
            "change_type": c.change_type,
            "current_period": c.current_period.isoformat() if c.current_period else None,
        }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
