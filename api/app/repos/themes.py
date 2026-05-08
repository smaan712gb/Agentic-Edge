"""Theme + symbol repository."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.db import Theme, ThemeSymbol


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
        t = await self.s.get(Theme, theme_id)
        if not t:
            return False
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
