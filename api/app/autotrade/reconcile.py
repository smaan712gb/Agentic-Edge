"""Position reconciliation: DB ↔ IBKR.

Run before any auto-action on a symbol (per-symbol scan) and twice
daily as full-account scans (09:25 ET pre-open, 16:30 ET post-close).
On mismatch the function emits an alert + returns a failure; the
calling auto-loop blocks its action.

Reconciliation is intentionally read-only. It never *fixes* state — a
human decides whether the DB or IBKR is the truth in any given drift.
The fix path is a manual operator action that writes positions table
to match IBKR (or cancels phantom orders).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.db import Position

from .alerts import alert
from .auto_gate import GateFailure

logger = logging.getLogger(__name__)


@dataclass
class ReconcileMismatch:
    symbol: str
    db: dict[str, Any]
    ibkr: dict[str, Any]
    field: str  # qty | avg_price | none_in_db | none_in_ibkr


@dataclass
class ReconcileResult:
    ok: bool
    mismatches: list[ReconcileMismatch] = field(default_factory=list)
    summary: str = ""


# Tolerances — IBKR's reported avg_price drifts by pennies due to FX/fees,
# so an exact match would be too strict. Quantity must match exactly.
_AVG_PRICE_TOLERANCE_PCT = 0.005  # 0.5%


async def reconcile_symbol(
    session: AsyncSession,
    *,
    symbol: str,
    ibkr_provider: Any,
) -> ReconcileResult:
    """Compare DB position for ``symbol`` against IBKR's reported position."""
    sym = symbol.strip().upper()

    # DB positions for this symbol (potentially multiple if more than one
    # account is tracked; for now single-account so length 0 or 1).
    db_rows = (
        await session.execute(
            select(Position).where(Position.symbol == sym)
        )
    ).scalars().all()
    db_qty = sum(float(p.qty) for p in db_rows)
    db_avg = (
        sum(float(p.qty) * float(p.avg_price) for p in db_rows) / db_qty
        if db_qty else 0.0
    )

    # IBKR side
    try:
        ib_positions = await ibkr_provider.get_positions()
    except Exception as e:
        logger.warning("ibkr position fetch failed during reconcile: %s", e)
        await alert(level="warning", title="Reconcile failed",
                    body=f"could not fetch IBKR positions: {e}")
        return ReconcileResult(False, [], summary=f"ibkr fetch error: {e}")

    ib_match = [p for p in ib_positions if (p.get("symbol") or "").upper() == sym]
    ib_qty = sum(float(p.get("qty") or 0) for p in ib_match)
    ib_avg = (
        sum(float(p.get("qty") or 0) * float(p.get("avg_price") or 0) for p in ib_match) / ib_qty
        if ib_qty else 0.0
    )

    mismatches: list[ReconcileMismatch] = []
    if abs(db_qty - ib_qty) > 1e-6:
        mismatches.append(ReconcileMismatch(
            symbol=sym, db={"qty": db_qty, "avg_price": db_avg},
            ibkr={"qty": ib_qty, "avg_price": ib_avg}, field="qty",
        ))
    elif db_qty > 0:
        diff_pct = abs(db_avg - ib_avg) / max(ib_avg, 0.01)
        if diff_pct > _AVG_PRICE_TOLERANCE_PCT:
            mismatches.append(ReconcileMismatch(
                symbol=sym, db={"qty": db_qty, "avg_price": db_avg},
                ibkr={"qty": ib_qty, "avg_price": ib_avg}, field="avg_price",
            ))

    if mismatches:
        await alert(
            level="warning",
            title=f"Position mismatch on {sym}",
            body=f"DB qty {db_qty} avg {db_avg:.2f} | IBKR qty {ib_qty} avg {ib_avg:.2f}",
        )
        return ReconcileResult(False, mismatches, summary=f"{sym} mismatch")
    return ReconcileResult(True, [], summary=f"{sym} ok")


async def reconcile_all(
    session: AsyncSession, *, ibkr_provider: Any,
) -> ReconcileResult:
    """Full-account scan: every distinct symbol in DB or IBKR is checked."""
    db_syms = {
        s for s in (
            await session.execute(select(Position.symbol).distinct())
        ).scalars().all()
    }
    try:
        ib_positions = await ibkr_provider.get_positions()
        ib_syms = {(p.get("symbol") or "").upper() for p in ib_positions if p.get("symbol")}
    except Exception as e:
        await alert(level="warning", title="Reconcile-all failed",
                    body=f"could not fetch IBKR positions: {e}")
        return ReconcileResult(False, [], summary=f"ibkr fetch error: {e}")

    all_mismatches: list[ReconcileMismatch] = []
    for sym in db_syms | ib_syms:
        r = await reconcile_symbol(session, symbol=sym, ibkr_provider=ibkr_provider)
        all_mismatches.extend(r.mismatches)

    if all_mismatches:
        return ReconcileResult(False, all_mismatches,
                               summary=f"{len(all_mismatches)} mismatch(es)")
    return ReconcileResult(True, [], summary=f"{len(db_syms | ib_syms)} symbols ok")


def reconcile_to_gate_failure(result: ReconcileResult) -> Optional[GateFailure]:
    """Lift a ReconcileResult into the gate failure shape so the auto gate
    can fold it into its result list uniformly."""
    if result.ok:
        return None
    return GateFailure(
        "reconcile",
        result.summary,
        detail={
            "mismatches": [
                {"symbol": m.symbol, "field": m.field,
                 "db": m.db, "ibkr": m.ibkr}
                for m in result.mismatches
            ],
        },
    )
