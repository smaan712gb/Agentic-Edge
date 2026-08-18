"""Theme + symbol repository."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.db import (
    NewsMention, Run, RunEvent, Theme, ThemeReport, ThemeRotation, ThemeSymbol,
    TickerScore, TradeIntent,
)


class ThemeRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # -- read --------------------------------------------------------

    async def list(self, user_id: Optional[str] = None) -> list[Theme]:
        q = select(Theme).order_by(Theme.created_at.asc())
        if user_id is not None:
            q = q.where((Theme.user_id == user_id) | (Theme.user_id.is_(None)))
        return list((await self.s.execute(q)).scalars().all())

    async def get(self, theme_id: str) -> Optional[Theme]:
        return await self.s.get(Theme, theme_id)

    async def count_for_user(self, user_id: Optional[str]) -> int:
        q = select(func.count()).select_from(Theme)
        if user_id is not None:
            q = q.where(Theme.user_id == user_id)
        return (await self.s.execute(q)).scalar_one()

    # -- write -------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        thesis: str,
        chokepoint: str = "",
        user_id: Optional[str] = None,
    ) -> Theme:
        # Slug-ish id; collision-safe.
        base = name.strip().lower().replace(" ", "-")
        tid = base
        n = 2
        while await self.s.get(Theme, tid):
            tid = f"{base}-{n}"
            n += 1
        theme = Theme(
            id=tid,
            user_id=user_id,
            name=name.strip(),
            thesis=thesis.strip(),
            chokepoint=chokepoint.strip(),
        )
        self.s.add(theme)
        await self.s.flush()
        await self.s.refresh(theme, attribute_names=["symbols"])
        return theme

    async def delete(self, theme_id: str) -> bool:
        """Delete a theme and everything that hangs off it, in dependency order.

        Deleting used to fail outright for any theme that had ever been run:

            IntegrityError: NOT NULL constraint failed: runs.theme_id

        The ``runs`` relationship carries no cascade, so SQLAlchemy tried to
        ORPHAN the runs by nulling their ``theme_id`` — a column declared
        NOT NULL. Only never-run themes could be deleted, which is exactly the
        opposite of what an operator wants: the ones worth removing are the
        ones with history.

        The database cannot help here. SQLite enforces foreign keys only when
        ``PRAGMA foreign_keys=ON``, which this app never sets, so the schema's
        ON DELETE CASCADE clauses are decorative at runtime. Everything is
        therefore removed explicitly, in order, with Core statements rather
        than ORM cascades — a theme can own ~50 runs whose events number in the
        thousands, and loading that graph to delete it row-by-row is needless.

        TRADE INTENTS ARE NEVER DELETED. They reference runs and carry the
        record of real positions, including open ones; their FK is SET NULL by
        design. Cascading into them would destroy position history — and for a
        live LEAP, the only row that knows the system owns it. Their run_id is
        nulled instead, which is what the schema asks for.
        """
        t = await self.s.get(Theme, theme_id)
        if not t:
            return False

        run_ids = [
            r[0] for r in (
                await self.s.execute(select(Run.id).where(Run.theme_id == theme_id))
            ).all()
        ]

        if run_ids:
            # 1. Preserve position history: detach intents, never delete them.
            await self.s.execute(
                update(TradeIntent)
                .where(TradeIntent.run_id.in_(run_ids))
                .values(run_id=None)
            )
            # 2. Children of the runs.
            for model in (RunEvent, TickerScore, ThemeReport):
                await self.s.execute(delete(model).where(model.run_id.in_(run_ids)))
            # 3. The runs themselves.
            await self.s.execute(delete(Run).where(Run.id.in_(run_ids)))

        # 4. Theme-scoped rows not reached via a run.
        await self.s.execute(delete(ThemeReport).where(ThemeReport.theme_id == theme_id))
        await self.s.execute(delete(ThemeRotation).where(ThemeRotation.theme_id == theme_id))
        # News is shared research, not theme-owned — detach, matching its SET NULL FK.
        await self.s.execute(
            update(NewsMention)
            .where(NewsMention.theme_id == theme_id)
            .values(theme_id=None)
        )

        # 5. The theme. Its symbols go with it via the ORM cascade.
        await self.s.delete(t)
        return True

    async def add_symbol(self, theme_id: str, symbol: str) -> Optional[Theme]:
        t = await self.s.get(Theme, theme_id)
        if not t:
            return None
        sym = symbol.strip().upper()
        existing = {s.symbol for s in t.symbols}
        if sym not in existing:
            t.symbols.append(ThemeSymbol(symbol=sym, position=len(t.symbols)))
            t.updated_at = datetime.now(timezone.utc)
        await self.s.flush()
        await self.s.refresh(t, attribute_names=["symbols"])
        return t

    async def remove_symbol(self, theme_id: str, symbol: str) -> bool:
        sym = symbol.upper()
        result = await self.s.execute(
            delete(ThemeSymbol).where(
                (ThemeSymbol.theme_id == theme_id) & (ThemeSymbol.symbol == sym),
            )
        )
        return result.rowcount > 0

    # -- DTO ---------------------------------------------------------

    @staticmethod
    def to_dto(t: Theme) -> dict[str, Any]:
        return {
            "id": t.id,
            "name": t.name,
            "thesis": t.thesis,
            "chokepoint": t.chokepoint,
            "symbols": [s.symbol for s in sorted(t.symbols, key=lambda x: x.position)],
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
