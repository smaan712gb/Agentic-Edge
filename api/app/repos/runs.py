"""Run + score repository.

The run lifecycle is:

    queued → running → done | error

Events are appended to ``run_events`` continuously by the runner. Scores
land in ``ticker_scores`` as they're produced. The summary + ranked
``best_positioned`` is set on the Run row at the end.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.app.db import Run, RunEvent, ThemeReport, TickerScore


class RunRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # -- create / update --------------------------------------------

    async def create(
        self,
        *,
        theme_id: str,
        user_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> Run:
        run = Run(
            id=uuid.uuid4().hex[:8],
            user_id=user_id,
            theme_id=theme_id,
            status="running",
            progress=0.0,
            started_at=datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
            config=config,
        )
        self.s.add(run)
        await self.s.flush()
        return run

    async def find_by_idempotency_key(
        self, user_id: Optional[str], key: str,
    ) -> Optional[Run]:
        q = select(Run).where(Run.idempotency_key == key)
        if user_id is not None:
            q = q.where(Run.user_id == user_id)
        return (await self.s.execute(q)).scalars().first()

    async def update_progress(self, run_id: str, progress: float) -> None:
        r = await self.s.get(Run, run_id)
        if r is not None:
            r.progress = progress

    async def mark_done(
        self, run_id: str, *, summary: Optional[str], best_positioned: list[str],
    ) -> None:
        r = await self.s.get(Run, run_id)
        if r is None:
            return
        r.status = "done"
        r.progress = 1.0
        r.finished_at = datetime.now(timezone.utc)
        r.summary = summary
        r.best_positioned = best_positioned

    async def mark_error(self, run_id: str, error: str) -> None:
        r = await self.s.get(Run, run_id)
        if r is None:
            return
        r.status = "error"
        r.finished_at = datetime.now(timezone.utc)
        r.error = error

    # -- read -------------------------------------------------------

    async def get(self, run_id: str) -> Optional[Run]:
        q = (
            select(Run)
            .where(Run.id == run_id)
            .options(
                selectinload(Run.events),
                selectinload(Run.scores),
                selectinload(Run.report),
            )
        )
        return (await self.s.execute(q)).scalars().first()

    async def list(self, *, user_id: Optional[str] = None, limit: int = 100) -> list[Run]:
        q = (
            select(Run)
            .options(selectinload(Run.scores))
            .order_by(Run.started_at.desc())
            .limit(limit)
        )
        if user_id is not None:
            q = q.where((Run.user_id == user_id) | (Run.user_id.is_(None)))
        return list((await self.s.execute(q)).scalars().all())

    async def count_recent_for_user(self, user_id: Optional[str], since: datetime) -> int:
        from sqlalchemy import func
        q = select(func.count()).select_from(Run).where(Run.started_at >= since)
        if user_id is not None:
            q = q.where(Run.user_id == user_id)
        return (await self.s.execute(q)).scalar_one()

    # -- scores -----------------------------------------------------

    async def add_score(
        self,
        *,
        run_id: str,
        symbol: str,
        setup: float,
        options: float,
        thesis_fit: float,
        composite: float,
        decision: str,
        conviction: Optional[int],
        drivers: list[str],
        risks: list[str],
        rationale: Optional[str] = None,
        agent_reports: Optional[dict[str, Any]] = None,
    ) -> TickerScore:
        score = TickerScore(
            run_id=run_id,
            symbol=symbol,
            setup=setup,
            options=options,
            thesis_fit=thesis_fit,
            composite=composite,
            decision=decision,
            conviction=conviction,
            drivers=drivers,
            risks=risks,
            rationale=rationale,
            agent_reports=agent_reports,
        )
        self.s.add(score)
        await self.s.flush()
        return score

    async def add_report(
        self,
        *,
        run_id: str,
        theme_id: str,
        summary: str,
        ranking: list[str],
        best_positioned: list[str],
    ) -> ThemeReport:
        rep = ThemeReport(
            run_id=run_id,
            theme_id=theme_id,
            summary=summary,
            ranking=ranking,
            best_positioned=best_positioned,
        )
        self.s.add(rep)
        await self.s.flush()
        return rep

    # -- DTO --------------------------------------------------------

    @staticmethod
    def to_dto(run: Run) -> dict[str, Any]:
        return {
            "id": run.id,
            "theme_id": run.theme_id,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "status": run.status,
            "progress": run.progress,
            "summary": run.summary,
            "best_positioned": run.best_positioned or [],
            "events": [
                {
                    "agent_id": e.agent_id,
                    "symbol": e.symbol,
                    "status": e.status,
                    "summary": e.summary,
                    "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                }
                for e in run.events
            ],
            "scores": [
                {
                    "symbol": sc.symbol,
                    "setup": sc.setup,
                    "options": sc.options,
                    "thesis_fit": sc.thesis_fit,
                    "composite": sc.composite,
                    "decision": sc.decision,
                    "drivers": sc.drivers or [],
                    "risks": sc.risks or [],
                }
                for sc in run.scores
            ],
        }
