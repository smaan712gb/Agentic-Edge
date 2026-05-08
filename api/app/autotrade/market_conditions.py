"""Market-conditions sanity checks.

The auto gate covers system state and budgets; this module covers the
*market* side — things that change minute-to-minute and would make a
correctly-budgeted action a bad idea right now.

Checks:

  is_rth(now)                        — within US regular trading hours (09:30–16:00 ET)
  is_first_15_min(now)               — open auction noise window (skipped for entries)
  vix_ok(threshold=40)               — VIX below threshold
  symbol_not_halted(symbol)          — IBKR/exchange halt check
  spread_acceptable(option, max=5%)  — bid/ask spread ≤ max % of mid
  volume_sane(option, min=...)       — today's contract volume above floor

Each check returns a ``GateFailure | None`` so callers can fold them
into the same result object the auto gate produces.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo
from typing import Any, Optional

from .auto_gate import GateFailure

logger = logging.getLogger(__name__)


_NY = ZoneInfo("America/New_York")


def _now_et(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(_NY)).astimezone(_NY)


def is_rth(now: Optional[datetime] = None) -> bool:
    """True if New York time is within 09:30–16:00 on a weekday."""
    et = _now_et(now)
    if et.weekday() >= 5:
        return False
    return time(9, 30) <= et.time() <= time(16, 0)


def is_first_2_min(now: Optional[datetime] = None) -> bool:
    """Block only the first 2-minute candle (9:30:00–9:32:00 ET).

    Operator preference: confirmation candle, not full 15-min open
    auction settle. Entries fire from 09:32 ET onwards.
    """
    et = _now_et(now)
    return et.weekday() < 5 and time(9, 30) <= et.time() < time(9, 32)


def is_first_15_min(now: Optional[datetime] = None) -> bool:
    """Legacy 15-min check kept for callers that want the full window."""
    et = _now_et(now)
    return et.weekday() < 5 and time(9, 30) <= et.time() <= time(9, 45)


def gate_rth(now: Optional[datetime] = None) -> Optional[GateFailure]:
    if not is_rth(now):
        return GateFailure("rth", "outside regular trading hours")
    return None


def gate_open_auction_block(now: Optional[datetime] = None) -> Optional[GateFailure]:
    """Block only the first 2 minutes (9:30:00–9:32:00 ET) for new entries."""
    if is_first_2_min(now):
        return GateFailure(
            "open_auction",
            "first 2-minute candle — confirmation window, entries blocked",
        )
    return None


# ---------------------------------------------------------------------------
# VIX gate
# ---------------------------------------------------------------------------


async def gate_vix(threshold: float = 40.0) -> Optional[GateFailure]:
    """VIX above threshold blocks new entries (regime is too disorderly).

    Reads VIX from yfinance (free, no key) — VIX rarely moves so frequently
    that a 1-min cache is a problem; we just fetch fresh per gate call.
    Failure to fetch is non-fatal: we log and let the action proceed.
    """
    try:
        import yfinance as yf  # type: ignore
        # ^VIX is the symbol; daily close is fine for our purpose.
        df = yf.Ticker("^VIX").history(period="2d", auto_adjust=False)
        if df.empty:
            return None
        last = float(df["Close"].iloc[-1])
        if last > threshold:
            return GateFailure(
                "vix", f"VIX {last:.1f} > {threshold:.1f}",
                detail={"vix": last, "threshold": threshold},
            )
    except Exception as e:  # pragma: no cover
        logger.warning("vix gate fetch failed (proceeding): %s", e)
    return None


# ---------------------------------------------------------------------------
# Per-contract / per-symbol checks (IBKR layer plumbs the data in)
# ---------------------------------------------------------------------------


def gate_spread_acceptable(
    bid: float, ask: float, *, max_pct: float = 0.05,
) -> Optional[GateFailure]:
    if bid <= 0 or ask <= 0 or ask < bid:
        return GateFailure("spread", "invalid bid/ask", detail={"bid": bid, "ask": ask})
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid if mid > 0 else 1.0
    if spread_pct > max_pct:
        return GateFailure(
            "spread", f"spread {spread_pct:.2%} > {max_pct:.2%}",
            detail={"bid": bid, "ask": ask, "spread_pct": spread_pct},
        )
    return None


def gate_open_interest(oi: int, *, min_oi: int) -> Optional[GateFailure]:
    if oi < min_oi:
        return GateFailure(
            "open_interest", f"OI {oi} < {min_oi}",
            detail={"oi": oi, "min": min_oi},
        )
    return None


def gate_iv_rank(iv_rank: float, *, min_rank: float = 40.0) -> Optional[GateFailure]:
    if iv_rank < min_rank:
        return GateFailure(
            "iv_rank", f"IV rank {iv_rank:.0f} < {min_rank:.0f}",
            detail={"iv_rank": iv_rank, "min": min_rank},
        )
    return None


def gate_symbol_not_halted(symbol_state: dict[str, Any]) -> Optional[GateFailure]:
    """Caller passes IBKR's contract market state. Halt indicators vary by
    exchange — we look at a few common ones and bail if any flag is set.
    """
    if not symbol_state:
        return None
    halted = (
        symbol_state.get("halted")
        or symbol_state.get("trading_halt")
        or symbol_state.get("market_state") in ("HALT", "PAUSED", "AUCTION")
    )
    if halted:
        return GateFailure(
            "halt", f"symbol halted/paused",
            detail={"state": symbol_state},
        )
    return None
