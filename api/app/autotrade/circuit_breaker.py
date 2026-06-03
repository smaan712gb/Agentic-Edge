"""Entry circuit breaker — account-level, halts NEW entries only.

The line this enforces (per operator policy on a high-beta book): a bad day
must never *close* positions. Position exits stay signal-driven (thesis
break, momentum exhaustion, exit-pressure, theme health). This breaker only
pauses *new* entries when the account itself is in trouble — capital
discipline ("stop adding on a bleeding day / when margin is thin / when we're
flying blind"), not a stop-loss.

Triggers (any one latches the breaker):
  * intraday NetLiq down more than BREAKER_INTRADAY_NAV_DROP_PCT from the
    day's opening NAV,
  * margin-risk cushion (ExcessLiquidity / NetLiq — the real distance to a
    margin call, NOT free cash) below BREAKER_MIN_MARGIN_CUSHION_PCT,
  * can't read the account or IBKR is disconnected (don't trade blind).

Once tripped it **latches** in system_state until an operator re-arms via
``POST /api/admin/autotrade/rearm-breaker``. The maintenance/exit loop is
unaffected — it keeps managing open positions on their own signals.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import get_settings
from ..db import SystemState, get_session as db_session
from .alerts import alert

logger = logging.getLogger("agentic_edge.circuit_breaker")


def _f(summary: dict[str, Any], tag: str) -> Optional[float]:
    try:
        v = summary.get(tag)
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


async def check_entry_breaker(ib: Any) -> Optional[str]:
    """Return a halt reason if NEW entries should be paused, else None.

    Evaluates + latches. Safe to call every entry tick. Never closes
    positions; callers use the return value only to skip new entries.
    """
    settings = get_settings()
    if not settings.BREAKER_ENABLED:
        return None

    # 1) Already latched? Stay halted until manual re-arm.
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is None:
            return None
        if state.entry_breaker_tripped:
            return state.entry_breaker_reason or "entry circuit breaker tripped"

    # 2) Read live account health (network — outside the DB session).
    reason: Optional[str] = None
    netliq: Optional[float] = None
    excess: Optional[float] = None
    maint: Optional[float] = None
    account_ok = False
    try:
        summary = await ib.get_account_summary()
        netliq = _f(summary, "NetLiquidation")
        # ExcessLiquidity is the TRUE distance-to-margin-call. For a long-only,
        # fully-paid options book it ~= NetLiq (no leverage), so it stays high
        # even when fully invested. We deliberately do NOT use AvailableFunds
        # here — that's just remaining cash (deployment headroom), which is
        # near-zero when fully invested yet carries ZERO margin-call risk;
        # using it false-tripped this breaker. Deployment discipline lives in
        # the entry caps; this EMERGENCY breaker fires only on real danger.
        excess = _f(summary, "ExcessLiquidity")
        maint = _f(summary, "MaintMarginReq")
        account_ok = True
    except Exception as e:
        reason = f"account read failed ({e}) — pausing new entries"

    # Connection staleness — don't open new positions blind. BUT a successful
    # account read above already proves the socket is live THIS tick, so only
    # consult the heartbeat snapshot when we have no independent confirmation.
    # Otherwise a cold/stale startup snapshot (the heartbeat hasn't beaten yet)
    # false-trips the breaker on every restart even though IBKR is connected.
    if reason is None and not account_ok:
        try:
            from .heartbeat import is_connected_snapshot
            if not is_connected_snapshot():
                reason = "IBKR disconnected — pausing new entries"
        except Exception:
            pass

    # 3) Intraday NAV drop + margin cushion (needs a readable NetLiq).
    drop_pct: Optional[float] = None
    if reason is None and netliq is not None and netliq > 0:
        now = datetime.now(timezone.utc)
        async with db_session() as s:
            state = await s.get(SystemState, 1)
            ref = state.breaker_nav_ref if state else None
            ref_date = state.breaker_nav_ref_date if state else None
            # Capture / reset the day's opening NAV on the first tick of a new
            # UTC day (stable across US RTH, which closes ~20:00 UTC).
            if state is not None and (ref is None or ref_date is None
                                      or ref_date.date() != now.date() or ref <= 0):
                state.breaker_nav_ref = netliq
                state.breaker_nav_ref_date = now
                ref = netliq
        if ref and ref > 0:
            drop_pct = (netliq - ref) / ref
            if drop_pct <= -abs(settings.BREAKER_INTRADAY_NAV_DROP_PCT):
                reason = (
                    f"intraday NetLiq {drop_pct*100:.1f}% vs day open "
                    f"(${ref:,.0f} -> ${netliq:,.0f}); threshold "
                    f"{-settings.BREAKER_INTRADAY_NAV_DROP_PCT*100:.0f}%"
                )

        # Real margin-risk cushion: ExcessLiquidity / NetLiq (fall back to
        # (NetLiq - MaintMargin)/NetLiq). If neither is readable, assume safe
        # (1.0) rather than false-trip.
        cushion = None
        if excess is not None:
            cushion = excess / netliq
        elif maint is not None:
            cushion = (netliq - maint) / netliq
        if reason is None and cushion is not None:
            if cushion < settings.BREAKER_MIN_MARGIN_CUSHION_PCT:
                reason = (
                    f"margin cushion {cushion*100:.1f}% (excess-liquidity basis); "
                    f"NetLiq ${netliq:,.0f}; "
                    f"floor {settings.BREAKER_MIN_MARGIN_CUSHION_PCT*100:.0f}%"
                )

    if reason is None:
        return None

    # 4) Latch + alert.
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is not None and not state.entry_breaker_tripped:
            state.entry_breaker_tripped = True
            state.entry_breaker_reason = reason
            state.entry_breaker_tripped_at = datetime.now(timezone.utc)
    logger.warning("ENTRY CIRCUIT BREAKER tripped: %s", reason)
    await alert(
        level="critical",
        title="Entry circuit breaker tripped — new entries paused",
        body=f"{reason}. Open positions continue on exit signals. Re-arm via admin once reviewed.",
    )
    return reason


async def rearm_breaker(actor: str = "operator") -> dict[str, Any]:
    """Clear the latch so new entries can resume. Resets the NAV reference so
    the next tick re-captures the day's opening NAV."""
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is None:
            return {"ok": False, "reason": "system_state missing"}
        was = state.entry_breaker_tripped
        state.entry_breaker_tripped = False
        state.entry_breaker_reason = None
        state.entry_breaker_tripped_at = None
        state.breaker_nav_ref = None
        state.breaker_nav_ref_date = None
        state.updated_by = actor
    logger.info("entry circuit breaker re-armed by %s (was tripped=%s)", actor, was)
    await alert(level="info", title="Entry circuit breaker re-armed",
                body=f"New entries may resume. Re-armed by {actor}.")
    return {"ok": True, "was_tripped": was}


async def breaker_status() -> dict[str, Any]:
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is None:
            return {"enabled": get_settings().BREAKER_ENABLED, "tripped": False}
        return {
            "enabled": get_settings().BREAKER_ENABLED,
            "tripped": state.entry_breaker_tripped,
            "reason": state.entry_breaker_reason,
            "tripped_at": state.entry_breaker_tripped_at.isoformat() if state.entry_breaker_tripped_at else None,
            "nav_ref": state.breaker_nav_ref,
        }
