"""Broker-truth guards for a long-only LEAPS book.

Every function here answers one question: *what does the broker actually
hold right now?* — because the incident this module exists to prevent was
caused by trusting the DB's memory of a position instead.

FN, 2026-08-18. One intent, ``qty=3``, a genuine earnings-break exit. The
close executor was handed ``qty`` straight off the intent row and fired 28
times over 3h15m. Every attempt was recorded ``abandoned`` with
``fill_price: null``; two of them filled at the broker after the cancel.
Because the quantity came from the intent and never from the account, each
retry asked to sell 3 regardless of what was left. +3 → 0 → −3: a naked
short call in a book whose entire mandate is long calls.

Three separate guards would each have stopped it, so all three live here:

  * ``resolve_close_qty`` — size a close from the LIVE position at submit
    time, clamp to what is held, refuse outright if the position is gone
    or already short.
  * ``short_option_positions`` — detect the state that must never exist.
  * ``halt_on_short_options`` — latch the entry breaker and page, so a
    breached book stops trading instead of trading on into the breach.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import get_settings
from ..db import SystemState, get_session as db_session
from .alerts import alert

logger = logging.getLogger("agentic_edge.position_guard")


# ---------------------------------------------------------------------------
# Live position reads
# ---------------------------------------------------------------------------


def _qty_of(p: dict[str, Any]) -> float:
    try:
        return float(p.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_option(p: dict[str, Any]) -> bool:
    return str(p.get("secType") or p.get("sec_type") or "").upper() in ("OPT", "FOP")


async def live_position_qty(ib: Any, *, conid: int) -> Optional[float]:
    """Signed quantity the broker holds on ``conid`` right now.

    Returns ``0.0`` when the broker is reachable and holds nothing, and
    ``None`` when the position snapshot could NOT be read. The two are not
    interchangeable: "flat" is a fact you may act on, "unknown" is not, and
    collapsing them is how a blind read turns into an unguarded order.
    """
    if not conid:
        return None
    try:
        positions = await ib.get_positions()
    except Exception as e:
        logger.warning("position_guard: broker position read failed for conid %s: %s", conid, e)
        return None
    total = 0.0
    for p in positions or []:
        try:
            if int(p.get("conid") or 0) == int(conid):
                total += _qty_of(p)
        except (TypeError, ValueError):
            continue
    return total


# ---------------------------------------------------------------------------
# Close sizing — broker truth, never the intent's memory
# ---------------------------------------------------------------------------


@dataclass
class CloseQtyDecision:
    """Outcome of sizing a close against the live position."""

    qty: int                  # contracts to actually submit (0 when refusing)
    disposition: str          # "proceed" | "clamped" | "refuse"
    reason: str = ""
    broker_qty: Optional[float] = None   # None = snapshot unreadable

    @property
    def ok(self) -> bool:
        return self.disposition in ("proceed", "clamped") and self.qty > 0

    def to_dict(self) -> dict[str, Any]:
        return {"qty": self.qty, "disposition": self.disposition,
                "reason": self.reason, "broker_qty": self.broker_qty}


async def resolve_close_qty(
    *, ib: Any, conid: int, symbol: str, requested: int, long_only: bool = True,
) -> CloseQtyDecision:
    """Size a sell-to-close from the LIVE position rather than the intent.

    ``requested`` is what the caller *wants* to sell (a full close reads the
    intent's qty, a trim reads a fraction of it). This function is the last
    thing between that number and the broker:

      * position unreadable       → refuse (never sell blind)
      * position == 0             → refuse (nothing to close; the intent is
                                    stale and reconciliation owns it)
      * position < 0 & long_only  → refuse + CRITICAL (already breached —
                                    selling more digs the hole deeper)
      * abs(position) < requested → clamp to what is held
      * otherwise                 → proceed
    """
    sym = (symbol or "").upper()
    req = int(requested or 0)
    if req <= 0:
        return CloseQtyDecision(0, "refuse", "requested qty <= 0")

    held = await live_position_qty(ib, conid=conid)

    if held is None:
        return CloseQtyDecision(
            0, "refuse",
            f"live position for {sym} (conid {conid}) unreadable — refusing to "
            f"submit a close sized from stale bookkeeping",
            broker_qty=None,
        )

    if held == 0:
        return CloseQtyDecision(
            0, "refuse",
            f"broker holds 0 on {sym} (conid {conid}); the intent's qty {req} is "
            f"stale — nothing to close",
            broker_qty=0.0,
        )

    if long_only and held < 0:
        # The invariant is already broken. A close order here is what broke
        # it in the first place (FN: three sells against a three-lot that had
        # already been sold twice), so this path must never place one.
        msg = (
            f"{sym} (conid {conid}) is SHORT {held:g} in a long-only book. "
            f"Refusing to sell {req} more. This is a mandate breach — flatten "
            f"manually and investigate before trading resumes."
        )
        logger.critical("position_guard: %s", msg)
        await alert(level="critical", title=f"LONG-ONLY BREACH — short position: {sym}", body=msg)
        return CloseQtyDecision(0, "refuse", msg, broker_qty=held)

    if abs(held) < req:
        clamped = int(abs(held))
        if clamped <= 0:
            return CloseQtyDecision(
                0, "refuse",
                f"broker holds {held:g} on {sym} — less than one contract",
                broker_qty=held,
            )
        return CloseQtyDecision(
            clamped, "clamped",
            f"broker holds {held:g} on {sym}; clamped close from {req} to {clamped}",
            broker_qty=held,
        )

    return CloseQtyDecision(req, "proceed", "", broker_qty=held)


# ---------------------------------------------------------------------------
# The long-only invariant
# ---------------------------------------------------------------------------


def short_option_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every SHORT option leg in a position snapshot.

    In a LEAPS-only book this list must always be empty. It is the single
    state that cannot be explained by any code path the strategy runs: the
    system buys long calls and sells them to close, so a negative option
    quantity means an order fired against a position that was no longer
    there.
    """
    return [p for p in (positions or []) if _is_option(p) and _qty_of(p) < 0]


def describe_short_options(shorts: list[dict[str, Any]]) -> str:
    parts = []
    for p in shorts:
        parts.append(
            f"{(p.get('symbol') or '?').upper()} {p.get('expiry') or '?'} "
            f"{p.get('strike') or '?'}{(p.get('right') or '')} × {_qty_of(p):g}"
        )
    return "; ".join(parts)


async def halt_on_short_options(
    positions: list[dict[str, Any]], *, source: str,
) -> Optional[str]:
    """Latch the entry breaker and page if the book holds any short option.

    Returns the halt reason, or None when the invariant holds. Only active
    in LEAPS-only mode — with the PMCC strategy enabled a short call is a
    designed leg, not a breach.

    This latches directly in ``system_state`` rather than calling into
    ``circuit_breaker`` so the guard has no import cycle with the module
    that consults it. Clearing it uses the same operator re-arm endpoint as
    every other breaker trip, which is the point: a mandate breach should
    require a human to look at the book before entries resume.
    """
    settings = get_settings()
    if not settings.LEAPS_ONLY:
        return None

    shorts = short_option_positions(positions)
    if not shorts:
        return None

    detail = describe_short_options(shorts)
    reason = (
        f"LONG-ONLY BREACH: {len(shorts)} short option position(s) — {detail}. "
        f"Detected by {source}. New entries halted."
    )
    logger.critical("position_guard: %s", reason)

    already_latched = False
    try:
        async with db_session() as s:
            state = await s.get(SystemState, 1)
            if state is not None:
                already_latched = bool(state.entry_breaker_tripped)
                if not already_latched:
                    state.entry_breaker_tripped = True
                    state.entry_breaker_reason = reason
                    state.entry_breaker_tripped_at = datetime.now(timezone.utc)
    except Exception as e:
        logger.error("position_guard: could not latch breaker on short-option breach: %s", e)

    # Page every time until it is resolved. This is not the class of alert
    # that should be deduped into silence — the book is outside its mandate
    # for as long as the position is open.
    await alert(
        level="critical",
        title=f"LONG-ONLY BREACH — {len(shorts)} short option position(s)",
        body=(f"{detail}. Entries are halted{'' if already_latched else ' (breaker latched)'}. "
              f"Flatten these manually, then re-arm the breaker once the book is clean."),
    )
    return reason
