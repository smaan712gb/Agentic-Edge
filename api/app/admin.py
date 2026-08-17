"""Admin endpoints: runtime kill switch, status, manual reconciliation triggers.

Authn: requires ``X-Admin-Token`` header matching ``Settings.ADMIN_API_TOKEN``.
If no token is configured, the endpoints return 503 — refusing to operate
rather than running open. This is intentional: in production the secret
manager supplies the token; in dev the operator sets it explicitly.

The kill switch is the *runtime* half of the dual gate. The other half
is ``Settings.AUTOTRADE_ENABLED`` which requires a redeploy to flip;
this row toggles in milliseconds for fast incident response.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from sqlalchemy import select

from .autotrade.alerts import alert
from .config import get_settings
from .db import SystemState, get_session as db_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin(token: str | None) -> None:
    settings = get_settings()
    if not settings.ADMIN_API_TOKEN:
        raise HTTPException(503, "ADMIN_API_TOKEN not configured — admin endpoints are disabled")
    if not token or token != settings.ADMIN_API_TOKEN:
        raise HTTPException(401, "invalid or missing X-Admin-Token")


class KillSwitchToggle(BaseModel):
    reason: str = ""
    actor: str = "operator"


@router.get("/autotrade/status")
async def autotrade_status(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin(x_admin_token)
    settings = get_settings()
    async with db_session() as s:
        state = await s.get(SystemState, 1)
    return {
        "env_autotrade_enabled":  settings.AUTOTRADE_ENABLED,
        "db_autotrade_enabled":   bool(state and state.autotrade_enabled),
        "effective_enabled":      settings.AUTOTRADE_ENABLED and bool(state and state.autotrade_enabled),
        "last_kill_at":           (state.last_kill_at.isoformat() if state and state.last_kill_at else None),
        "kill_reason":            state.kill_reason if state else None,
        "updated_at":             (state.updated_at.isoformat() if state else None),
        "updated_by":             state.updated_by if state else None,
        "entry_breaker_tripped":  bool(state and state.entry_breaker_tripped),
        "entry_breaker_reason":   state.entry_breaker_reason if state else None,
        "entry_breaker_tripped_at": (state.entry_breaker_tripped_at.isoformat()
                                     if state and state.entry_breaker_tripped_at else None),
    }


@router.post("/autotrade/rearm-breaker")
async def autotrade_rearm_breaker(
    body: KillSwitchToggle,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Clear the entry circuit-breaker latch so NEW entries can resume.

    Distinct from the kill switch: the breaker only paused new entries; the
    maintenance/exit loop kept managing open positions throughout. Re-arming
    re-captures the day's NAV reference on the next entry tick."""
    _require_admin(x_admin_token)
    from .autotrade.circuit_breaker import rearm_breaker
    return await rearm_breaker(actor=body.actor)


