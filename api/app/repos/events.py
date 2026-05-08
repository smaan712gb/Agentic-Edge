"""Run-event repository.

The live SSE consumer reads events from an in-process queue for
sub-second latency. Every event is *also* persisted here so a client
that connects after the run has already begun can be served the
backlog from the DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from api.app.db import RunEvent


class EventRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def append(
        self,
        *,
        run_id: str,
        agent_id: str,
        symbol: Optional[str],
        status: str,
        summary: Optional[str],
        timestamp: Optional[datetime] = None,
    ) -> RunEvent:
        ev = RunEvent(
            run_id=run_id,
            agent_id=agent_id,
            symbol=symbol,
            status=status,
            summary=summary,
            timestamp=timestamp or datetime.now(timezone.utc),
        )
        self.s.add(ev)
        await self.s.flush()
        return ev
