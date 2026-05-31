"""Auto-entry loop — Phase C.

Runs as a background task in the FastAPI lifespan. Every poll interval:

  1. If AUTOTRADE not enabled (env + DB), do nothing.
  2. If outside RTH or in the first 2-minute candle, do nothing.
  3. Find completed runs from today that have Buy decisions we haven't
     already acted on (no TradeIntent exists for that run+symbol).
  4. For each candidate, in score order (highest composite first):
        a. Run the auto-gate stack (kill switch + universe + caps + regime).
        b. If passed: build the PMCC intent (probes IBKR chain).
        c. If build succeeds: submit it via walking-limit executor.
        d. Audit each step regardless of outcome.

Caps from auto_gate apply across all loops, so this can't blow through
the daily new-entry cap or per-symbol throttle.

Single-instance assumption: one process owns the loop. For multi-instance
deployments, externalize via a cron hitting a `/api/admin/entry-loop/tick`
endpoint and disable the in-process task.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.config import get_settings
from api.app.db import (
    Run,
    SystemState,
    TickerScore,
    TradeIntent,
    get_session as db_session,
)

from .alerts import alert
from .auto_gate import check_auto_action, record_auto_action
from .market_conditions import gate_open_auction_block, gate_rth

logger = logging.getLogger("agentic_edge.entry_loop")


_TASK: Optional[asyncio.Task] = None
_POLL_INTERVAL_SEC = 60


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def start_entry_loop() -> None:
    """Spawn the background task. Idempotent."""
    global _TASK
    if _TASK and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop_forever(), name="entry_loop")
    logger.info("auto-entry loop started (poll=%ds)", _POLL_INTERVAL_SEC)


async def stop_entry_loop() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None


async def _loop_forever() -> None:
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("entry_loop tick failed: %s", e)
        await asyncio.sleep(_POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# One iteration
# ---------------------------------------------------------------------------


STUCK_INTENT_TIMEOUT_MIN = 10  # an intent in 'submitting' status for longer than this is stuck


async def _tick() -> None:
    settings = get_settings()
    # Watchdog: any TradeIntent stuck in 'submitting' beyond the timeout
    # gets marked abandoned. Walks-limit hangs (rare but real — IBKR
    # didn't ack, network glitch mid-walk) shouldn't block the loop on
    # subsequent ticks.
    await _resolve_stuck_intents()

    if not settings.AUTOTRADE_ENABLED:
        return  # env-level kill switch
    # DB-level kill switch
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is None or not state.autotrade_enabled:
            return

    # Time-of-day gates (RTH + first 2-min candle).
    if (gate_rth() is not None) or (gate_open_auction_block() is not None):
        return

    # Account-level circuit breaker — halts NEW entries (never closes
    # positions) on a severe breach: intraday NAV drop, thin margin cushion,
    # or a blind/stale broker. Latches until manually re-armed. The exit loop
    # is unaffected; open high-beta positions keep exiting on their own
    # signals, not on a down day.
    try:
        from api.app.positions import _ibkr
        from .circuit_breaker import check_entry_breaker
        ib_for_breaker = await _ibkr()
        halt_reason = await check_entry_breaker(ib_for_breaker)
        if halt_reason is not None:
            logger.warning("auto-entry: circuit breaker halts new entries — %s", halt_reason)
            return
    except Exception as e:
        # Fail closed: if we can't evaluate account health, don't open new
        # positions this tick.
        logger.warning("auto-entry: breaker check errored (%s) — skipping new entries this tick", e)
        return

    # Pull today's completed runs and queue Buy decisions we haven't acted on.
    candidates = await _find_unprocessed_buys()
    if not candidates:
        return

    # Macro regime read once per tick. Tightens NAV sizing in elevated /
    # defensive regimes; blocks new entries entirely in panic.
    sizing_factor = 1.0
    macro_regime = "calm"
    try:
        from api.app.positions import _ibkr
        from tradingagents.strategies.macro_regime import get_macro_regime
        ib = await _ibkr()
        macro = await get_macro_regime(ib)
        sizing_factor = macro.sizing_factor
        macro_regime = macro.regime
        logger.info(
            "auto-entry: macro=%s sizing_factor=%.2f (VIX=%s, SPX=%s)",
            macro.regime, sizing_factor,
            f"{macro.vix_last:.1f}" if macro.vix_last else "?",
            f"{macro.spx_change_pct*100:+.2f}%" if macro.spx_change_pct else "?",
        )
    except Exception as e:
        logger.debug("entry_loop macro fetch failed: %s — using sizing_factor=1.0", e)

    if sizing_factor <= 0:
        logger.warning("auto-entry: macro=%s blocks new entries (sizing_factor=0)", macro_regime)
        async with db_session() as s:
            from .auto_gate import record_auto_action
            await record_auto_action(
                s, loop="entry", action_type=f"entry_blocked_macro_{macro_regime}",
                gate_result=None,
                payload={"regime": macro_regime, "sizing_factor": 0.0},
                outcome="blocked",
            )
        return

    for run_id, theme_id, symbol, composite in candidates:
        ok = await _process_one(run_id, theme_id, symbol, composite,
                                sizing_factor=sizing_factor,
                                macro_regime=macro_regime)
        if not ok:
            # Most rejections are gate-driven (rate limit, regime, etc.).
            # Don't slam the rest of the queue if the cap is hit.
            if await _daily_cap_exhausted():
                logger.info("auto-entry: daily cap exhausted, sleeping until next tick")
                return


async def _find_unprocessed_buys() -> list[tuple[str, str, str, float]]:
    """Return (run_id, theme_id, symbol, composite) tuples for un-traded Buy
    decisions on runs that finished today. Sorted by composite desc.

    Skips symbols where:
      * a TradeIntent already exists for (run_id, symbol) — already routed
      * the loop already attempted any open_* on this symbol today

    Does NOT skip names already held in the IBKR account: the operator
    explicitly wants the system to *stack* PMCC + existing stock when the
    thesis is working (momentum amplification — long stock + long LEAP +
    short call = bigger participation when right, defined risk on the
    LEAP leg).
    """
    from api.app.db import AutoAction

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    async with db_session() as s:
        rows = (
            await s.execute(
                select(
                    TickerScore.run_id, Run.theme_id, TickerScore.symbol,
                    TickerScore.composite,
                )
                .join(Run, Run.id == TickerScore.run_id)
                .where(Run.status == "done")
                .where(Run.finished_at >= today)
                .where(TickerScore.decision == "Buy")
                .order_by(TickerScore.composite.desc())
            )
        ).all()

        # Already submitted (any TradeIntent for this run+symbol).
        existing = {
            (i.run_id, i.symbol)
            for i in (
                await s.execute(
                    select(TradeIntent).where(TradeIntent.run_id.in_({r[0] for r in rows}))
                )
            ).scalars().all()
        }

        # Already-attempted-today symbols (any outcome from today's loop runs).
        # We treat any ``open_*`` action as "we processed this symbol today"
        # so a symbol that hit stock-fallback or filled doesn't get re-tried
        # the next tick. Bound to today so tomorrow's run starts fresh.
        attempted_today = {
            r[0]
            for r in (
                await s.execute(
                    select(AutoAction.symbol)
                    .where(AutoAction.timestamp >= today)
                    .where(AutoAction.symbol.is_not(None))
                    .where(AutoAction.action_type.like("open_%"))
                    .where(AutoAction.gate_status.in_(["passed", "error"]))
                )
            ).all()
        }

        return [
            (r[0], r[1], r[2], float(r[3] or 0))
            for r in rows
            if (r[0], r[2]) not in existing and r[2] not in attempted_today
        ]


async def _process_one(
    run_id: str, theme_id: str, symbol: str, composite: float,
    *, sizing_factor: float = 1.0, macro_regime: str = "calm",
) -> bool:
    """Run the gate, build the PMCC, submit it. Returns True if a trade was
    placed (or attempted), False if rejected upstream."""

    # --- Gate stack ----
    async with db_session() as s:
        gate = await check_auto_action(
            s, loop="entry", action_type="open_pmcc",
            symbol=symbol, theme_id=theme_id,
            estimated_capital_pct=0.0, is_new_entry=True,
        )
        if not gate.passed:
            await record_auto_action(
                s, loop="entry", action_type="open_pmcc",
                gate_result=gate, symbol=symbol,
                payload={"run_id": run_id, "composite": composite},
            )
            f = gate.first_reject()
            logger.info("auto-entry rejected for %s: %s — %s",
                        symbol, f.gate if f else "?", f.reason if f else "")
            return False
        # Note: deliberately NOT writing a "gate_passed" record here.
        # The budget gate counts ``gate_status == "passed"`` rows; an
        # extra row would double-count and prematurely trip the daily
        # caps. We record exactly one outcome row at the end of the
        # attempt (ineligible / filled / abandoned / error).

    # --- Build PMCC ---- (mirrors trade_intents.build_pmcc but in-process)
    from api.app.positions import _ibkr
    from tradingagents.strategies.pmcc import select_pmcc_legs

    try:
        ib = await _ibkr()
    except Exception as e:
        await alert(level="warning", title="auto-entry: IBKR unreachable",
                    body=f"{symbol} skipped — {e}")
        return False

    # 1 contract for math; we re-size below based on NAV.
    # Wrap eligibility probe — symbols without listed options (ADRs like
    # ABBNY, BESIY) raise ProviderError here. That's an "ineligible name",
    # not a system error; treat it as ineligible and move on so one bad
    # symbol doesn't crash the entire entry tick.
    try:
        elig = await select_pmcc_legs(symbol=symbol, contracts=1, ibkr=ib)
    except Exception as e:
        logger.info("auto-entry: %s PMCC probe failed — %s", symbol, e)
        async with db_session() as s:
            await record_auto_action(
                s, loop="entry", action_type="open_pmcc_ineligible",
                gate_result=gate, symbol=symbol,
                payload={"run_id": run_id, "reason": f"probe_error: {e}"},
                outcome="ineligible",
            )
        return True  # treat as processed; don't retry this symbol today
    if not elig.eligible or elig.candidate is None:
        # PMCC failed eligibility. For high-conviction names (composite ≥ 7),
        # fall back to a small stock buy — better to participate at reduced
        # size than miss a momentum-thesis play because the options book is
        # thin. Liquidity-driven failures are the typical case here (low LEAP
        # OI, wide short-call spread); other failure modes (no chain, bad
        # symbol) we still skip.
        is_liquidity_fail = any(
            kw in (elig.reason or "").lower()
            for kw in ("oi", "spread", "credit", "no leap candidate", "no short call")
        )
        if composite >= 7.0 and is_liquidity_fail:
            logger.info(
                "auto-entry: %s PMCC ineligible (%s) — falling back to stock",
                symbol, elig.reason,
            )
            return await _try_stock_fallback(
                run_id=run_id, theme_id=theme_id, symbol=symbol,
                composite=composite, gate=gate, pmcc_reason=elig.reason, ib=ib,
                sizing_factor=sizing_factor,
            )
        logger.info("auto-entry: %s ineligible — %s", symbol, elig.reason)
        async with db_session() as s:
            # Ineligibility = "this name doesn't fit", not a system error.
            # Do NOT pass error= here, or record_auto_action marks the row
            # gate_status='error' which would trip the circuit breaker on
            # 3 consecutive ineligible names.
            await record_auto_action(
                s, loop="entry", action_type="open_pmcc_ineligible",
                gate_result=gate, symbol=symbol,
                payload={"run_id": run_id, "reason": elig.reason},
                outcome="ineligible",
            )
        return False
    cand = elig.candidate

    # Manager-conviction tilt: names tracked legendary investors hold with
    # cross-fund confirmation get a bounded size boost (never a gate). Neutral
    # (1.0) for untracked names.
    conviction_factor, conviction_meta = await _manager_conviction(symbol)

    # NAV-aware sizing: target ~7% NAV per spread × macro sizing_factor ×
    # conviction, capped at $250k absolute.
    nav = await _fetch_nav(ib)
    n_contracts = _size_pmcc_contracts(
        net_debit_per_spread=cand.net_debit, nav=nav,
        sizing_factor=sizing_factor, conviction_factor=conviction_factor,
    )
    if n_contracts <= 0:
        logger.info("auto-entry: %s sized to 0 contracts (macro=%s blocks)",
                    symbol, macro_regime)
        return False
    total_debit = round(cand.net_debit * n_contracts * 100, 2)
    logger.info(
        "auto-entry: %s sized to %d contracts (NAV=$%.0f × sf=%.2f × conv=%.2f, debit/spread=$%.2f, total=$%.0f, macro=%s%s)",
        symbol, n_contracts, nav, sizing_factor, conviction_factor, cand.net_debit, total_debit, macro_regime,
        (f", smart-money={conviction_meta.get('manager_count')}mgr" if conviction_meta.get("matched") else ""),
    )

    # Persist intent. Keep this dict in sync with the ExecutionConfig
    # constructed below — they're the same config in two forms (audit JSON
    # vs dataclass). 5¢ steps because options >$3 have a 5¢ exchange min
    # tick, so 1¢ walks were no-ops.
    #
    # Cap of 0.30 of half-spread (was 0.60): entries are the discretionary
    # leg of every trade — we'd rather abandon a thin name and re-try next
    # tick than pay 40% of spread above mid. Exits and rolls stay at 0.50
    # in maint_loop because exits can't afford to abandon. The thin-combo
    # path inside walking_limit.py separately caps its auto-widened cap at
    # 0.50 so it can't push beyond the operator's intent.
    walking_cfg = {
        "initial_offset_cents": 5, "walk_increment_cents": 5,
        "walk_interval_sec": 30, "max_offset_pct_of_spread": 0.30,
        "timeout_sec": 300,
        "leap_conid": cand.leap.conid, "short_call_conid": cand.short_call.conid,
        "spot_at_build": cand.spot, "auto_origin": "entry_loop",
        "nav_at_build": nav, "target_pct_nav": PMCC_TARGET_PCT_NAV,
        "conviction_factor": conviction_factor, "manager_conviction": conviction_meta,
    }
    async with db_session() as s:
        intent = TradeIntent(
            run_id=run_id, symbol=symbol, side="BUY", qty=n_contracts,
            order_type="LMT", status="submitting",
            structure="pmcc", position_state="leap_pending",
            entry_strategy="combo",
            leap_expiry=cand.leap.expiry, leap_strike=cand.leap.strike,
            leap_delta_actual=cand.leap.delta, leap_iv=cand.leap.iv,
            leap_open_interest=cand.leap.open_interest, leap_qty=n_contracts,
            short_call_expiry=cand.short_call.expiry,
            short_call_strike=cand.short_call.strike,
            short_call_delta_actual=cand.short_call.delta,
            short_call_iv=cand.short_call.iv,
            short_call_open_interest=cand.short_call.open_interest,
            short_call_qty=n_contracts,
            net_debit_target=cand.net_debit, max_loss=total_debit,
            walking_config=walking_cfg, rationale=cand.rationale,
        )
        s.add(intent)
        await s.flush()
        intent_id = intent.id

    await alert(
        level="info",
        title=f"Auto-submit PMCC: {symbol}",
        body=f"LEAP ${cand.leap.strike:.0f} {cand.leap.expiry} + short ${cand.short_call.strike:.0f} {cand.short_call.expiry} · net debit ${cand.net_debit:.2f}",
    )

    # --- Submit ----
    from tradingagents.strategies.execution import (
        ExecutionConfig, submit_pmcc_combo,
    )
    legs = [
        {"conid": cand.leap.conid,        "ratio": 1, "action": "BUY"},
        {"conid": cand.short_call.conid,  "ratio": 1, "action": "SELL"},
    ]
    exec_cfg = ExecutionConfig(
        initial_offset_cents=5, walk_increment_cents=5,
        walk_interval_sec=30, max_offset_pct_of_spread=0.30, timeout_sec=300,
    )
    try:
        result = await submit_pmcc_combo(
            ibkr=ib, symbol=symbol, legs=legs, contracts=n_contracts, config=exec_cfg,
        )
    except Exception as e:
        logger.exception("auto-entry submit failed for %s: %s", symbol, e)
        async with db_session() as s:
            i = await s.get(TradeIntent, intent_id)
            if i:
                i.status = "error"
                i.position_state = "abandoned"
            await record_auto_action(
                s, loop="entry", action_type="open_pmcc_submit_error",
                gate_result=gate, symbol=symbol, intent_id=intent_id,
                error=str(e), outcome="error",
            )
        return True

    # Persist result
    async with db_session() as s:
        i = await s.get(TradeIntent, intent_id)
        if i is not None:
            if result.status == "filled":
                i.status = "filled"
                i.position_state = "pmcc_full"
                i.net_debit_filled = result.fill_price
                i.ibkr_order_id = str(result.order_id) if result.order_id else None
                i.leap_filled_at = datetime.now(timezone.utc)
                i.short_call_filled_at = datetime.now(timezone.utc)
            elif result.status == "abandoned":
                i.status = "abandoned"
                i.position_state = "abandoned"
            else:
                i.status = "error"
                i.position_state = "abandoned"
        await record_auto_action(
            s, loop="entry", action_type=f"open_pmcc_{result.status}",
            gate_result=gate, symbol=symbol, intent_id=intent_id,
            payload=result.to_dict(), outcome=result.status, error=result.error,
            ibkr_order_id=str(result.order_id) if result.order_id else None,
        )

    if result.status == "filled":
        # Slippage telemetry — record the difference between the cap's
        # anchor (mid at submit time) and the actual fill price. For BUY
        # combos, positive slippage means we paid above mid. The walker
        # carries the initial mid in result.mid_at_submit. Bias-protect by
        # treating None as 0.
        slippage_per_spread = None
        if result.fill_price is not None and result.mid_at_submit is not None:
            slippage_per_spread = round(result.fill_price - result.mid_at_submit, 4)
        await alert(
            level="info",
            title=f"PMCC filled: {symbol}",
            body=(
                f"@${result.fill_price:.2f} after {result.walk_steps} walk steps "
                f"({result.elapsed_sec:.0f}s)"
                + (f" · slippage vs mid +${slippage_per_spread:.3f}/spread"
                   if slippage_per_spread is not None else "")
            ),
        )
        # Slippage alarm — surface any fill that paid more than 5¢ above
        # the mid at submit time. 5¢ per spread × 10 contracts × 100 mult
        # = $50 of avoidable cost; worth an operator-visible warning so
        # the cap or drift threshold can be re-tuned if this fires often.
        if slippage_per_spread is not None and slippage_per_spread > 0.05:
            await alert(
                level="warning",
                title=f"PMCC slippage > 5¢: {symbol}",
                body=(
                    f"filled @${result.fill_price:.2f} vs mid ${result.mid_at_submit:.2f} "
                    f"(+${slippage_per_spread:.3f}/spread × {n_contracts} contracts × $100 "
                    f"= ${slippage_per_spread * n_contracts * 100:.0f} above mid)"
                ),
            )
    elif result.status == "abandoned":
        await alert(
            level="warning",
            title=f"PMCC abandoned: {symbol}",
            body=f"walked to cap, no fill: {result.error or 'timeout'}",
        )
    return True


# Sizing — operator wants aggressive deployment of buying power.
# These knobs reproduce the operator's manual sizing on hot momentum names:
#   * PMCC entry: ~7% of NAV per spread, 8-15 contracts on typical AI names
#   * Stock fallback: ~3% of NAV per name (smaller — no defined-risk LEAP)
PMCC_TARGET_PCT_NAV    = 0.07
PMCC_MAX_DOLLARS       = 250_000
STOCK_FALLBACK_NAV_PCT = 0.03


async def _manager_conviction(symbol: str) -> tuple[float, dict]:
    """Bounded smart-money sizing tilt for a symbol (1.0 = neutral). Lazy
    import + fail-open so the tracker never blocks an entry."""
    try:
        from api.app.hedge_funds.conviction import manager_conviction
        return await manager_conviction(symbol)
    except Exception as e:
        logger.debug("conviction unavailable for %s: %s", symbol, e)
        return 1.0, {}


async def _fetch_nav(ib: Any) -> float:
    """Read NetLiquidation from IBKR. Returns 0.0 if unreachable."""
    try:
        summary = await ib.get_account_summary()
        for tag in ("NetLiquidation", "EquityWithLoanValue"):
            v = summary.get(tag)
            if v is not None and str(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
    except Exception as e:
        logger.warning("entry_loop: NAV fetch failed: %s", e)
    return 0.0


def _size_pmcc_contracts(
    *, net_debit_per_spread: float, nav: float,
    sizing_factor: float = 1.0, conviction_factor: float = 1.0,
) -> int:
    """Target contracts so net debit deployed ≈ PMCC_TARGET_PCT_NAV × NAV,
    capped at PMCC_MAX_DOLLARS. Minimum 1 contract.

    ``sizing_factor`` (0.0–1.0) tightens the target when the macro regime is
    elevated/defensive/panic (calm=1.0, defensive 0.50, panic 0.0 → blocked).

    ``conviction_factor`` (≥1.0) BOOSTS the %-of-NAV target for names tracked
    legendary investors hold with cross-fund confirmation. It lifts the
    percentage allocation but is re-capped against PMCC_MAX_DOLLARS, so smart
    money can tilt allocation without ever breaching the absolute ceiling.
    """
    if net_debit_per_spread <= 0 or nav <= 0:
        return 1
    if sizing_factor <= 0:
        return 0
    # Conviction lifts the % target, then the absolute $ cap clamps it, then
    # the macro factor tightens — order matters so the cap is never exceeded.
    target = min(nav * PMCC_TARGET_PCT_NAV * max(conviction_factor, 1.0),
                 PMCC_MAX_DOLLARS) * sizing_factor
    n = int(target / (net_debit_per_spread * 100))
    return max(1, n)


async def _try_stock_fallback(
    *, run_id: str, theme_id: str, symbol: str, composite: float,
    gate, pmcc_reason: str, ib: Any,
    sizing_factor: float = 1.0,
) -> bool:
    """Buy stock instead of PMCC when option liquidity isn't there.

    Sized at 0.5% of NAV. Marketable limit (bid+1¢) so we cross the spread
    fast but never pay through the offer. Same audit + intent persistence
    pattern as the PMCC path.
    """
    # Account snapshot for sizing
    try:
        summary = await ib.get_account_summary()
        nav = float(summary.get("NetLiquidation") or summary.get("EquityWithLoanValue") or 0)
    except Exception as e:
        logger.warning("stock-fallback: NAV fetch failed for %s: %s", symbol, e)
        nav = 0.0
    if nav <= 0:
        async with db_session() as s:
            await record_auto_action(
                s, loop="entry", action_type="open_stock_ineligible",
                gate_result=gate, symbol=symbol,
                payload={"run_id": run_id, "reason": "could not fetch NAV"},
                outcome="ineligible_no_nav",
            )
        return False

    # Live quote
    from ib_insync import Stock  # type: ignore
    ib_inst = await ib._ensure_connected()
    contract = Stock(symbol, "SMART", "USD")
    qualified = await ib_inst.qualifyContractsAsync(contract)
    if not qualified:
        async with db_session() as s:
            await record_auto_action(
                s, loop="entry", action_type="open_stock_ineligible",
                gate_result=gate, symbol=symbol,
                payload={"run_id": run_id, "reason": "qualify failed"},
                outcome="ineligible_qualify_failed",
            )
        return False
    contract = qualified[0]
    ticker = ib_inst.reqMktData(contract, "", False, False)
    await asyncio.sleep(1.5)
    bid = float(ticker.bid or 0)
    ask = float(ticker.ask or 0)
    last = float(ticker.last or 0)
    try: ib_inst.cancelMktData(contract)
    except Exception: pass

    spot = bid if bid > 0 else (last if last > 0 else 0)
    if spot <= 0:
        async with db_session() as s:
            await record_auto_action(
                s, loop="entry", action_type="open_stock_ineligible",
                gate_result=gate, symbol=symbol,
                payload={"run_id": run_id, "reason": "no live quote"},
                outcome="ineligible_no_quote",
            )
        return False

    # Marketable limit: bid+1¢ for buy, capped at ask
    limit = round((bid + 0.01) if bid > 0 else (last + 0.01), 2)
    if ask > 0 and limit > ask:
        limit = round(ask, 2)
    # Same bounded smart-money tilt as the PMCC path (boost only, capped).
    conviction_factor, _conv_meta = await _manager_conviction(symbol)
    target_dollars = nav * STOCK_FALLBACK_NAV_PCT * sizing_factor * max(conviction_factor, 1.0)
    if target_dollars <= 0:
        async with db_session() as s:
            await record_auto_action(
                s, loop="entry", action_type="open_stock_blocked_macro",
                gate_result=gate, symbol=symbol,
                payload={"run_id": run_id, "sizing_factor": sizing_factor},
                outcome="blocked_macro",
            )
        return False
    qty = max(1, int(target_dollars / limit))

    # Persist intent
    from api.app.db import TradeAuditLog
    intent_id = None
    async with db_session() as s:
        intent = TradeIntent(
            run_id=run_id, symbol=symbol, side="BUY", qty=qty, order_type="LMT",
            limit_px=limit, status="submitting",
            structure="stock", position_state="leap_pending",
            entry_strategy="stock_fallback",
            walking_config={"pmcc_reason": pmcc_reason, "auto_origin": "entry_loop"},
            rationale=(
                f"Stock fallback: PMCC ineligible ({pmcc_reason}). "
                f"Buying {qty} sh {symbol} @ LMT ${limit} (~{STOCK_FALLBACK_NAV_PCT*100:.1f}% NAV)."
            ),
        )
        s.add(intent)
        await s.flush()
        intent_id = intent.id
        s.add(TradeAuditLog(
            intent_id=intent_id, action="stock_submit_attempt",
            payload={"symbol": symbol, "qty": qty, "limit": limit,
                     "bid": bid, "ask": ask, "spot": spot, "pmcc_reason": pmcc_reason},
        ))

    await alert(
        level="info",
        title=f"Auto-submit STOCK fallback: {symbol}",
        body=f"{qty} sh @ ${limit} (~{fmt_money(qty*limit)}); PMCC ineligible: {pmcc_reason}",
    )

    # Submit via existing IbkrProvider.submit_trade
    from tradingagents.dataflows.providers.base import TradeIntent as IntentDC
    intent_dc = IntentDC(
        ticker=symbol, side="BUY", qty=qty, order_type="LMT",
        limit_px=limit, tif="DAY", account_mode="paper",
    )
    try:
        result = await ib.submit_trade(intent_dc)
    except Exception as e:
        logger.exception("stock-fallback submit failed for %s: %s", symbol, e)
        async with db_session() as s:
            i = await s.get(TradeIntent, intent_id)
            if i:
                i.status = "error"
                i.position_state = "abandoned"
            s.add(TradeAuditLog(
                intent_id=intent_id, action="stock_submit_outcome",
                outcome="error", error=str(e),
            ))
            await record_auto_action(
                s, loop="entry", action_type="open_stock_error",
                gate_result=gate, symbol=symbol, intent_id=intent_id,
                error=str(e), outcome="error",
            )
        return True

    order_id = str(result.get("ibkr_order_id") or result.get("order_id") or "")
    async with db_session() as s:
        i = await s.get(TradeIntent, intent_id)
        if i:
            i.status = "submitted"
            i.position_state = "leap_pending"
            i.ibkr_order_id = order_id
        s.add(TradeAuditLog(
            intent_id=intent_id, action="stock_submit_outcome",
            outcome="submitted", ibkr_account=None,
            payload={"ibkr_order_id": order_id, "qty": qty, "limit": limit},
        ))
        await record_auto_action(
            s, loop="entry", action_type="open_stock_submitted",
            gate_result=gate, symbol=symbol, intent_id=intent_id,
            payload={"qty": qty, "limit": limit, "nav_pct": STOCK_FALLBACK_NAV_PCT},
            outcome="submitted", ibkr_order_id=order_id,
        )
    logger.info("stock-fallback: SUBMITTED %s qty=%d @ $%.2f (orderId=%s)",
                symbol, qty, limit, order_id)
    return True


def fmt_money(v: float) -> str:
    return f"${v:,.0f}"


async def _resolve_stuck_intents() -> None:
    """Mark stuck-in-submitting intents as abandoned + log + alert.

    A clean run flips status to 'filled' / 'abandoned' / 'error' before
    returning. If something hangs (rare but observed: walking-limit
    timed out without bubbling, IBKR connection blip mid-walk), the row
    sits in 'submitting' forever and the per-symbol cap blocks retries.
    This watchdog catches those after STUCK_INTENT_TIMEOUT_MIN minutes.
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STUCK_INTENT_TIMEOUT_MIN)
    async with db_session() as s:
        rows = (
            await s.execute(
                select(TradeIntent).where(TradeIntent.status == "submitting")
                .where(TradeIntent.updated_at < cutoff)
            )
        ).scalars().all()
        for i in rows:
            logger.warning(
                "watchdog: marking stuck intent %s (%s) as abandoned (in submitting since %s)",
                i.id, i.symbol, i.updated_at,
            )
            i.status = "abandoned"
            i.position_state = "abandoned"
            from api.app.db import TradeAuditLog
            s.add(TradeAuditLog(
                intent_id=i.id, action="watchdog_abandon",
                outcome="abandoned",
                payload={"reason": f"submitting > {STUCK_INTENT_TIMEOUT_MIN}min"},
            ))
        if rows:
            await alert(
                level="warning",
                title=f"Watchdog: {len(rows)} stuck intent(s) abandoned",
                body=", ".join(f"{i.symbol} ({i.id})" for i in rows),
            )


async def _daily_cap_exhausted() -> bool:
    """Quick check: have we already passed the daily new-entry cap?

    Mirrors the bookkeeping-exclusion logic in ``auto_gate._gate_strategy_budget``
    so this short-circuit doesn't trip on background informational rows
    (``*_ineligible``, ``*_hold``, etc.) that aren't real entry attempts.
    """
    from .auto_gate import DEFAULT_CAPS
    from sqlalchemy import func
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with db_session() as s:
        from api.app.db import AutoAction
        n = (
            await s.execute(
                select(func.count())
                .select_from(AutoAction)
                .where(AutoAction.timestamp >= today)
                .where(AutoAction.gate_status == "passed")
                .where(AutoAction.loop == "entry")
                .where(~AutoAction.action_type.like("%_gate_passed"))
                .where(~AutoAction.action_type.like("%_ineligible"))
                .where(~AutoAction.action_type.like("%_hold"))
            )
        ).scalar_one()
    return n >= DEFAULT_CAPS["AUTO_MAX_NEW_ENTRIES_PER_DAY"]