@router.post("/autotrade/enable")
async def autotrade_enable(
    body: KillSwitchToggle,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Flip the runtime kill switch ON.

    Both halves of the gate must agree before any auto-action runs.
    Enabling here is necessary but not sufficient if AUTOTRADE_ENABLED
    env is false.
    """
    _require_admin(x_admin_token)
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is None:
            raise HTTPException(500, "system_state not initialised")
        state.autotrade_enabled = True
        state.kill_reason = None
        state.updated_by = body.actor
        state.updated_at = datetime.now(timezone.utc)
    await alert(
        level="warning",
        title="Auto-trade ENABLED",
        body=f"by={body.actor}; reason={body.reason or '(none)'}",
    )
    return {"ok": True, "autotrade_enabled": True}


@router.post("/autotrade/disable")
async def autotrade_disable(
    body: KillSwitchToggle,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Flip the runtime kill switch OFF (immediate hard stop)."""
    _require_admin(x_admin_token)
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is None:
            raise HTTPException(500, "system_state not initialised")
        state.autotrade_enabled = False
        state.last_kill_at = datetime.now(timezone.utc)
        state.kill_reason = body.reason or "operator disable"
        state.updated_by = body.actor
        state.updated_at = state.last_kill_at
    await alert(
        level="critical",
        title="Auto-trade DISABLED",
        body=f"by={body.actor}; reason={body.reason or '(none)'}",
    )
    return {"ok": True, "autotrade_enabled": False, "reason": body.reason or "operator disable"}


@router.get("/scheduler/status")
async def scheduler_status_endpoint(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin(x_admin_token)
    from .scheduler import scheduler_status
    return await scheduler_status()


class SchedulerCronUpdate(BaseModel):
    cron: str
    actor: str = "operator"


@router.post("/scheduler/enable")
async def scheduler_enable_endpoint(
    body: KillSwitchToggle,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin(x_admin_token)
    from .scheduler import enable_scheduler
    return await enable_scheduler(actor=body.actor)


@router.post("/scheduler/disable")
async def scheduler_disable_endpoint(
    body: KillSwitchToggle,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin(x_admin_token)
    from .scheduler import disable_scheduler
    return await disable_scheduler(actor=body.actor)


@router.post("/scheduler/update-cron")
async def scheduler_update_cron_endpoint(
    body: SchedulerCronUpdate,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin(x_admin_token)
    from .scheduler import update_cron
    try:
        return await update_cron(body.cron, actor=body.actor)
    except ValueError as e:
        raise HTTPException(400, f"invalid cron: {e}")


@router.post("/scheduler/run-now")
async def scheduler_run_now_endpoint(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Fire the daily job immediately, in addition to the cron schedule."""
    _require_admin(x_admin_token)
    from .scheduler import fire_now
    try:
        result = await fire_now()
        return {"ok": True, "result": result}
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/positions/import")
async def import_existing_positions(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Register every IBKR position as a managed TradeIntent.

    Use case: you already hold stocks before the system started and want
    the system to take ownership of them — this writes one TradeIntent
    row per holding with ``structure='existing_stock'`` and
    ``status='filled'``. The maintenance loop (Phase D) will then evaluate
    them on each daily run and propose rolls/exits/conversions to PMCC.

    Idempotent: skips positions already imported (matched by symbol).
    """
    _require_admin(x_admin_token)
    from .positions import _ibkr
    from .db import TradeAuditLog, TradeIntent

    try:
        ib = await _ibkr()
        rows = await ib.get_positions()
    except Exception as e:
        raise HTTPException(503, f"IBKR not reachable: {e}")

    stocks = [r for r in rows if r.get("secType") == "STK" and float(r.get("qty") or 0) != 0]
    imported: list[dict[str, Any]] = []
    skipped: list[str] = []

    async with db_session() as s:
        # Existing imports
        existing_syms = {
            r[0]
            for r in (
                await s.execute(
                    select(TradeIntent.symbol).where(
                        TradeIntent.entry_strategy == "existing_import"
                    )
                )
            ).all()
        }
        for r in stocks:
            sym = (r.get("symbol") or "").upper()
            if sym in existing_syms:
                skipped.append(sym)
                continue
            qty = float(r["qty"])
            avg = float(r["avg_price"])
            last = float(r.get("last_price") or 0)
            intent = TradeIntent(
                symbol=sym, side="BUY" if qty > 0 else "SELL",
                qty=abs(qty), order_type="MKT",
                limit_px=avg, status="filled",
                structure="stock", position_state="pmcc_full",  # treat as fully held
                entry_strategy="existing_import",
                rationale=(
                    f"Imported pre-existing {sym} position: {qty:.0f} sh @ avg ${avg:.2f}, "
                    f"last ${last:.2f}. Maintenance loop owns this going forward."
                ),
            )
            s.add(intent)
            await s.flush()
            s.add(TradeAuditLog(
                intent_id=intent.id, action="existing_import",
                outcome="imported",
                ibkr_account=None,
                payload={
                    "symbol": sym, "qty": qty, "avg_price": avg, "last_price": last,
                    "secType": r.get("secType"), "currency": r.get("currency"),
                },
            ))
            imported.append({"symbol": sym, "qty": qty, "avg_price": avg, "intent_id": intent.id})
    return {
        "imported": len(imported), "skipped_existing": len(skipped),
        "details": imported,
        "skipped": skipped,
    }


@router.post("/positions/exit/{intent_id}")
async def manual_exit_position(
    intent_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Operator-confirmed close of a held position.

    Stocks: marketable LMT SELL via the same path the maintenance loop
    uses. PMCCs: combo close (SELL LEAP + BUY-TO-CLOSE short) walked at
    net credit through the same walking-limit executor as entries.
    """
    _require_admin(x_admin_token)
    from .db import TradeAuditLog, TradeIntent

    async with db_session() as s:
        intent = await s.get(TradeIntent, intent_id)
        if intent is None:
            raise HTTPException(404, "intent not found")
        if intent.status not in ("filled", "submitted"):
            raise HTTPException(400, f"intent in status {intent.status!r} cannot be exited")
        symbol = intent.symbol
        structure = intent.structure
        qty = int(intent.qty)

    if structure == "stock":
        from .positions import _ibkr
        from .autotrade.maint_loop import _execute_stock_exit
        try:
            ib = await _ibkr()
        except Exception as e:
            raise HTTPException(503, f"IBKR not reachable: {e}")
        async with db_session() as s:
            i = await s.get(TradeIntent, intent_id)
            await _execute_stock_exit(
                intent=i, qty=abs(float(i.qty or 0)),
                reason="operator manual close", exit_kind="operator", ib=ib,
            )
        return {"ok": True, "intent_id": intent_id, "action": "stock_exit_submitted"}

    if structure == "leap_only":
        # LEAPS-only is the PRODUCTION structure, and this endpoint used to fall
        # through to the 400 below for it — so every flag-only exit alert
        # ("close via /api/admin/positions/exit/{id} if you agree") pointed the
        # operator at a route that rejected the entire live book. Sell-to-close
        # the long call through the same walker the maintenance loop uses.
        from .positions import _ibkr
        from .autotrade.maint_loop import _flag_pmcc_close
        try:
            await _ibkr()
        except Exception as e:
            raise HTTPException(503, f"IBKR not reachable: {e}")
        async with db_session() as s:
            i = await s.get(TradeIntent, intent_id)
            if i is None:
                raise HTTPException(404, "intent not found")
            # operator=True: this IS the explicit confirmation, so it bypasses
            # the kill-switch downgrade and the daily close cap — both of which
            # govern AUTONOMOUS exits, not a human pressing the button.
            await _flag_pmcc_close(
                intent=i, reason="operator manual close", kind="operator",
                auto_execute=True, operator=True,
            )
        async with db_session() as s:
            i = await s.get(TradeIntent, intent_id)
            return {"ok": True, "intent_id": intent_id, "action": "leap_close_submitted",
                    "status": i.status if i else None,
                    "position_state": i.position_state if i else None}

    if structure in ("pmcc", "pmcc_sequenced"):
        from .positions import _ibkr
        from tradingagents.strategies.execution import (
            ExecutionConfig, submit_pmcc_combo,
        )
        cfg = dict((intent.walking_config or {}))
        leap_conid = int(cfg.get("leap_conid", 0))
        short_conid = int(cfg.get("short_call_conid", 0))
        if not leap_conid or not short_conid:
            raise HTTPException(500, "intent missing leg conids; cannot auto-close")
        try:
            ib = await _ibkr()
        except Exception as e:
            raise HTTPException(503, f"IBKR not reachable: {e}")
        legs = [
            {"conid": leap_conid,  "ratio": 1, "action": "SELL"},
            {"conid": short_conid, "ratio": 1, "action": "BUY"},
        ]
        async with db_session() as s:
            s.add(TradeAuditLog(
                intent_id=intent_id, action="manual_pmcc_close_attempt",
                payload={"symbol": symbol, "legs": legs, "qty": qty},
            ))
        result = await submit_pmcc_combo(
            ibkr=ib, symbol=symbol, legs=legs, contracts=qty,
            config=ExecutionConfig(
                initial_offset_cents=1, walk_increment_cents=1,
                walk_interval_sec=20, max_offset_pct_of_spread=0.50,
                timeout_sec=180,
            ),
        )
        async with db_session() as s:
            i = await s.get(TradeIntent, intent_id)
            if i:
                i.status = "closing" if result.status == "filled" else "abandoned"
                i.position_state = "closing"
            s.add(TradeAuditLog(
                intent_id=intent_id, action="manual_pmcc_close_outcome",
                outcome=result.status,
                payload={**result.to_dict(), "error": result.error},
            ))
        return {"ok": True, "intent_id": intent_id, "execution": result.to_dict()}

    raise HTTPException(400, f"unknown structure {structure!r}")


@router.post("/positions/flatten-all")
async def flatten_all(
    body: KillSwitchToggle,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """EMERGENCY STOP: (1) flip the kill switch OFF, (2) global-cancel all
    working orders, (3) submit marketable closes for EVERY live position.

    Bypasses the trading kill switch deliberately — this IS the emergency exit.
    Long option positions close via the single-leg walker (Urgent); stocks via a
    marketable SELL. Best-effort per position; returns a per-symbol summary."""
    _require_admin(x_admin_token)
    from .positions import _ibkr
    from tradingagents.strategies.execution import ExecutionConfig, submit_single_leg_option
    from tradingagents.dataflows.providers.base import TradeIntent as IntentDC

    # 1. Kill switch OFF (stops the autonomous loops from placing anything new).
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is not None:
            state.autotrade_enabled = False
            state.last_kill_at = datetime.now(timezone.utc)
            state.kill_reason = f"FLATTEN-ALL by {body.actor}: {body.reason or '(none)'}"
            state.updated_by = body.actor
    await alert(level="critical", title="FLATTEN-ALL invoked",
                body=f"by={body.actor}; reason={body.reason or '(none)'} — cancelling orders + closing all positions")

    try:
        ib = await _ibkr()
    except Exception as e:
        raise HTTPException(503, f"IBKR not reachable: {e}")

    # 2. Global-cancel every working order.
    cancelled = False
    try:
        ib_inst = await ib._ensure_connected()
        ib_inst.reqGlobalCancel()
        cancelled = True
    except Exception as e:
        logger.warning("flatten-all: reqGlobalCancel failed: %s", e)

    # 3. Close every live position.
    positions = await ib.get_positions()
    closed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for p in positions:
        sym = (p.get("symbol") or "").upper()
        qty = float(p.get("qty") or 0)
        if qty == 0:
            continue
        sec = str(p.get("secType") or p.get("sec_type") or "").upper()
        conid = int(p.get("conid") or 0)
        try:
            if sec == "OPT" and conid and qty > 0:
                r = await submit_single_leg_option(
                    ibkr=ib, conid=conid, contracts=int(abs(qty)), action="SELL",
                    config=ExecutionConfig(walk_interval_sec=15, max_offset_pct_of_spread=0.60,
                                           timeout_sec=120),
                    adaptive_priority="Urgent")
                closed.append({"symbol": sym, "conid": conid, "qty": qty, "status": r.status})
            elif sec == "STK":
                side = "SELL" if qty > 0 else "BUY"
                r2 = await ib.submit_trade(IntentDC(
                    ticker=sym, side=side, qty=int(abs(qty)), order_type="MKT",
                    tif="DAY", account_mode=get_settings().IBKR_MODE))
                closed.append({"symbol": sym, "qty": qty, "status": r2.get("status")})
        except Exception as e:
            errors.append({"symbol": sym, "error": str(e)})
            logger.exception("flatten-all: close failed for %s: %s", sym, e)

    return {"ok": True, "kill_switch": "disabled", "orders_cancelled": cancelled,
            "closed": closed, "errors": errors, "count": len(closed)}


@router.get("/positions/managed")
async def list_managed_positions(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> list[dict[str, Any]]:
    """All open intents the system is managing — used by the kill-switch UI."""
    _require_admin(x_admin_token)
    from .db import TradeIntent
    async with db_session() as s:
        rows = (
            await s.execute(
                select(TradeIntent)
                .where(TradeIntent.status.in_(["filled", "submitted"]))
                # Must match the maintenance loop's active-state set. 'leap_open'
                # (the filled bare-LEAP state, i.e. the entire LEAPS-only book)
                # and 'leap_open_naked'/'closing' were missing, so the kill-switch
                # UI rendered an EMPTY position list on a fully-invested account —
                # the worst possible time to believe you hold nothing.
                .where(TradeIntent.position_state.in_(
                    ["pmcc_full", "leap_pending", "leap_open", "leap_open_naked", "closing"]))
                .order_by(TradeIntent.created_at.desc())
            )
        ).scalars().all()
        return [
            {
                "id": i.id, "symbol": i.symbol, "structure": i.structure,
                "qty": i.qty, "status": i.status, "position_state": i.position_state,
                "leap_strike": i.leap_strike, "leap_expiry": i.leap_expiry,
                "short_call_strike": i.short_call_strike, "short_call_expiry": i.short_call_expiry,
                "net_debit_filled": i.net_debit_filled,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in rows
        ]


@router.post("/reconcile")
async def trigger_reconcile(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Manually trigger full-account reconciliation. Useful after a manual
    operator trade, or to verify state before re-enabling autotrade."""
    _require_admin(x_admin_token)
    from .autotrade.reconcile import reconcile_all
    from .positions import _ibkr  # type: ignore[import-not-found]
    try:
        ib = await _ibkr()
    except Exception as e:
        raise HTTPException(503, f"IBKR not reachable: {e}")
    async with db_session() as s:
        result = await reconcile_all(s, ibkr_provider=ib)
    return {
        "ok": result.ok,
        "summary": result.summary,
        "mismatches": [
            {"symbol": m.symbol, "field": m.field, "db": m.db, "ibkr": m.ibkr}
            for m in result.mismatches
        ],
    }


# ---------------------------------------------------------------------------
# Closing-Bell Accumulation
# ---------------------------------------------------------------------------


@router.post("/closing-accumulation/run-now")
async def closing_accumulation_run_now(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Trigger the closing-bell accumulation sweep across all theme symbols.

    Persists one row per (symbol, today's-date) into
    closing_accumulation_signals. Returns a summary + the high/medium
    confidence setups (full results live in the table).

    Designed for the ~15:50-16:30 ET window. Outside that window the
    AH metrics will come back empty (gate 2 fails) and most signals
    will be no-confidence — which is correct, not an error.
    """
    _require_admin(x_admin_token)
    from .autotrade.maint_loop import run_closing_accumulation_sweep
    result = await run_closing_accumulation_sweep()
    return result


@router.get("/closing-accumulation/today")
async def closing_accumulation_today(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    min_confidence: str = "medium",
) -> dict[str, Any]:
    """Read today's CBA signals (persisted by the most recent sweep).

    ``min_confidence`` filters the result list. Use 'low' to see
    near-misses, 'none' to see everything including failures.
    """
    _require_admin(x_admin_token)
    from datetime import date as _date
    today_dt = datetime(_date.today().year, _date.today().month,
                        _date.today().day, tzinfo=timezone.utc)
    from .db import ClosingAccumulationSignal
    confidence_order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    threshold = confidence_order.get(min_confidence.lower(), 2)
    async with db_session() as s:
        rows = (
            await s.execute(
                select(ClosingAccumulationSignal)
                .where(ClosingAccumulationSignal.date == today_dt)
                .order_by(ClosingAccumulationSignal.confidence.desc())
            )
        ).scalars().all()
    out = []
    for r in rows:
        if confidence_order.get(r.confidence, 0) < threshold:
            continue
        out.append({
            "symbol": r.symbol,
            "theme_id": r.theme_id,
            "confidence": r.confidence,
            "setup_passes": r.setup_passes,
            "gate1_passes": r.gate1_passes,
            "gate2_passes": r.gate2_passes,
            "theme_confirmed": r.theme_confirmed,
            "last_30m_rvol": r.last_30m_rvol,
            "day_rvol": r.day_rvol,
            "pct_session_above_vwap": r.pct_session_above_vwap,
            "ah_print_count": r.ah_print_count,
            "ah_holds_close": r.ah_holds_close,
            "vwap": r.vwap,
            "moc_price": r.moc_price,
            "entry_recommendation": r.entry_recommendation,
            "rationale": r.rationale,
            "failure_filters": r.failure_filters,
        })
    return {"date": today_dt.date().isoformat(), "count": len(out),
            "signals": out}


# ---------------------------------------------------------------------------
# Quant Research Factory — manual feature snapshot / label triggers
# ---------------------------------------------------------------------------


@router.post("/features/snapshot-now")
async def features_snapshot_now(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    with_market: bool = True,
    with_flow: bool = True,
) -> dict[str, Any]:
    """Compute + persist a point-in-time feature snapshot for the whole
    universe right now. Graph + cross-theme features always populate; market /
    flow are best-effort (toggle off to snapshot instantly without provider
    calls). Idempotent per (symbol, today). Research-only — no gate reads it."""
    _require_admin(x_admin_token)
    from .research.features import build_snapshot
    return await build_snapshot(with_market=with_market, with_flow=with_flow)


@router.post("/features/label-now")
async def features_label_now(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Backfill realised forward-return labels onto every snapshot whose
    forward window has elapsed. Idempotent."""
    _require_admin(x_admin_token)
    from .research.labeler import run_labeler
    return await run_labeler()
