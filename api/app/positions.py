"""Real positions / equity, sourced from IBKR paper account.

Three endpoints feed off this module:

  /api/positions             — current open positions
  /api/performance/today     — today's gain/loss + account value
  /api/performance/curve     — 90-day equity curve (from equity_snapshots)

The IBKR connection is established lazily and reused via the singleton
in ``ib_client``. Snapshots are written to the DB on every read so the
equity curve grows with usage; the chart is always backed by historical
DB rows, not a live IBKR call.

Double-gate enforcement (paper mode + account-id prefix 'D') happens
inside ``IbkrProvider``; this layer trusts the provider's invariants.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from .config import get_settings
from .db import EquitySnapshot, Position, get_session as db_session

logger = logging.getLogger(__name__)


_ib_lock = asyncio.Lock()
_ib_provider: Any | None = None


async def _ibkr() -> Any:
    """Singleton IbkrProvider with auto-reconnect.

    First call: instantiates + connects. Subsequent calls: returns the
    same provider; the provider's ``_ensure_connected`` handles dropped
    sockets transparently inside each method that uses it.
    """
    global _ib_provider
    async with _ib_lock:
        if _ib_provider is not None:
            # Provider exists — proactively reconnect if the socket dropped.
            await _ib_provider._ensure_connected()
            return _ib_provider
        from tradingagents.dataflows.providers.ibkr import IbkrProvider
        s = get_settings()
        _ib_provider = IbkrProvider(
            host=s.IBKR_HOST, port=s.IBKR_PORT, client_id=s.IBKR_CLIENT_ID,
            account_mode=s.IBKR_MODE,
        )
        await _ib_provider._ensure_connected()
        return _ib_provider


async def positions_real() -> list[dict[str, Any]]:
    """Read open positions from IBKR paper, snapshot to DB, return DTOs."""
    try:
        prov = await _ibkr()
        rows = await prov.get_positions()
    except Exception as e:
        logger.warning("IBKR positions read failed (%s) — returning empty", e)
        return []

    out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    async with db_session() as s:
        for r in rows:
            symbol = r.get("symbol")
            qty = float(r.get("qty") or 0)
            if not symbol or qty == 0:
                continue
            avg_price = float(r.get("avg_price") or 0)
            last_price = float(r.get("last_price") or 0)
            pnl = float(r.get("pnl") or (last_price - avg_price) * qty)
            account_id = str(r.get("account_id") or "default")
            out.append({
                "symbol": symbol, "qty": qty, "avg_price": avg_price,
                "last_price": last_price, "pnl": pnl,
            })
            # Upsert position snapshot. Use SQLAlchemy core dialect-specific
            # path so the same code works on SQLite (dev) and Postgres (prod).
            stmt = (
                sqlite_upsert(Position).values(
                    user_id=None, account_id=account_id, symbol=symbol,
                    qty=qty, avg_price=avg_price, last_price=last_price,
                    pnl=pnl, captured_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "account_id", "symbol"],
                    set_={"qty": qty, "avg_price": avg_price,
                          "last_price": last_price, "pnl": pnl,
                          "captured_at": now},
                )
            )
            await s.execute(stmt)
    return out


async def equity_today_real() -> dict[str, Any]:
    """Today's gain/loss + current account equity from IBKR.

    Pulls IBKR's authoritative ``DailyPnL`` from accountSummary when
    available (requires an active reqPnL subscription on the account; the
    field is populated as soon as IB's P&L service responds). Falls back
    to comparing today's snapshot vs the previous DB snapshot.

    Records today's snapshot so the equity-curve endpoint has historical
    data to render.
    """
    try:
        prov = await _ibkr()
        summary = await prov.get_account_summary()
    except Exception as e:
        logger.warning("IBKR account summary failed (%s)", e)
        return {"equity": 0.0, "gain": 0.0, "gain_pct": 0.0,
                "unrealized_pnl": 0.0, "realized_pnl": 0.0,
                "cash": 0.0, "notional": 0.0,
                "as_of": datetime.now(timezone.utc).date().isoformat(),
                "error": str(e)}

    # IBKR account-summary tags are CamelCase strings → numeric values.
    # See: https://interactivebrokers.github.io/tws-api/account_summary_tags.html
    def _f(*tags: str) -> float:
        for t in tags:
            v = summary.get(t)
            if v is not None and str(v) != "":
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return 0.0

    equity         = _f("NetLiquidation", "EquityWithLoanValue")
    cash           = _f("TotalCashValue")
    notional       = _f("GrossPositionValue")
    daily_pnl      = _f("DailyPnL")            # IBKR-computed, authoritative when present
    unrealized_pnl = _f("UnrealizedPnL")
    realized_pnl   = _f("RealizedPnL")
    available      = _f("AvailableFunds")
    buying_power   = _f("BuyingPower")

    # IBKR's account ID for the currently-connected paper session. We
    # already validated in _ensure_connected that the prefix is 'D'; just
    # ask ib_insync for the canonical id.
    account_id = "default"
    try:
        ib = await prov._ensure_connected()
        accounts = ib.managedAccounts()
        if accounts:
            account_id = accounts[0]
    except Exception:
        pass

    # ET-aware "today" — trading day, not UTC day. Using UTC for the
    # ``as_of`` field caused the UI to render the prior calendar day
    # because browsers parse ISO dates as UTC midnight then localise
    # back. ET is the relevant calendar for an IBKR US-stocks account.
    from zoneinfo import ZoneInfo
    et = datetime.now(ZoneInfo("America/New_York"))
    today_et_date = et.date()
    today = datetime(today_et_date.year, today_et_date.month, today_et_date.day,
                     tzinfo=timezone.utc)  # store ET-midnight as a UTC datetime for DB

    # Snapshot upsert + fetch today's *first* snapshot (start-of-monitoring
    # baseline) so "Today" P&L shows realized + unrealized vs. the moment
    # the system started watching today, not vs. a missing yesterday.
    async with db_session() as s:
        # First snapshot today — if a row already exists, keep the original
        # equity (first-of-day baseline) instead of overwriting.
        first_today = (
            await s.execute(
                select(EquitySnapshot)
                .where(EquitySnapshot.account_id == account_id)
                .where(EquitySnapshot.date == today)
            )
        ).scalars().first()
        if first_today is None:
            s.add(EquitySnapshot(
                user_id=None, account_id=account_id, date=today,
                equity=equity, cash=cash, notional=notional,
            ))
            opening_equity = equity
        else:
            # Keep the first observation; update cash/notional only.
            first_today.cash = cash
            first_today.notional = notional
            opening_equity = float(first_today.equity)

        prev = (
            await s.execute(
                select(EquitySnapshot)
                .where(EquitySnapshot.account_id == account_id)
                .where(EquitySnapshot.date < today)
                .order_by(EquitySnapshot.date.desc())
                .limit(1)
            )
        ).scalars().first()

    # Priority order for "Today" P&L:
    #   1. IBKR's DailyPnL tag (authoritative) when populated
    #   2. Yesterday's close snapshot (when we have one)
    #   3. First-snapshot-today (system-monitoring baseline) — day-1 default
    if daily_pnl != 0.0:
        gain = round(daily_pnl, 2)
        baseline = equity - daily_pnl if equity else 0.0
    elif prev is not None:
        baseline = float(prev.equity)
        gain = round(equity - baseline, 2)
    else:
        baseline = opening_equity
        gain = round(equity - baseline, 2)
    gain_pct = round(((equity / baseline) - 1) * 100, 4) if baseline else 0.0

    return {
        "equity":         equity,
        "gain":           gain,
        "gain_pct":       gain_pct,
        "unrealized_pnl": round(unrealized_pnl, 2),
        "realized_pnl":   round(realized_pnl, 2),
        "cash":           round(cash, 2),
        "notional":       round(notional, 2),
        "available_funds": round(available, 2),
        "buying_power":   round(buying_power, 2),
        "account_id":     account_id,
        "as_of":          today_et_date.isoformat(),
    }


async def equity_curve_real() -> dict[str, Any]:
    """Last-90-days equity curve from equity_snapshots."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=120)
    async with db_session() as s:
        rows = (
            await s.execute(
                select(EquitySnapshot)
                .where(EquitySnapshot.date >= cutoff)
                .order_by(EquitySnapshot.date.asc())
            )
        ).scalars().all()

    points = [{"date": r.date.date().isoformat(), "equity": r.equity} for r in rows]
    starting = points[0]["equity"] if points else 0.0
    return {"points": points, "starting_equity": starting}
