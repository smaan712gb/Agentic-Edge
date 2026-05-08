"""Universe enforcement — every auto-tradable symbol must come from a theme.

Hard rule: a symbol can be auto-traded if and only if it is currently
listed in ``theme_symbols`` for at least one theme. Removing the last
theme that contains a symbol retires it from new auto-entries — but
existing positions continue to be maintained (rolls, hedges) until the
operator manually closes them.

This is the *selection floor*. The agent runs above it choose which of
the universe symbols to enter; this module ensures nothing outside the
universe ever reaches the auto-execution layer, even by mistake.
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.db import ThemeSymbol


async def current_universe(session: AsyncSession) -> set[str]:
    """Return the set of symbols currently in any theme."""
    rows = (
        await session.execute(select(distinct(ThemeSymbol.symbol)))
    ).scalars().all()
    return {s.upper() for s in rows}


async def validate_in_universe(session: AsyncSession, symbol: str) -> bool:
    """True iff ``symbol`` appears in at least one theme today."""
    sym = symbol.strip().upper()
    return sym in await current_universe(session)


async def filter_to_universe(
    session: AsyncSession, symbols: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Split ``symbols`` into (in_universe, out_of_universe)."""
    universe = await current_universe(session)
    in_u, out_u = [], []
    for raw in symbols:
        sym = raw.strip().upper()
        (in_u if sym in universe else out_u).append(sym)
    return in_u, out_u
