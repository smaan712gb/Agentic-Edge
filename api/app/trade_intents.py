"""Trade-intent endpoints: build, preview, submit, cancel.

The flow:
  1. Run finishes → scorecard says BUY on a symbol.
  2. User clicks **Build PMCC** in the UI on the Run detail page.
     →  POST /api/trade-intents/{run_id}/{symbol}/build-pmcc
        Probes the IBKR chain, picks legs, persists a TradeIntent
        with status="pending_review" and the leg/financials filled.
  3. UI shows the legs + financials + walking config.
  4. User clicks **Submit to IBKR** when ready.
     →  POST /api/trade-intents/{intent_id}/submit
        Runs the auto-gate stack, then the walking-limit executor.
        Updates the intent with order_id, fill price, status.
  5. (or) **Cancel** to discard a pending review.
     →  POST /api/trade-intents/{intent_id}/cancel

Submit is the only path that sends to IBKR. All paths write
``trade_audit_log`` rows with intent + outcome.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .autotrade.auto_gate import check_auto_action, record_auto_action
from .config import get_settings
from .db import (
    AutoAction,
    Run,
    TickerScore,
    TradeAuditLog,
    TradeIntent,
    get_session as db_session,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trade-intents", tags=["trade-intents"])


# ---------------------------------------------------------------------------
# Pydantic
# ---------------------------------------------------------------------------


class BuildPmccIn(BaseModel):
    contracts: int = Field(1, ge=1, le=20, description="Number of PMCC spreads")
    leap_delta_target:  float = Field(0.90, ge=0.70, le=0.95)
    short_delta_target: float = Field(0.25, ge=0.10, le=0.40)


# ---------------------------------------------------------------------------
# Build (probes IBKR, persists pending_review intent)
# ---------------------------------------------------------------------------


@router.post("/{run_id}/{symbol}/build-pmcc", status_code=201)
async def build_pmcc(run_id: str, symbol: str, body: BuildPmccIn) -> dict[str, Any]:
    """Probe IBKR option chain, select PMCC legs, persist intent for review."""
    sym = symbol.strip().upper()

    # Validate the run + score exist
    async with db_session() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        score = (
            await s.execute(
                select(TickerScore)
                .where(TickerScore.run_id == run_id)
                .where(TickerScore.symbol == sym)
            )
        ).scalars().first()
        if score is None:
            raise HTTPException(404, f"no score for {sym} in run {run_id}")
        if score.decision != "Buy":
            raise HTTPException(
                400,
                f"refusing to build PMCC for non-Buy decision ({score.decision})",
            )
        theme_id = run.theme_id
        score_rationale = score.rationale

    # IBKR option-chain probe + leg selection
    from .positions import _ibkr
    from tradingagents.strategies.pmcc import select_pmcc_legs

    try:
        ib = await _ibkr()
    except Exception as e:
        raise HTTPException(503, f"IBKR not reachable: {e}")

    elig = await select_pmcc_legs(
        symbol=sym, contracts=body.contracts, ibkr=ib,
        leap_delta_target=body.leap_delta_target,
        short_delta_target=body.short_delta_target,
    )
    if not elig.eligible or elig.candidate is None:
        raise HTTPException(422, f"PMCC ineligible: {elig.reason}")
    cand = elig.candidate

    # Persist as pending_review intent. Same conservative cap as the
    # auto-entry loop: operator-built PMCCs are entries, and entries can
    # afford to abandon and re-try; better to skip a thin name than pay
    # 40% of spread above mid. Operator can override walking_cfg per-build
    # on the preview screen if they want a different posture.
    walking_cfg = {
        "initial_offset_cents": 1, "walk_increment_cents": 1,
        "walk_interval_sec": 30, "max_offset_pct_of_spread": 0.30,
        "timeout_sec": 300,
    }
    async with db_session() as s:
        intent = TradeIntent(
            run_id=run_id, symbol=sym,
            side="BUY", qty=body.contracts, order_type="LMT",
            status="pending_review",
            structure="pmcc", position_state="pending",
            entry_strategy="combo",
            leap_expiry=cand.leap.expiry, leap_strike=cand.leap.strike,
            leap_delta_target=body.leap_delta_target, leap_delta_actual=cand.leap.delta,
            leap_iv=cand.leap.iv, leap_open_interest=cand.leap.open_interest,
            leap_qty=body.contracts,
            short_call_expiry=cand.short_call.expiry, short_call_strike=cand.short_call.strike,
            short_call_delta_target=body.short_delta_target,
            short_call_delta_actual=cand.short_call.delta,
            short_call_iv=cand.short_call.iv, short_call_open_interest=cand.short_call.open_interest,
            short_call_qty=body.contracts,
            net_debit_target=cand.net_debit,
            max_loss=cand.max_loss,
            walking_config=walking_cfg,
            rationale=f"{score_rationale or ''}\n\n{cand.rationale}".strip(),
            ibkr_combo_conid=None,
        )
        # Stash conids on the intent for the executor — packed in walking_config so
        # we don't add another column right now.
        walking_cfg["leap_conid"] = cand.leap.conid
        walking_cfg["short_call_conid"] = cand.short_call.conid
        walking_cfg["spot_at_build"] = cand.spot
        intent.walking_config = walking_cfg
        s.add(intent)
        await s.flush()
        intent_id = intent.id
        dto = _intent_to_dto(intent)
    return dto


# ---------------------------------------------------------------------------
# Submit (runs gate stack, then walking-limit executor)
# ---------------------------------------------------------------------------


@router.post("/{intent_id}/submit")
async def submit_intent(intent_id: str) -> dict[str, Any]:
    """Run the auto-gate, then submit the combo to IBKR via walking limit."""
    settings = get_settings()
    async with db_session() as s:
        intent = await s.get(TradeIntent, intent_id)
        if intent is None:
            raise HTTPException(404, "intent not found")
        if intent.status != "pending_review":
            raise HTTPException(400, f"intent in status {intent.status!r} cannot be submitted")
        if intent.structure != "pmcc":
            raise HTTPException(400, f"submit endpoint expects PMCC; got {intent.structure!r}")

        # Pull the snapshot we need; close the session before the long IBKR call.
        run = await s.get(Run, intent.run_id) if intent.run_id else None
        theme_id = run.theme_id if run else None
        symbol = intent.symbol
        contracts = int(intent.qty)
        cfg = dict(intent.walking_config or {})
        leap_conid = int(cfg.pop("leap_conid", 0))
        short_conid = int(cfg.pop("short_call_conid", 0))
        if not leap_conid or not short_conid:
            raise HTTPException(500, "intent is missing leg conids; rebuild PMCC")

    # Auto-gate (kill switch + universe + budget + sector regime)
    async with db_session() as s:
        gate = await check_auto_action(
            s, loop="entry", action_type="open_pmcc",
            symbol=symbol, theme_id=theme_id,
            estimated_capital_pct=0.0, is_new_entry=True, nav=0.0,
        )
        if not gate.passed:
            await record_auto_action(
                s, loop="entry", action_type="open_pmcc",
                gate_result=gate, symbol=symbol, intent_id=intent_id,
            )
            first = gate.failures[0]
            return {
                "status": "gate_rejected",
                "intent_id": intent_id,
                "gate": first.gate, "reason": first.reason,
                "detail": first.detail,
            }
        await record_auto_action(
            s, loop="entry", action_type="open_pmcc_gate_passed",
            gate_result=gate, symbol=symbol, intent_id=intent_id,
        )

    # Audit "submit_attempt" before the broker call.
    async with db_session() as s:
        audit_attempt = TradeAuditLog(
            intent_id=intent_id, action="submit_attempt",
            payload={"symbol": symbol, "contracts": contracts,
                     "leap_conid": leap_conid, "short_call_conid": short_conid,
                     "walking_config": cfg},
        )
        s.add(audit_attempt)
        # Update intent state
        i = await s.get(TradeIntent, intent_id)
        if i:
            i.position_state = "leap_pending"
            i.status = "submitting"

    # Connect IBKR + run executor
    from .positions import _ibkr
    from tradingagents.strategies.execution import (
        ExecutionConfig, submit_pmcc_combo,
    )
    try:
        ib = await _ibkr()
    except Exception as e:
        async with db_session() as s:
            i = await s.get(TradeIntent, intent_id)
            if i:
                i.status = "error"
            s.add(TradeAuditLog(
                intent_id=intent_id, action="submit_outcome",
                outcome="ibkr_unreachable", error=str(e),
            ))
        raise HTTPException(503, f"IBKR not reachable: {e}")

    legs = [
        {"conid": leap_conid,        "ratio": 1, "action": "BUY"},
        {"conid": short_conid,       "ratio": 1, "action": "SELL"},
    ]
    exec_cfg = ExecutionConfig(
        initial_offset_cents=cfg.get("initial_offset_cents", 1),
        walk_increment_cents=cfg.get("walk_increment_cents", 1),
        walk_interval_sec=cfg.get("walk_interval_sec", 30),
        # Default 0.30 of half-spread (was 0.50): keep the operator path
        # aligned with the auto-entry path's conservative cap. Per-intent
        # overrides via walking_config still work.
        max_offset_pct_of_spread=cfg.get("max_offset_pct_of_spread", 0.30),
        timeout_sec=cfg.get("timeout_sec", 300),
    )
    result = await submit_pmcc_combo(
        ibkr=ib, symbol=symbol, legs=legs, contracts=contracts, config=exec_cfg,
    )

    # Persist result
    async with db_session() as s:
        i = await s.get(TradeIntent, intent_id)
        if i is None:
            return result.to_dict()
        if result.status == "filled":
            i.status = "filled"
            i.position_state = "pmcc_full"
            i.net_debit_filled = result.fill_price
            if result.fill_price and contracts:
                i.max_loss = round(result.fill_price * contracts * 100, 2)
            i.leap_filled_at = datetime.now(timezone.utc)
            i.short_call_filled_at = datetime.now(timezone.utc)
            i.ibkr_order_id = str(result.order_id) if result.order_id else None
        elif result.status == "abandoned":
            i.status = "abandoned"
            i.position_state = "abandoned"
        elif result.status == "rejected_pretrade":
            i.status = "rejected"
            i.position_state = "abandoned"
        else:
            i.status = "error"
            i.position_state = "abandoned"
        s.add(TradeAuditLog(
            intent_id=intent_id, action="submit_outcome",
            outcome=result.status, ibkr_account=None,
            payload=result.to_dict(),
            error=result.error,
        ))
    return {"status": result.status, "intent_id": intent_id, "execution": result.to_dict()}


# ---------------------------------------------------------------------------
# Cancel pending review
# ---------------------------------------------------------------------------


@router.post("/{intent_id}/cancel")
async def cancel_intent(intent_id: str) -> dict[str, Any]:
    async with db_session() as s:
        i = await s.get(TradeIntent, intent_id)
        if i is None:
            raise HTTPException(404, "intent not found")
        if i.status not in ("pending_review", "pending"):
            raise HTTPException(400, f"intent in status {i.status!r} cannot be cancelled")
        i.status = "cancelled"
        i.position_state = "cancelled"
        s.add(TradeAuditLog(
            intent_id=intent_id, action="cancel_review",
            outcome="cancelled",
        ))
    return {"status": "cancelled", "intent_id": intent_id}


# ---------------------------------------------------------------------------
# List by run (frontend pulls these to render Build buttons + previews)
# ---------------------------------------------------------------------------


@router.get("/by-run/{run_id}")
async def list_by_run(run_id: str) -> list[dict[str, Any]]:
    async with db_session() as s:
        rows = (
            await s.execute(
                select(TradeIntent).where(TradeIntent.run_id == run_id)
                .order_by(TradeIntent.created_at.desc())
            )
        ).scalars().all()
        return [_intent_to_dto(r) for r in rows]


@router.get("/{intent_id}")
async def get_intent(intent_id: str) -> dict[str, Any]:
    async with db_session() as s:
        i = await s.get(TradeIntent, intent_id)
        if i is None:
            raise HTTPException(404, "intent not found")
        return _intent_to_dto(i)


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


def _intent_to_dto(i: TradeIntent) -> dict[str, Any]:
    return {
        "id": i.id, "run_id": i.run_id, "symbol": i.symbol,
        "side": i.side, "qty": i.qty,
        "status": i.status, "structure": i.structure,
        "position_state": i.position_state,
        "entry_strategy": i.entry_strategy,
        "leap": _leg_dto(i, "leap"),
        "short_call": _leg_dto(i, "short_call"),
        "net_debit_target": i.net_debit_target,
        "net_debit_cap":    i.net_debit_cap,
        "net_debit_filled": i.net_debit_filled,
        "max_loss": i.max_loss,
        "walking_config": _scrub_conids(i.walking_config),
        "rationale": i.rationale,
        "ibkr_order_id": i.ibkr_order_id,
        "created_at": i.created_at.isoformat() if i.created_at else None,
        "updated_at": i.updated_at.isoformat() if i.updated_at else None,
    }


def _leg_dto(i: TradeIntent, prefix: str) -> dict[str, Any]:
    return {
        "expiry":         getattr(i, f"{prefix}_expiry"),
        "strike":         getattr(i, f"{prefix}_strike"),
        "delta_target":   getattr(i, f"{prefix}_delta_target"),
        "delta_actual":   getattr(i, f"{prefix}_delta_actual"),
        "iv":             getattr(i, f"{prefix}_iv"),
        "open_interest":  getattr(i, f"{prefix}_open_interest"),
        "qty":            getattr(i, f"{prefix}_qty"),
        "filled_at":      getattr(i, f"{prefix}_filled_at").isoformat() if getattr(i, f"{prefix}_filled_at") else None,
        "fill_price":     getattr(i, f"{prefix}_fill_price"),
    }


def _scrub_conids(cfg: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Don't expose the IBKR conids over the public API — they're internal
    plumbing the executor uses; the UI just needs the human-readable strikes."""
    if not cfg:
        return cfg
    return {k: v for k, v in cfg.items() if k not in ("leap_conid", "short_call_conid")}
