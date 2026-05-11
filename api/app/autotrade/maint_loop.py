"""Maintenance loop — Phase D.

Runs every 5 min during RTH. For every open position the system owns
(stocks + PMCCs), evaluates roll/exit/hedge triggers and submits the
implied trade through the same auto-gate + walking-limit executor that
entries use.

Key invariants:
  * Same gate stack as entries (kill switch, daily caps, sector regime)
  * Same audit trail (auto_actions + trade_audit_log)
  * Read-only fast paths preferred — only mutate state when a trigger
    actually fires, so a 5-min tick is cheap
  * Idempotent — restarting mid-cycle shouldn't double-fire actions
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.config import get_settings
from api.app.db import (
    AutoAction,
    Position,
    Run,
    SystemState,
    TickerScore,
    TradeAuditLog,
    TradeIntent,
    get_session as db_session,
)

from .alerts import alert
from .auto_gate import check_auto_action, record_auto_action
from .market_conditions import gate_rth

logger = logging.getLogger("agentic_edge.maint_loop")


_TASK: Optional[asyncio.Task] = None
_POLL_INTERVAL_SEC = 300       # 5 min during RTH


async def start_maintenance_loop() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop_forever(), name="maint_loop")
    logger.info("maintenance loop started (poll=%ds)", _POLL_INTERVAL_SEC)


async def stop_maintenance_loop() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None


async def _loop_forever() -> None:
    # Initial delay so the entry loop can fire first if both are starting.
    await asyncio.sleep(15.0)
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("maintenance tick failed: %s", e)
        await asyncio.sleep(_POLL_INTERVAL_SEC)


async def _tick() -> None:
    settings = get_settings()

    # Watchdog runs first — independent of AUTOTRADE_ENABLED. Cleans up
    # zombie 'running' runs so the UI doesn't show false-positives and
    # so the operator can re-trigger without manual intervention.
    try:
        await _resolve_stuck_runs(idle_timeout_min=15)
    except Exception as e:
        logger.warning("maint loop: stuck-run watchdog failed: %s", e)

    if not settings.AUTOTRADE_ENABLED:
        return
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is None or not state.autotrade_enabled:
            return

    # RTH only — no maintenance actions outside US trading hours.
    if gate_rth() is not None:
        return

    # Snapshot state from IBKR + DB
    try:
        from api.app.positions import _ibkr
        ib = await _ibkr()
        positions = await ib.get_positions()
    except Exception as e:
        logger.warning("maint loop: IBKR unavailable (%s); skipping tick", e)
        return

    pos_by_symbol = {(p.get("symbol") or "").upper(): p for p in positions}

    # Adopt any IBKR position that doesn't have a TradeIntent yet — gives
    # the maintenance loop ownership of "orphan" positions (existing
    # holdings imported into the system, manual fills, etc.) so they get
    # monitored for exit triggers like everything else.
    orphans_adopted = await _adopt_orphan_positions(positions)

    # Macro regime read once per tick — VIX + SPX overlay applied across
    # all per-position evaluations downstream. Best-effort fetch; on
    # failure we get a 'calm' default and the audit row records the
    # degraded read so the operator can see it.
    try:
        from tradingagents.strategies.macro_regime import get_macro_regime
        macro = await get_macro_regime(ib)
    except Exception as e:
        logger.warning("macro regime fetch failed: %s", e)
        from tradingagents.strategies.macro_regime import MacroRegime
        macro = MacroRegime(rationale=f"macro fetch failed: {e}")
    async with db_session() as s:
        await record_auto_action(
            s, loop="maintenance", action_type=f"macro_regime_{macro.regime}",
            gate_result=_synthetic_passed_gate(),
            payload={
                "regime": macro.regime,
                "vix": macro.vix_last,
                "spx_change_pct": macro.spx_change_pct,
                "sizing_factor": macro.sizing_factor,
                "leap_roll_deferred": macro.leap_roll_deferred,
                "earnings_window_mult": macro.earnings_window_mult,
                "rationale": macro.rationale,
            },
            outcome="ok",
        )

    # 8-K filings watcher — sweeps the universe-wide stream once per tick,
    # filters to symbols we care about (held + theme universes), writes
    # filing_alert audit rows for new events. Severity classification
    # feeds the thesis-break detector via _filing_breaks_by_symbol below.
    filing_breaks_by_symbol = await _watch_8k_filings(pos_by_symbol)

    # Daily IV snapshot capture — one row per (symbol, date) for every
    # theme-universe symbol. Idempotent: a snapshot for today's date
    # short-circuits the inner fetch. Builds the historical IV
    # distribution that the percentile signal needs.
    await _capture_daily_iv_snapshots(ib)

    # Insider universe sweep — pulls FMP's latest insider trades stream,
    # filters to theme-universe symbols, fires insider_alert audit rows
    # for fresh activity. Early-warning complement to the per-symbol
    # get_insider_sell_pressure aggregator (which is 4-hour cached).
    await _watch_insider_universe()

    # Pull every open intent (the system's record of what it owns)
    async with db_session() as s:
        intents = (
            await s.execute(
                select(TradeIntent).where(TradeIntent.status.in_(["filled", "submitted"]))
                .where(TradeIntent.position_state.in_(["pmcc_full", "leap_pending"]))
            )
        ).scalars().all()

    if not intents:
        # Heartbeat row even when there's nothing to evaluate — the operator
        # needs to see the loop is alive on the runs page.
        async with db_session() as s:
            await record_auto_action(
                s, loop="maintenance", action_type="heartbeat",
                gate_result=_synthetic_passed_gate(),
                payload={"intents_evaluated": 0,
                         "ibkr_positions": len(positions),
                         "orphans_adopted": orphans_adopted},
                outcome="no_intents",
            )
        return

    logger.info("maint loop: tick — %d open intent(s) to evaluate", len(intents))

    # Latest agent decision per symbol (today's run)
    latest_decisions = await _latest_decisions_today({i.symbol for i in intents})

    for intent in intents:
        sym = (intent.symbol or "").upper()
        try:
            if intent.structure == "stock":
                await _evaluate_stock(
                    intent, pos_by_symbol.get(sym), latest_decisions.get(sym),
                    ib, macro,
                    filing_thesis_break=filing_breaks_by_symbol.get(sym),
                )
            elif intent.structure in ("pmcc", "pmcc_sequenced"):
                await _evaluate_pmcc(intent, latest_decisions.get(sym), ib)
        except Exception as e:
            logger.exception("maint loop: %s evaluation failed: %s", sym, e)

    # End-of-tick heartbeat — the operator-visible "I'm alive and I checked".
    async with db_session() as s:
        await record_auto_action(
            s, loop="maintenance", action_type="heartbeat",
            gate_result=_synthetic_passed_gate(),
            payload={"intents_evaluated": len(intents),
                     "ibkr_positions": len(positions),
                     "orphans_adopted": orphans_adopted},
            outcome="ok",
        )


async def _adopt_orphan_positions(ibkr_positions: list[dict]) -> int:
    """Create synthetic TradeIntent rows for IBKR positions that don't yet
    have one. Without this, positions imported manually or held before the
    system was deployed are invisible to the maintenance loop's exit logic.

    **Gated on theme membership.** Only positions whose symbol appears in
    ``theme_symbols`` get adopted — random legacy holdings outside the
    chokepoint theme universe are intentionally left alone. The framework's
    job is to manage names tied to active themes; non-themed positions
    are out of scope.

    Only stock positions are adopted automatically; option positions (PMCC
    legs) need leg-aware metadata that lives in the run's intent record,
    so we leave those for the operator to import via the admin endpoint.
    """
    if not ibkr_positions:
        return 0
    sym_to_pos = {(p.get("symbol") or "").upper(): p
                  for p in ibkr_positions if p.get("symbol")}
    if not sym_to_pos:
        return 0

    from api.app.db import ThemeSymbol

    async with db_session() as s:
        # Symbols that already have an OPEN intent (any open lifecycle state)
        rows = (
            await s.execute(
                select(TradeIntent.symbol)
                .where(TradeIntent.symbol.in_(list(sym_to_pos.keys())))
                .where(TradeIntent.status.in_(["filled", "submitted", "submitting"]))
                .where(TradeIntent.position_state.in_(
                    ["pmcc_full", "leap_pending", "leap_open_naked", "pending"]))
            )
        ).all()
        owned = {(r[0] or "").upper() for r in rows}

        # Theme universe — only adopt positions in this set.
        theme_rows = (
            await s.execute(
                select(ThemeSymbol.symbol)
                .where(ThemeSymbol.symbol.in_(list(sym_to_pos.keys())))
            )
        ).all()
        in_theme_universe = {(r[0] or "").upper() for r in theme_rows if r[0]}

        new_count = 0
        skipped_non_theme = 0
        for sym, pos in sym_to_pos.items():
            if sym in owned:
                continue
            if sym not in in_theme_universe:
                skipped_non_theme += 1
                continue
            qty = float(pos.get("qty") or 0)
            if qty == 0:
                continue
            sec_type = str(pos.get("sec_type") or "STK").upper()
            # Phase A only adopts equity. Option leg adoption needs the
            # combo's expiry/strike/right metadata, which we treat as a
            # separate (operator-confirmed) import path.
            if sec_type != "STK":
                continue
            avg = float(pos.get("avg_price") or 0)
            intent = TradeIntent(
                symbol=sym,
                side="BUY" if qty > 0 else "SELL",
                qty=abs(qty),
                limit_px=avg if avg > 0 else None,
                status="filled",
                structure="stock",
                position_state="leap_pending",   # legacy state name = "live"
                entry_strategy="adopted_orphan",
                rationale=(
                    f"Adopted from existing IBKR position "
                    f"({abs(qty):.0f} sh @ ${avg:.2f})."
                ),
                walking_config={
                    "adopted_at": datetime.now(timezone.utc).isoformat(),
                    "source": "maint_loop_orphan_adopt",
                },
            )
            s.add(intent)
            new_count += 1
        if new_count:
            await s.flush()

    if new_count or skipped_non_theme:
        logger.info(
            "maint loop: adopted %d orphan position(s); skipped %d non-theme position(s)",
            new_count, skipped_non_theme,
        )
    return new_count


# ---------------------------------------------------------------------------
# Per-position evaluation
# ---------------------------------------------------------------------------


async def _latest_decisions_today(symbols: set[str]) -> dict[str, str]:
    """Return latest scorecard decision per symbol from today's done runs."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    out: dict[str, str] = {}
    if not symbols:
        return out
    async with db_session() as s:
        rows = (
            await s.execute(
                select(TickerScore.symbol, TickerScore.decision, TickerScore.composite, Run.finished_at)
                .join(Run, Run.id == TickerScore.run_id)
                .where(Run.status == "done").where(Run.finished_at >= today)
                .where(TickerScore.symbol.in_(symbols))
                .order_by(Run.finished_at.desc())
            )
        ).all()
    # Latest by symbol
    for sym, decision, _composite, _ts in rows:
        out.setdefault((sym or "").upper(), decision)
    return out


async def _evaluate_stock(
    intent: TradeIntent, pos: Optional[dict],
    latest_decision: Optional[str], ib: Any,
    macro: Optional[Any] = None,
    filing_thesis_break: Optional[str] = None,
) -> None:
    """Trim/exit decisions for a held stock position.

    Order of evaluation per tick:
      1. Profit-preservation trim ladder (Phase B) — partial closes that
         recover capital while letting winners run.
      2. Hard exit triggers (Avoid signal, ATR breach) — full close.

    Trim and exit can both fire in the same tick: trim first, then if the
    exit logic still says go, the remainder closes.
    """
    if pos is None:
        # We have an intent but IBKR shows no position — possibly closed manually.
        async with db_session() as s:
            i = await s.get(TradeIntent, intent.id)
            if i:
                i.status = "closed"
                i.position_state = "closed"
        return

    from tradingagents.strategies.maintenance.exits import maybe_exit_stock
    from tradingagents.strategies.maintenance.profit_preservation import (
        evaluate_stock_trim,
    )
    from tradingagents.strategies.maintenance.theme_health import (
        is_theme_hot_for_symbol, get_thesis_break_signal,
    )

    avg = float(pos.get("avg_price") or 0)
    last = float(pos.get("last_price") or 0)
    qty = float(pos.get("qty") or 0)
    if qty == 0 or avg <= 0 or last <= 0:
        return

    # ---- Theme health gates -----------------------------------------
    # A symbol with no theme home (orphan adopt) gets theme_hot=True so it
    # isn't aggressively trimmed before the rotation engine has a chance
    # to find it a home — the absence of a theme is itself a flag, surfaced
    # via the audit row.
    async with db_session() as s:
        theme_hot, best_theme = await is_theme_hot_for_symbol(s, intent.symbol)
        thesis_break = await get_thesis_break_signal(s, intent.symbol)
    has_theme_home = best_theme is not None
    if not has_theme_home:
        theme_hot = True

    # Fold the SEC-filing-derived break (if any) into the theme-derived one.
    if filing_thesis_break:
        thesis_break = (
            f"{thesis_break}; filing: {filing_thesis_break}"
            if thesis_break else f"filing: {filing_thesis_break}"
        )

    # If thesis is broken, skip trim and force a full exit.
    if thesis_break:
        await _execute_stock_exit(
            intent=intent, qty=abs(qty),
            reason=f"thesis broken: {thesis_break}", exit_kind="thesis_break",
            ib=ib,
        )
        return

    # ---- Profit-preservation trim ladder ----------------------------
    signals = await _compute_daily_signals(intent.symbol)
    trim_today = await _check_trimmed_today(intent.symbol, kind="stock")
    trim = await evaluate_stock_trim(
        symbol=intent.symbol, avg_price=avg, current_price=last,
        pct_move_today=signals.get("pct_move_today"),
        volume_ratio_vs_20d=signals.get("volume_ratio"),
        rsi_14=signals.get("rsi_14"),
        theme_hot=theme_hot,
        already_trimmed_today=trim_today,
    )
    if trim.should_trim:
        # Round down so we never trim more than asked. Always leave at least
        # 1 share to keep the position alive — full closes go through the
        # exit path with proper state transitions.
        trim_qty = max(1, int(abs(qty) * trim.trim_pct))
        if trim_qty < abs(qty):
            await _execute_stock_trim(intent=intent, trim_qty=trim_qty,
                                      decision=trim, signals=signals, ib=ib)
            qty = qty - trim_qty if qty > 0 else qty + trim_qty   # remainder

    # ---- Momentum exhaustion + rotation candidates (Phase D) --------
    from tradingagents.strategies.maintenance.momentum_exhaustion import (
        evaluate_momentum_exhaustion,
    )
    from tradingagents.strategies.maintenance.rotation import (
        find_rotation_candidate,
    )
    from tradingagents.strategies.maintenance.exit_pressure import (
        compute_exit_pressure,
    )

    atr_30d = await _compute_atr_30d(intent.symbol)

    # Closing-auction imbalance — only available 15:50-16:00 ET. We only
    # bother fetching it for *stretched* names (the imbalance signal is
    # most meaningful when paired with technical exhaustion); skipping
    # the fetch for non-stretched names keeps the per-tick cost low.
    auction_imbalance = None
    auction_price = None
    ma20 = signals.get("ma_20d")
    if ma20 and ma20 > 0:
        stretched = (last - ma20) / ma20 > 0.20
        if stretched and _is_auction_window():
            try:
                aq = await ib.get_auction_imbalance(symbol=intent.symbol)
                auction_imbalance = aq.get("imbalance")
                auction_price = aq.get("auction_price")
            except Exception as e:
                logger.debug("auction imbalance fetch failed for %s: %s",
                             intent.symbol, e)

    # Insider + analyst pressure signals — FMP-cached, cheap per tick.
    insider_pressure = None
    analyst_pressure = None
    closes_for_iv: Optional[list[float]] = None
    try:
        from tradingagents.dataflows.providers.fmp import FmpProvider
        from tradingagents.strategies.maintenance.analyst_grades import (
            evaluate_analyst_pressure,
        )
        fmp = FmpProvider()
        insider_pressure = await fmp.get_insider_sell_pressure(intent.symbol)
        # 60-day price change for the analyst upgrade-after-run signal,
        # plus daily closes that the IV signal needs for realized-vol math.
        pct_60d: Optional[float] = None
        try:
            from datetime import date, timedelta
            from tradingagents.dataflows.fallback import get_stock_data_with_fallback
            df60 = await get_stock_data_with_fallback(
                intent.symbol, date.today() - timedelta(days=90), date.today(),
                ibkr_provider=ib,
            )
            if df60 is not None and len(df60) >= 40 and "Close" in df60.columns:
                closes = df60["Close"].astype(float).tolist()
                closes_for_iv = closes
                if len(closes) >= 60:
                    pct_60d = (closes[-1] - closes[-60]) / closes[-60]
                else:
                    pct_60d = (closes[-1] - closes[0]) / closes[0]
        except Exception as e:
            logger.debug("60d price fetch failed for %s: %s", intent.symbol, e)
        try:
            grades = await fmp.get_analyst_grade_changes(intent.symbol)
            analyst_pressure = await evaluate_analyst_pressure(
                symbol=intent.symbol, grade_changes=grades, pct_60d=pct_60d,
            )
        except Exception as e:
            logger.debug("analyst grades fetch failed for %s: %s", intent.symbol, e)
    except Exception as e:
        logger.debug("FMP signal init failed for %s: %s", intent.symbol, e)

    # IV signal (8th exhaustion indicator). Daily snapshot is captured by
    # _capture_iv_snapshot earlier in this tick; per-position eval reads
    # today's IV + the underlying's realized vol from closes_for_iv.
    iv_signal_obj = None
    try:
        from tradingagents.strategies.maintenance.iv_signal import (
            evaluate_iv_signal,
        )
        # Today's IV from the freshly-captured snapshot (or live fetch if
        # capture hasn't run yet today).
        from api.app.db import IvSnapshot
        from datetime import datetime as _dt, timezone as _tz
        today_dt = _dt.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        iv_today_value: Optional[float] = None
        async with db_session() as s:
            row = (
                await s.execute(
                    select(IvSnapshot.atm_call_iv)
                    .where(IvSnapshot.symbol == intent.symbol)
                    .where(IvSnapshot.date == today_dt)
                )
            ).first()
            if row:
                iv_today_value = float(row[0])
        # Fallback: pull live ATM IV if no snapshot for today yet.
        if iv_today_value is None:
            try:
                live = await ib.get_atm_call_iv(symbol=intent.symbol)
                if live.get("iv"):
                    iv_today_value = float(live["iv"])
            except Exception as e:
                logger.debug("live ATM IV fetch failed for %s: %s", intent.symbol, e)
        async with db_session() as s:
            iv_signal_obj = await evaluate_iv_signal(
                s, symbol=intent.symbol,
                iv_today=iv_today_value, closes=closes_for_iv,
            )
    except Exception as e:
        logger.debug("IV signal eval failed for %s: %s", intent.symbol, e)

    exhaustion = evaluate_momentum_exhaustion(
        symbol=intent.symbol, current_price=last,
        ma_20d=signals.get("ma_20d"),
        rsi_14=signals.get("rsi_14"),
        volume_ratio=signals.get("volume_ratio"),
        open_today=signals.get("open_today"),
        prior_close=signals.get("prior_close"),
        atr_30d=atr_30d,
        auction_imbalance=auction_imbalance,
        auction_price=auction_price,
        insider_pressure=insider_pressure,
        analyst_pressure=analyst_pressure,
        iv_signal=iv_signal_obj,
    )

    # Institutional flow (13F) — quarterly, only refreshed when a new
    # quarter is filed. We fetch the latest *completed* quarter once
    # per tick; FMP cache (4h) makes this near-free.
    inst_flow = None
    try:
        from tradingagents.strategies.maintenance.institutional_flow import (
            _latest_completed_quarter, evaluate_institutional_flow,
        )
        from tradingagents.dataflows.providers.fmp import FmpProvider as _FMP
        y, q = _latest_completed_quarter()
        summary = await _FMP().get_institutional_position_summary(
            intent.symbol, year=y, quarter=q,
        )
        inst_flow = evaluate_institutional_flow(
            symbol=intent.symbol, summary=summary,
        )
    except Exception as e:
        logger.debug("13F flow fetch failed for %s: %s", intent.symbol, e)

    # Earnings-call transcript — DeepSeek-pro reads the latest completed
    # quarter's call and extracts structured guidance/demand/margin signals.
    # Falls back to a regex keyword scan if the LLM call fails or
    # DEEPSEEK_API_KEY isn't set. Both FMP transcript fetch and LLM
    # output are cached, so the per-tick cost is one cache hit per name.
    transcript_signal = None
    try:
        from tradingagents.strategies.maintenance.earnings_transcript import (
            evaluate_transcript_smart,
        )
        from tradingagents.dataflows.providers.fmp import FmpProvider as _FMP2
        from tradingagents.strategies.maintenance.institutional_flow import (
            _latest_completed_quarter as _lcq,
        )
        y, q = _lcq()
        transcript_row = await _FMP2().get_earnings_transcript(
            intent.symbol, year=y, quarter=q,
        )
        transcript_signal = await evaluate_transcript_smart(transcript_row)
    except Exception as e:
        logger.debug("transcript fetch failed for %s: %s", intent.symbol, e)

    # If transcript signal trips a thesis break, fire full exit. The
    # early-exit gate above only checks theme + filing breaks; transcript
    # data lives with the FMP fetches and we can't move that block (the
    # 60-day price feeds the analyst signal too). So this is a second
    # exit point downstream of the trim — by the time we reach here,
    # any partial trim has already happened, and the remainder closes.
    if transcript_signal and transcript_signal.thesis_break:
        await _execute_stock_exit(
            intent=intent, qty=abs(qty),
            reason=f"thesis broken: transcript: {transcript_signal.rationale}",
            exit_kind="thesis_break", ib=ib,
        )
        return

    rotation_candidate = None
    async with db_session() as s:
        try:
            rotation_candidate = await find_rotation_candidate(
                s, held_symbol=intent.symbol,
                held_score=(best_theme.composite if best_theme else None),
            )
        except Exception as e:
            logger.warning("rotation lookup failed for %s: %s", intent.symbol, e)

    pressure = compute_exit_pressure(
        theme_composite=(best_theme.composite if best_theme else None),
        theme_streak_days=(best_theme.streak_days_below_floor if best_theme else 0),
        trim_band=trim.band,
        exhaustion_score=exhaustion.score,
        rotation_score_delta=(rotation_candidate.score_delta if rotation_candidate else None),
    )

    # Per-tick observability row — captures the full picture even when
    # nothing fires this tick. Operators can filter on action_type
    # 'position_pressure' on the runs page to see live exit-pressure scores.
    async with db_session() as s:
        await record_auto_action(
            s, loop="maintenance", action_type=f"position_pressure_{pressure.band}",
            gate_result=_synthetic_passed_gate(),
            symbol=intent.symbol, intent_id=intent.id,
            payload={
                "score": pressure.score,
                "band": pressure.band,
                "rationale": pressure.rationale,
                "sub_scores": pressure.sub_scores,
                "trim_fired": trim.should_trim,
                "trim_band": trim.band,
                "exhaustion": {
                    "score": exhaustion.score,
                    "tripped": exhaustion.signals_tripped,
                    "available": exhaustion.signals_available,
                },
                "rotation": (
                    {"to": rotation_candidate.candidate_symbol,
                     "delta": rotation_candidate.score_delta,
                     "reason": rotation_candidate.reason}
                    if rotation_candidate else None
                ),
                "theme": (
                    {"composite": best_theme.composite,
                     "streak": best_theme.streak_days_below_floor}
                    if best_theme else None
                ),
                "institutional_flow": (
                    {"label": inst_flow.flow_label,
                     "ownership_pct": inst_flow.ownership_pct,
                     "ownership_pct_change": inst_flow.ownership_pct_change,
                     "investors_change": inst_flow.investors_change,
                     "crowded": inst_flow.crowded,
                     "rationale": inst_flow.rationale}
                    if inst_flow else None
                ),
                "analyst_pressure": (
                    {"upgrades_30d": analyst_pressure.upgrades_30d,
                     "downgrades_30d": analyst_pressure.downgrades_30d,
                     "upgrade_after_run": analyst_pressure.upgrade_after_run,
                     "downgrade_acceleration": analyst_pressure.downgrade_acceleration,
                     "pct_60d": analyst_pressure.pct_60d,
                     "rationale": analyst_pressure.rationale}
                    if analyst_pressure else None
                ),
                "transcript": (
                    {"period": transcript_signal.period,
                     "severity": transcript_signal.severity_score,
                     "thesis_break": transcript_signal.thesis_break,
                     "matches": transcript_signal.matches[:5],
                     "rationale": transcript_signal.rationale}
                    if transcript_signal and transcript_signal.has_transcript else None
                ),
            },
            outcome=pressure.band,
        )

    # If rotation pressure is high and the held position isn't already
    # being trimmed/exited, surface it as an alert. Auto-execution of
    # rotation is intentionally out of scope — moving capital between
    # names stays a human decision.
    if rotation_candidate and pressure.band in ("trim_heavy", "aggressive"):
        await alert(
            level="warning",
            title=f"Rotation candidate: {intent.symbol} -> {rotation_candidate.candidate_symbol}",
            body=rotation_candidate.reason,
        )

    # ---- Hard exit (full close) -------------------------------------
    decision = await maybe_exit_stock(
        symbol=intent.symbol, current_price=last, avg_price=avg,
        latest_decision=latest_decision, atr_30d=atr_30d,
    )
    if not decision.should_exit:
        return
    if abs(qty) <= 0:
        return  # already trimmed to flat (shouldn't happen due to floor above)

    await _execute_stock_exit(intent=intent, qty=abs(qty), reason=decision.reason,
                              exit_kind=decision.exit_kind, ib=ib)


async def _evaluate_pmcc(intent: TradeIntent, latest_decision: Optional[str], ib: Any) -> None:
    """Roll/exit decisions for a PMCC position. Currently flags only;
    actual roll execution requires the option-chain probe + combo build,
    same path as entries. For Phase D v1 we mark intent flags + alert
    the operator; v2 will auto-fire the rolls."""
    from tradingagents.strategies.maintenance.exits import maybe_close_pmcc
    from tradingagents.strategies.maintenance.earnings import days_to_earnings

    # Close decision first
    leap_dte_days = _dte_from_str(intent.leap_expiry) if intent.leap_expiry else None
    close = await maybe_close_pmcc(
        symbol=intent.symbol,
        leap_delta=intent.leap_delta_actual,
        latest_decision=latest_decision,
        leap_dte=leap_dte_days,
    )
    if close.should_exit:
        await _flag_pmcc_close(intent=intent, reason=close.reason, kind=close.exit_kind)
        return

    # Earnings hedge check
    days_to_e = await days_to_earnings(intent.symbol)
    if days_to_e is not None and 0 <= days_to_e <= 2:
        await alert(
            level="warning",
            title=f"Earnings in {days_to_e}d for {intent.symbol}",
            body=(
                f"Recommend buy back short ${intent.short_call_strike} "
                f"{intent.short_call_expiry} before close, re-sell day after print."
            ),
        )
        async with db_session() as s:
            await record_auto_action(
                s, loop="maintenance", action_type="earnings_hedge_due",
                gate_result=_synthetic_passed_gate(),
                symbol=intent.symbol, intent_id=intent.id,
                payload={"days_to_earnings": days_to_e},
                outcome="flagged",
            )
        return

    # ---- Short-call roll: full operator-spec evaluation -------------
    # We invoke the rolls module which applies all the rules:
    #   * defensive (delta ≥ 0.70)
    #   * time (DTE ≤ 7 OTM)
    #   * profit (≥ 80% credit captured)
    #   * earnings (close-only — don't sell into print)
    # Strike picking honours expected-move + recent-high + momentum mode +
    # cost guard. We *flag* the roll with the suggested replacement leg;
    # operator confirms via /api/admin/positions/exit (and the next slice
    # will add a /positions/roll endpoint that fires the combo).
    if intent.short_call_expiry and intent.short_call_strike:
        await _evaluate_short_call_roll(intent, ib, days_to_e)

    # ---- LEAP forward roll evaluation -------------------------------
    if intent.leap_expiry and leap_dte_days is not None and leap_dte_days <= 180:
        await _evaluate_leap_forward_roll(intent, ib, leap_dte_days)


async def _evaluate_short_call_roll(intent: TradeIntent, ib: Any, days_to_e: Optional[int]) -> None:
    """Pull the chain + live short quote, ask the rolls module for a decision,
    persist the decision as a flag with the suggested replacement."""
    from tradingagents.strategies.maintenance.rolls import maybe_roll_short_call

    sym = intent.symbol
    # Need: chain expirations + strikes, current short quote (delta + mid),
    # underlying spot + IV. All best-effort — if any fail, skip and try
    # next tick.
    try:
        chain = await ib.get_option_chain(symbol=sym)
        spot = await _get_spot(ib, sym)
        cur_quote = await ib.get_option_quote(
            symbol=sym, expiry=intent.short_call_expiry,
            strike=float(intent.short_call_strike), right="C",
        )
    except Exception as e:
        logger.debug("short-call roll eval skipped for %s (data fetch failed): %s", sym, e)
        return

    decision = await maybe_roll_short_call(
        symbol=sym,
        short_expiry=intent.short_call_expiry,
        short_strike=float(intent.short_call_strike),
        current_short_delta=cur_quote.get("delta"),
        current_short_mid=_mid_or_last(cur_quote),
        open_credit=intent.short_call_fill_price,    # if known; rolls handles None gracefully
        underlying_spot=spot or 0.0,
        underlying_iv=cur_quote.get("iv"),
        chain_strikes=chain.get("strikes", []),
        chain_expirations=chain.get("expirations", []),
        ibkr=ib,
        days_to_earnings=days_to_e,
    )

    # Earnings → short-call must be CLOSED (not rolled). Auto-fire close-only.
    if decision.skip_short_until_event == "earnings":
        await alert(level="warning",
                    title=f"Earnings hedge: closing short call {sym}",
                    body=decision.reason)
        await _execute_auto_short_call_close(intent, ib, decision.reason)
        return

    if not decision.should_roll:
        return  # hold

    # Auto-fire the roll
    await _execute_auto_short_call_roll(intent, decision, ib)


async def _evaluate_leap_forward_roll(intent: TradeIntent, ib: Any, leap_dte_days: int) -> None:
    from tradingagents.strategies.maintenance.rolls import maybe_roll_leap_forward

    sym = intent.symbol
    try:
        chain = await ib.get_option_chain(symbol=sym)
        spot = await _get_spot(ib, sym)
    except Exception as e:
        logger.debug("LEAP roll eval skipped for %s (data fetch failed): %s", sym, e)
        return

    decision = await maybe_roll_leap_forward(
        symbol=sym, leap_expiry=intent.leap_expiry,
        leap_strike=float(intent.leap_strike or 0),
        current_leap_delta=intent.leap_delta_actual,
        underlying_spot=spot or 0.0,
        chain_strikes=chain.get("strikes", []),
        chain_expirations=chain.get("expirations", []),
        ibkr=ib,
    )
    if not decision.should_roll:
        # If the decision says recommend_close, surface that; otherwise quiet.
        if decision.detail and decision.detail.get("recommend_close"):
            await alert(
                level="warning",
                title=f"LEAP recommend close: {sym}",
                body=decision.reason,
            )
            async with db_session() as s:
                await record_auto_action(
                    s, loop="maintenance", action_type="leap_close_recommended",
                    gate_result=_synthetic_passed_gate(),
                    symbol=sym, intent_id=intent.id,
                    payload={"reason": decision.reason, "leap_dte": leap_dte_days},
                    outcome="flagged",
                )
        return

    # Auto-fire LEAP forward roll
    await _execute_auto_leap_forward_roll(intent, decision, ib)


async def _get_spot(ib: Any, symbol: str) -> Optional[float]:
    """Lightweight spot fetch used by maintenance-loop quote enrichment.
    Reuses the PMCC strategy's fallback chain (IBKR live → Polygon → yfinance)."""
    from tradingagents.strategies.pmcc import _fetch_spot
    return await _fetch_spot(ib, symbol)


def _mid_or_last(q: dict[str, Any]) -> Optional[float]:
    bid = q.get("bid"); ask = q.get("ask")
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2
    last = q.get("last"); model = q.get("model_price")
    for v in (last, model):
        if v and v > 0:
            return float(v)
    return None


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------


async def _execute_stock_exit(*, intent: TradeIntent, qty: float, reason: str, exit_kind: str, ib: Any) -> None:
    """Submit a marketable LMT SELL to close a stock position. Audited."""
    from ib_insync import Stock  # type: ignore
    from tradingagents.dataflows.providers.base import TradeIntent as IntentDC

    # Quick quote
    ib_inst = await ib._ensure_connected()
    contract = Stock(intent.symbol, "SMART", "USD")
    qualified = await ib_inst.qualifyContractsAsync(contract)
    if not qualified:
        logger.warning("maint exit: could not qualify %s", intent.symbol)
        return
    contract = qualified[0]
    ticker = ib_inst.reqMktData(contract, "", False, False)
    await asyncio.sleep(1.5)
    bid = float(ticker.bid or 0)
    ask = float(ticker.ask or 0)
    last = float(ticker.last or 0)
    try: ib_inst.cancelMktData(contract)
    except Exception: pass

    # Marketable limit on sell side: bid - 1¢ to fill quickly
    if bid > 0:
        limit = round(bid - 0.01, 2)
    elif last > 0:
        limit = round(last - 0.01, 2)
    else:
        logger.warning("maint exit: no quote for %s; skipping", intent.symbol)
        return

    async with db_session() as s:
        s.add(TradeAuditLog(
            intent_id=intent.id, action="maint_exit_attempt",
            payload={"symbol": intent.symbol, "qty": qty, "limit": limit,
                     "reason": reason, "kind": exit_kind, "bid": bid, "ask": ask},
        ))

    intent_dc = IntentDC(
        ticker=intent.symbol, side="SELL", qty=int(qty),
        order_type="LMT", limit_px=limit, tif="DAY", account_mode="paper",
    )
    try:
        result = await ib.submit_trade(intent_dc)
    except Exception as e:
        logger.exception("maint exit submit failed for %s: %s", intent.symbol, e)
        async with db_session() as s:
            s.add(TradeAuditLog(
                intent_id=intent.id, action="maint_exit_outcome",
                outcome="error", error=str(e),
            ))
        return

    order_id = str(result.get("ibkr_order_id") or "")
    async with db_session() as s:
        i = await s.get(TradeIntent, intent.id)
        if i:
            i.status = "closing"
            i.position_state = "closing"
        s.add(TradeAuditLog(
            intent_id=intent.id, action="maint_exit_outcome",
            outcome="submitted",
            payload={"order_id": order_id, "limit": limit, "qty": qty},
        ))
        await record_auto_action(
            s, loop="maintenance", action_type="stock_exit_submitted",
            gate_result=_synthetic_passed_gate(), symbol=intent.symbol,
            intent_id=intent.id,
            payload={"qty": qty, "limit": limit, "kind": exit_kind, "reason": reason},
            outcome="submitted", ibkr_order_id=order_id,
        )
    await alert(
        level="warning",
        title=f"Stock EXIT: {intent.symbol}",
        body=f"{qty} sh @ LMT ${limit}: {reason}",
    )


async def _flag_pmcc_close(*, intent: TradeIntent, reason: str, kind: str) -> None:
    """Auto-fire PMCC close: SELL LEAP + BUY-TO-CLOSE short, walked at
    net credit through the existing combo executor.

    Falls back to flag+alert if auto-fire pre-conditions fail (missing
    conids, daily cap hit, IBKR unreachable) — operator still gets the
    signal in Slack and via the auto_actions audit row.
    """
    cfg = dict((intent.walking_config or {}))
    leap_conid = int(cfg.get("leap_conid", 0))
    short_conid = int(cfg.get("short_call_conid", 0))
    if not leap_conid or not short_conid:
        async with db_session() as s:
            await record_auto_action(
                s, loop="maintenance", action_type="pmcc_close_flagged",
                gate_result=_synthetic_passed_gate(),
                symbol=intent.symbol, intent_id=intent.id,
                payload={"reason": reason, "kind": kind, "manual_required": "missing leg conids"},
                outcome="flagged",
            )
        await alert(
            level="warning",
            title=f"PMCC close — manual: {intent.symbol}",
            body=f"{reason}. Missing leg conids; close via /api/admin/positions/exit/{intent.id}.",
        )
        return

    if await _maintenance_cap_hit("close"):
        await alert(level="warning",
                    title=f"PMCC close skipped (daily cap): {intent.symbol}",
                    body=reason)
        async with db_session() as s:
            await record_auto_action(
                s, loop="maintenance", action_type="pmcc_close_skipped_cap",
                gate_result=_synthetic_passed_gate(),
                symbol=intent.symbol, intent_id=intent.id,
                payload={"reason": reason, "kind": kind},
                outcome="cap_hit",
            )
        return

    legs = [
        {"conid": leap_conid,  "ratio": 1, "action": "SELL"},
        {"conid": short_conid, "ratio": 1, "action": "BUY"},
    ]
    qty = int(intent.qty or 1)

    from api.app.positions import _ibkr
    from tradingagents.strategies.execution import (
        ExecutionConfig, submit_pmcc_combo,
    )
    try:
        ib = await _ibkr()
    except Exception as e:
        async with db_session() as s:
            await record_auto_action(
                s, loop="maintenance", action_type="pmcc_close_error",
                gate_result=_synthetic_passed_gate(),
                symbol=intent.symbol, intent_id=intent.id,
                payload={"reason": reason}, outcome="error", error=f"IBKR unreachable: {e}",
            )
        return

    async with db_session() as s:
        s.add(TradeAuditLog(
            intent_id=intent.id, action="auto_pmcc_close_attempt",
            payload={"symbol": intent.symbol, "legs": legs, "qty": qty,
                     "reason": reason, "kind": kind},
        ))

    result = await submit_pmcc_combo(
        ibkr=ib, symbol=intent.symbol, legs=legs, contracts=qty,
        action="SELL",   # close = receive net credit
        config=ExecutionConfig(
            initial_offset_cents=1, walk_increment_cents=1,
            walk_interval_sec=20, max_offset_pct_of_spread=0.50,
            timeout_sec=180,
        ),
    )

    async with db_session() as s:
        i = await s.get(TradeIntent, intent.id)
        if i:
            if result.status == "filled":
                i.status = "closed"
                i.position_state = "closed"
            elif result.status not in ("abandoned", "rejected_pretrade"):
                i.status = "error"
        s.add(TradeAuditLog(
            intent_id=intent.id, action="auto_pmcc_close_outcome",
            outcome=result.status, payload=result.to_dict(),
            error=result.error,
        ))
        await record_auto_action(
            s, loop="maintenance", action_type=f"pmcc_close_{result.status}",
            gate_result=_synthetic_passed_gate(),
            symbol=intent.symbol, intent_id=intent.id,
            payload={"reason": reason, "kind": kind, "execution": result.to_dict()},
            outcome=result.status,
        )

    if result.status == "filled":
        await alert(level="warning", title=f"PMCC CLOSED: {intent.symbol}",
                    body=f"@ ${result.fill_price:.2f} net credit. Reason: {reason}")
    elif result.status == "abandoned":
        await alert(level="warning", title=f"PMCC close abandoned: {intent.symbol}",
                    body=f"walked to floor; will retry next tick. {reason}")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _synthetic_passed_gate() -> Any:
    """Maintenance actions don't go through check_auto_action (they're
    not new entries). We still want them in auto_actions for audit, so
    we hand record_auto_action a synthetic 'passed' result."""
    from .auto_gate import AutoGateResult
    return AutoGateResult(passed=True, failures=[])


def _dte_from_str(yyyymmdd: str) -> Optional[int]:
    if not yyyymmdd or len(yyyymmdd) < 8:
        return None
    try:
        from datetime import date
        return (date.fromisoformat(f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}")
                - date.today()).days
    except Exception:
        return None


async def _compute_atr_30d(symbol: str) -> Optional[float]:
    """Wilder's ATR(30) — uses provider fallback chain (Polygon -> IBKR ->
    FMP -> yfinance) so a single provider rate-limit doesn't kill the
    indicator. None on total failure."""
    try:
        from datetime import date, timedelta
        from tradingagents.dataflows.fallback import get_stock_data_with_fallback
        # IBKR singleton from positions module so fallback can use it
        try:
            from api.app.positions import _ibkr
            ib = await _ibkr()
        except Exception:
            ib = None
        df = await get_stock_data_with_fallback(
            symbol, date.today() - timedelta(days=60), date.today(),
            ibkr_provider=ib,
        )
        if df is None or len(df) < 30 or "High" not in df.columns:
            return None
        # True range across the last 30 bars
        highs = df["High"].astype(float).iloc[-31:].tolist()
        lows  = df["Low"].astype(float).iloc[-31:].tolist()
        closes = df["Close"].astype(float).iloc[-31:].tolist()
        trs = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        if not trs:
            return None
        return sum(trs) / len(trs)
    except Exception as e:
        logger.warning("ATR fetch failed for %s: %s", symbol, e)
        return None


async def _compute_daily_signals(symbol: str) -> dict[str, Optional[float]]:
    """Daily indicator bundle used by the trim ladder + momentum exhaustion.

    Keys:
      pct_move_today     today's close vs yesterday's close (decimal)
      volume_ratio       today's volume / 20-day average (excl. today)
      rsi_14             Wilder's RSI(14)
      ma_20d             20-day simple moving average of close
      open_today         today's open
      prior_close        previous session's close
    """
    out: dict[str, Optional[float]] = {
        "pct_move_today": None, "volume_ratio": None, "rsi_14": None,
        "ma_20d": None, "open_today": None, "prior_close": None,
    }
    try:
        from datetime import date, timedelta
        from tradingagents.dataflows.fallback import get_stock_data_with_fallback
        try:
            from api.app.positions import _ibkr
            ib = await _ibkr()
        except Exception:
            ib = None
        df = await get_stock_data_with_fallback(
            symbol, date.today() - timedelta(days=60), date.today(),
            ibkr_provider=ib,
        )
        if df is None or len(df) < 20 or "Close" not in df.columns:
            return out
        closes = df["Close"].astype(float).tolist()
        opens = df["Open"].astype(float).tolist() if "Open" in df.columns else []
        volumes = df["Volume"].astype(float).tolist() if "Volume" in df.columns else []

        if len(closes) >= 2:
            out["pct_move_today"] = (closes[-1] - closes[-2]) / closes[-2]
            out["prior_close"] = closes[-2]
        if opens:
            out["open_today"] = opens[-1]

        if len(closes) >= 20:
            out["ma_20d"] = sum(closes[-20:]) / 20.0

        if len(volumes) >= 21 and volumes[-1] > 0:
            avg20 = sum(volumes[-21:-1]) / 20.0
            if avg20 > 0:
                out["volume_ratio"] = volumes[-1] / avg20

        if len(closes) >= 15:
            gains, losses = [], []
            for i in range(1, len(closes)):
                ch = closes[i] - closes[i - 1]
                gains.append(max(0.0, ch))
                losses.append(max(0.0, -ch))
            avg_gain = sum(gains[-14:]) / 14.0
            avg_loss = sum(losses[-14:]) / 14.0
            if avg_loss == 0:
                out["rsi_14"] = 100.0
            else:
                rs = avg_gain / avg_loss
                out["rsi_14"] = 100.0 - (100.0 / (1.0 + rs))
    except Exception as e:
        logger.warning("daily signals fetch failed for %s: %s", symbol, e)
    return out


async def _watch_8k_filings(pos_by_symbol: dict[str, dict]) -> dict[str, str]:
    """Sweep recent 8-K filings, write alerts, return per-symbol
    thesis-break strings for the high-severity ones.

    Returns dict mapping symbol -> reason string when a guidance- or
    after-hours-classified 8-K hit; the maint-loop's _evaluate_stock
    folds this into its existing thesis-break check.
    """
    from tradingagents.strategies.maintenance.sec_filings_watch import (
        FilingEvent, find_new_8k_events, thesis_break_signal_from_filings,
    )
    from api.app.db import ThemeSymbol

    # Build the union of symbols we care about: anything we hold + every
    # symbol in any theme universe (so news on candidates also lands).
    held = {(s or "").upper() for s in pos_by_symbol.keys()}
    async with db_session() as s:
        theme_rows = (
            await s.execute(select(ThemeSymbol.symbol).distinct())
        ).all()
    theme_syms = {(r[0] or "").upper() for r in theme_rows if r[0]}
    of_interest = held | theme_syms
    if not of_interest:
        return {}

    # Earnings calendar lookup so the severity classifier can tell an
    # earnings-cycle 8-K from a guidance / off-cycle one.
    earnings_by_symbol: dict[str, Any] = {}
    try:
        from tradingagents.strategies.maintenance.earnings import (
            get_earnings_dates_for,
        )
        earnings_by_symbol = await get_earnings_dates_for(of_interest)
    except Exception as e:
        logger.debug("earnings calendar lookup failed for filings watcher: %s", e)

    try:
        from tradingagents.dataflows.providers.fmp import FmpProvider
        fmp = FmpProvider()
    except Exception as e:
        logger.warning("filings watcher: FMP provider init failed: %s", e)
        return {}

    events: list[FilingEvent] = []
    async with db_session() as s:
        try:
            events = await find_new_8k_events(
                s, fmp_provider=fmp,
                symbols_of_interest=of_interest,
                earnings_dates_by_symbol=earnings_by_symbol,
            )
        except Exception as e:
            logger.warning("filings watcher: sweep failed: %s", e)

    if not events:
        return {}

    # Audit + alert per event
    breaks_by_sym: dict[str, str] = {}
    for ev in events:
        async with db_session() as s:
            await record_auto_action(
                s, loop="maintenance",
                action_type=f"filing_alert_{ev.severity}",
                gate_result=_synthetic_passed_gate(),
                symbol=ev.symbol,
                payload={
                    "form_type": ev.form_type,
                    "filing_date": ev.filing_date,
                    "accepted_date": ev.accepted_date,
                    "has_financials": ev.has_financials,
                    "severity": ev.severity,
                    "rationale": ev.rationale,
                    "link": ev.link,
                    "final_link": ev.final_link,
                    "is_held": ev.symbol in held,
                },
                outcome="flagged",
            )

        # Slack-style alert per filing — held positions get warning level,
        # universe-only filings stay at info.
        await alert(
            level=("warning" if ev.symbol in held else "info"),
            title=f"8-K {ev.severity}: {ev.symbol}"
                  + (" (held)" if ev.symbol in held else " (theme universe)"),
            body=f"{ev.rationale} · {ev.final_link or ev.link or ''}",
        )

    # Per-symbol thesis-break strings for the guidance/after-hours ones —
    # only on held positions (theme universe filings inform research, not
    # exit logic).
    held_events = [e for e in events if e.symbol in held]
    composed = thesis_break_signal_from_filings(held_events)
    if composed:
        # Spread the composite across the affected symbols so the per-stock
        # evaluator picks up only its own filings.
        for ev in held_events:
            if ev.severity in ("guidance", "after_hours"):
                breaks_by_sym[ev.symbol] = (
                    breaks_by_sym.get(ev.symbol, "")
                    + ("; " if ev.symbol in breaks_by_sym else "")
                    + f"{ev.severity} 8-K: {ev.rationale}"
                )
    return breaks_by_sym


async def _resolve_stuck_runs(idle_timeout_min: int = 15) -> int:
    """Mark any 'running' run with no event progress for >idle_timeout_min as failed.

    Detects HUNG runs (DeepSeek timeout edge cases, asyncio deadlock,
    provider stalls) by checking the latest run_event timestamp — not
    started_at, which would falsely kill legitimately slow runs.

    Logic:
      * If the run has events, the most recent event must be < idle_timeout_min ago
      * If the run has no events at all, started_at must be < idle_timeout_min ago
        (allows fresh runs to spin up agents before declaring them stuck)
    """
    from api.app.db import Run, RunEvent
    from sqlalchemy import func as sa_func

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=idle_timeout_min)
    count = 0
    async with db_session() as s:
        # Subquery: latest event timestamp per run
        latest_event = (
            select(
                RunEvent.run_id,
                sa_func.max(RunEvent.timestamp).label("latest"),
            )
            .group_by(RunEvent.run_id)
            .subquery()
        )
        rows = (
            await s.execute(
                select(Run, latest_event.c.latest)
                .outerjoin(latest_event, latest_event.c.run_id == Run.id)
                .where(Run.status == "running")
            )
        ).all()
        for run, last_evt in rows:
            # Use latest event time when present; otherwise fall back to
            # started_at so fresh runs get a grace window before judgment.
            ref_dt = last_evt or run.started_at
            if ref_dt is None:
                continue
            # SQLAlchemy may return tz-naive datetime when reading from
            # SQLite — coerce to UTC for safe comparison.
            if ref_dt.tzinfo is None:
                ref_dt = ref_dt.replace(tzinfo=timezone.utc)
            if ref_dt < cutoff:
                run.status = "failed"
                run.finished_at = datetime.now(timezone.utc)
                run.error = (
                    f"watchdog: no progress for >{idle_timeout_min} min "
                    f"(last activity: {ref_dt.isoformat()}). "
                    f"Likely a hung LLM/provider call. Re-trigger from the UI."
                )
                count += 1
    if count:
        logger.warning("maint loop: watchdog auto-failed %d stuck run(s)", count)
    return count


async def run_closing_accumulation_sweep() -> dict[str, Any]:
    """Sweep the theme-universe for closing-bell accumulation signals.

    Runs both gates (end-of-day quality + AH follow-through) plus the
    failure filters per the operator's confirmation-stack framework.
    Persists one row per (symbol, today's-date) into
    closing_accumulation_signals.

    Designed to run once per day at ~15:50 ET, ideally on its own cron
    (the data window is 15:50-16:30 ET). Can also be triggered manually
    via the admin endpoint for testing on any day's data.

    Returns: ``{themes_scanned, symbols_evaluated, setups_found,
                  details: [{symbol, confidence, ...}, ...]}``
    """
    from datetime import date as _date
    from tradingagents.strategies.maintenance.closing_accumulation import (
        sweep_theme_for_accumulation,
    )
    from api.app.db import ClosingAccumulationSignal, ThemeSymbol, Theme

    # Get IBKR singleton
    try:
        from api.app.positions import _ibkr
        ib = await _ibkr()
    except Exception as e:
        logger.warning("CBA sweep: IBKR unavailable: %s", e)
        return {"themes_scanned": 0, "symbols_evaluated": 0,
                "setups_found": 0, "error": str(e)}

    # Build the theme -> symbol map
    theme_symbols_map: dict[str, list[str]] = {}
    async with db_session() as s:
        rows = (
            await s.execute(
                select(Theme.id, ThemeSymbol.symbol)
                .join(ThemeSymbol, ThemeSymbol.theme_id == Theme.id)
            )
        ).all()
    for theme_id, sym in rows:
        if not sym:
            continue
        theme_symbols_map.setdefault(theme_id, []).append(sym.upper())

    if not theme_symbols_map:
        return {"themes_scanned": 0, "symbols_evaluated": 0,
                "setups_found": 0}

    today = _date.today()
    today_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    total_eval = 0
    setups_found = 0
    details: list[dict[str, Any]] = []

    for theme_id, symbols in theme_symbols_map.items():
        try:
            signals = await sweep_theme_for_accumulation(
                theme_id=theme_id, symbols=symbols, ibkr=ib,
            )
        except Exception as e:
            logger.warning("CBA: theme %s sweep failed: %s", theme_id, e)
            continue

        for sig in signals:
            total_eval += 1
            if sig.setup_passes:
                setups_found += 1

            # Upsert one row per (symbol, date). Pattern matches the
            # IV-snapshot capture: idempotent within the day.
            async with db_session() as s:
                existing = (
                    await s.execute(
                        select(ClosingAccumulationSignal.id)
                        .where(ClosingAccumulationSignal.symbol == sig.symbol)
                        .where(ClosingAccumulationSignal.date == today_dt)
                    )
                ).first()
                m = sig.metrics
                payload = dict(
                    symbol=sig.symbol,
                    date=today_dt,
                    theme_id=theme_id,
                    setup_passes=sig.setup_passes,
                    confidence=sig.confidence,
                    gate1_passes=sig.gate1_passes,
                    gate2_passes=sig.gate2_passes,
                    theme_confirmed=sig.theme_confirmed,
                    last_30m_rvol=(m.last_30m_rvol if m else None),
                    day_rvol=(m.day_rvol if m else None),
                    pct_session_above_vwap=(m.pct_session_above_vwap if m else None),
                    ah_print_count=(m.ah_print_count if m else None),
                    ah_cumulative_volume=(m.ah_cumulative_volume if m else None),
                    ah_holds_close=(m.ah_holds_close if m else None),
                    moc_price=(m.moc_price if m else None),
                    vwap=(m.vwap if m else None),
                    entry_recommendation=sig.entry_recommendation,
                    rationale=sig.rationale,
                    failure_filters=sig.failure_filters_tripped,
                )
                if existing:
                    await s.execute(
                        ClosingAccumulationSignal.__table__.update()
                        .where(ClosingAccumulationSignal.id == existing[0])
                        .values(**{k: v for k, v in payload.items()
                                   if k not in ("symbol", "date")})
                    )
                else:
                    s.add(ClosingAccumulationSignal(**payload))

            if sig.setup_passes or sig.confidence in ("high", "medium"):
                details.append({
                    "symbol": sig.symbol,
                    "theme_id": theme_id,
                    "confidence": sig.confidence,
                    "gate1": sig.gate1_passes,
                    "gate2": sig.gate2_passes,
                    "theme_confirmed": sig.theme_confirmed,
                    "entry": sig.entry_recommendation,
                    "rationale": sig.rationale,
                })

    logger.info(
        "CBA sweep done: %d themes, %d symbols evaluated, %d setups found",
        len(theme_symbols_map), total_eval, setups_found,
    )
    return {
        "themes_scanned": len(theme_symbols_map),
        "symbols_evaluated": total_eval,
        "setups_found": setups_found,
        "details": details,
    }


async def _watch_insider_universe() -> int:
    """Sweep FMP's universe-wide insider trade stream, filter to
    theme-universe symbols, audit fresh activity.

    Dedup uses auto_actions: if an insider_alert row for this
    (symbol, filingDate) tuple already exists, skip. That keeps the
    watcher idempotent across maint ticks.

    Returns the number of new alerts written. The aggregator
    (FmpProvider.get_insider_sell_pressure) still produces the
    momentum-exhaustion trip signal — this watcher is the early-
    warning row that surfaces *which* insider filed *what* before
    the 4-hour aggregator cache refreshes.
    """
    from api.app.db import AutoAction, ThemeSymbol
    from tradingagents.dataflows.providers.fmp import (
        FmpProvider, _is_insider_sale,
    )

    async with db_session() as s:
        theme_rows = (
            await s.execute(select(ThemeSymbol.symbol).distinct())
        ).all()
    universe = {(r[0] or "").upper() for r in theme_rows if r[0]}
    if not universe:
        return 0

    try:
        fmp = FmpProvider()
    except Exception as e:
        logger.debug("insider universe sweep: FMP init failed: %s", e)
        return 0

    rows: list[dict[str, Any]] = []
    try:
        rows = await fmp.get_recent_insider_trades(limit=100)
    except Exception as e:
        logger.debug("insider universe sweep failed: %s", e)
        return 0

    # Filter to theme-universe symbols + sales only (P-Purchase rows go
    # through a different signal — buy-side accumulation, future work).
    matches: list[dict[str, Any]] = []
    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        if sym not in universe:
            continue
        if not _is_insider_sale(r):
            continue
        owner = (r.get("typeOfOwner") or "").lower()
        if not any(k in owner for k in ("officer", "director", "10")):
            continue
        matches.append(r)
    if not matches:
        return 0

    # Dedup against existing insider_alert rows from the last 7 days
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    seven_days_ago = today - timedelta(days=7)
    async with db_session() as s:
        prior = (
            await s.execute(
                select(AutoAction.symbol, AutoAction.payload)
                .where(AutoAction.action_type == "insider_alert")
                .where(AutoAction.timestamp >= seven_days_ago)
            )
        ).all()
    seen_keys: set[tuple[str, str]] = set()
    for sym, payload in prior:
        if isinstance(payload, dict):
            seen_keys.add(
                ((sym or "").upper(), str(payload.get("filing_date") or ""))
            )

    new_count = 0
    for r in matches:
        sym = str(r.get("symbol") or "").upper()
        filing_date = str(r.get("filingDate") or r.get("transactionDate") or "")
        if (sym, filing_date) in seen_keys:
            continue
        owner = r.get("typeOfOwner") or ""
        reporter = r.get("reportingName") or "?"
        try:
            qty = float(r.get("securitiesTransacted") or 0)
            price = float(r.get("price") or 0)
            usd = qty * price
        except (TypeError, ValueError):
            usd = 0.0

        async with db_session() as s:
            await record_auto_action(
                s, loop="maintenance", action_type="insider_alert",
                gate_result=_synthetic_passed_gate(),
                symbol=sym,
                payload={
                    "filing_date": filing_date,
                    "transaction_date": str(r.get("transactionDate") or ""),
                    "reporting_name": reporter,
                    "owner_type": owner,
                    "transaction_type": r.get("transactionType"),
                    "shares": r.get("securitiesTransacted"),
                    "price": r.get("price"),
                    "value_usd": round(usd, 2),
                    "filing_link": r.get("link"),
                },
                outcome="flagged",
            )
        await alert(
            level="info",
            title=f"Insider sale: {sym} ({owner})",
            body=f"{reporter} sold {r.get('securitiesTransacted')} sh @ ${r.get('price')} ({filing_date})",
        )
        new_count += 1

    if new_count:
        logger.info("maint loop: %d new insider alerts (theme-universe filtered)", new_count)
    return new_count


async def _capture_daily_iv_snapshots(ib: Any) -> int:
    """Capture front-month ATM call IV for every theme-universe symbol
    that doesn't already have a snapshot today.

    Runs once per maint tick — the inner fetch is gated by the
    iv_signal.capture_iv_snapshot idempotency check, so subsequent
    calls in the same day no-op cheaply (one DB query per symbol).

    Returns the number of new rows written. Build-up over a few weeks
    will populate the percentile distribution; until then the IV-vs-
    realized fallback covers the signal.
    """
    from tradingagents.strategies.maintenance.iv_signal import (
        capture_iv_snapshot,
    )
    from api.app.db import ThemeSymbol

    async with db_session() as s:
        rows = (
            await s.execute(select(ThemeSymbol.symbol).distinct())
        ).all()
    syms = sorted({(r[0] or "").upper() for r in rows if r[0]})
    if not syms:
        return 0

    new_count = 0
    for sym in syms:
        try:
            async with db_session() as s:
                wrote = await capture_iv_snapshot(s, symbol=sym, ibkr=ib)
                if wrote:
                    new_count += 1
        except Exception as e:
            logger.debug("IV capture skipped for %s: %s", sym, e)
    if new_count:
        logger.info("maint loop: captured %d new IV snapshot(s)", new_count)
    return new_count


def _is_auction_window() -> bool:
    """True between 15:50 and 16:00 ET — when NYSE closing-auction
    imbalance ticks are streamed. IBKR returns None outside this window."""
    from zoneinfo import ZoneInfo
    et = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:        # Sat/Sun
        return False
    return et.hour == 15 and et.minute >= 50 or (et.hour == 16 and et.minute == 0)


async def _check_trimmed_today(symbol: str, *, kind: str) -> bool:
    """Return True if a trim was already submitted for ``symbol`` today.

    ``kind`` is "stock" or "leap" — matches the action_type prefix written
    by the trim primitives. Used by the trim evaluator to guarantee at most
    one trim per position per session.
    """
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    prefix = "stock_trim_" if kind == "stock" else "leap_trim_"
    async with db_session() as s:
        row = (
            await s.execute(
                select(AutoAction.id)
                .where(AutoAction.symbol == symbol)
                .where(AutoAction.action_type.like(f"{prefix}%"))
                .where(AutoAction.timestamp >= today)
                .limit(1)
            )
        ).first()
    return row is not None


async def _execute_stock_trim(
    *, intent: TradeIntent, trim_qty: int, decision: Any,
    signals: dict[str, Optional[float]], ib: Any,
) -> None:
    """Submit a partial-position SELL to take profits without closing.

    Mirrors ``_execute_stock_exit`` but only sells ``trim_qty`` shares
    and decrements ``intent.qty`` rather than transitioning to closed.
    """
    from ib_insync import Stock  # type: ignore
    from tradingagents.dataflows.providers.base import TradeIntent as IntentDC

    sym = intent.symbol
    ib_inst = await ib._ensure_connected()
    contract = Stock(sym, "SMART", "USD")
    qualified = await ib_inst.qualifyContractsAsync(contract)
    if not qualified:
        logger.warning("trim: could not qualify %s", sym)
        return
    contract = qualified[0]
    ticker = ib_inst.reqMktData(contract, "", False, False)
    await asyncio.sleep(1.5)
    bid = float(ticker.bid or 0)
    ask = float(ticker.ask or 0)
    last = float(ticker.last or 0)
    try:
        ib_inst.cancelMktData(contract)
    except Exception:
        pass

    if bid > 0:
        limit = round(bid - 0.01, 2)
    elif last > 0:
        limit = round(last - 0.01, 2)
    else:
        logger.warning("trim: no quote for %s; skipping", sym)
        return

    async with db_session() as s:
        s.add(TradeAuditLog(
            intent_id=intent.id, action="stock_trim_attempt",
            payload={"symbol": sym, "trim_qty": trim_qty,
                     "limit": limit, "band": decision.band,
                     "trim_pct": decision.trim_pct, "reason": decision.reason,
                     "signals": signals},
        ))

    intent_dc = IntentDC(
        ticker=sym, side="SELL", qty=int(trim_qty),
        order_type="LMT", limit_px=limit, tif="DAY", account_mode="paper",
    )
    try:
        result = await ib.submit_trade(intent_dc)
    except Exception as e:
        logger.exception("trim submit failed for %s: %s", sym, e)
        async with db_session() as s:
            s.add(TradeAuditLog(
                intent_id=intent.id, action="stock_trim_outcome",
                outcome="error", error=str(e),
            ))
        return

    order_id = str(result.get("ibkr_order_id") or "")
    async with db_session() as s:
        i = await s.get(TradeIntent, intent.id)
        if i:
            # Decrement qty by trim amount; intent stays "filled"/live.
            i.qty = max(0, (i.qty or 0) - int(trim_qty))
        s.add(TradeAuditLog(
            intent_id=intent.id, action="stock_trim_outcome",
            outcome="submitted",
            payload={"order_id": order_id, "limit": limit,
                     "trim_qty": trim_qty, "remaining_qty": i.qty if i else None},
        ))
        await record_auto_action(
            s, loop="maintenance",
            action_type=f"stock_trim_{decision.band}_submitted",
            gate_result=_synthetic_passed_gate(), symbol=sym,
            intent_id=intent.id,
            payload={"trim_qty": trim_qty, "trim_pct": decision.trim_pct,
                     "band": decision.band, "limit": limit,
                     "reason": decision.reason, "signals": signals},
            outcome="submitted", ibkr_order_id=order_id,
        )

    await alert(
        level="info",
        title=f"Stock TRIM ({decision.band}): {sym}",
        body=f"-{trim_qty} sh @ LMT ${limit} · {decision.reason}",
    )


# ---------------------------------------------------------------------------
# Daily caps for maintenance actions
# ---------------------------------------------------------------------------


async def _maintenance_cap_hit(kind: str) -> bool:
    """True if today's count of `kind` actions has hit the daily cap."""
    from api.app.autotrade.auto_gate import DEFAULT_CAPS
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "close":
        action_prefix = "pmcc_close_"
        cap = DEFAULT_CAPS["AUTO_MAX_CLOSES_PER_DAY"]
    elif kind == "roll":
        action_prefix = "pmcc_roll_"
        cap = DEFAULT_CAPS["AUTO_MAX_ROLLS_PER_DAY"]
    else:
        return False
    async with db_session() as s:
        from sqlalchemy import func
        n = (
            await s.execute(
                select(func.count())
                .select_from(AutoAction)
                .where(AutoAction.timestamp >= today)
                .where(AutoAction.loop == "maintenance")
                .where(AutoAction.action_type.like(f"{action_prefix}filled"))
            )
        ).scalar_one()
    return n >= cap


# ---------------------------------------------------------------------------
# Auto-fire executors — short-call close, short-call roll, LEAP forward roll
# ---------------------------------------------------------------------------


async def _execute_auto_short_call_close(intent: TradeIntent, ib: Any, reason: str) -> None:
    """Buy back the existing short call WITHOUT opening a new one.

    Earnings-hedge use case: closes the short leg 2 sessions before the
    print, leaves the LEAP uncapped through the move. Next maintenance
    tick after earnings will see the short side empty and (when
    sequenced-relisting is wired) re-establish.
    """
    cfg = dict((intent.walking_config or {}))
    short_conid = int(cfg.get("short_call_conid", 0))
    if not short_conid:
        try:
            q = await ib.get_option_quote(
                symbol=intent.symbol, expiry=intent.short_call_expiry,
                strike=float(intent.short_call_strike), right="C",
            )
            short_conid = int(q.get("conid", 0))
        except Exception as e:
            logger.warning("earnings close: cannot qualify short for %s: %s", intent.symbol, e)
            return
    if not short_conid or await _maintenance_cap_hit("close"):
        return

    qty = int(intent.qty or 1)
    legs = [{"conid": short_conid, "ratio": 1, "action": "BUY"}]
    async with db_session() as s:
        s.add(TradeAuditLog(
            intent_id=intent.id, action="auto_short_close_attempt",
            payload={"symbol": intent.symbol, "qty": qty, "reason": reason},
        ))

    from tradingagents.strategies.execution import ExecutionConfig, submit_pmcc_combo
    result = await submit_pmcc_combo(
        ibkr=ib, symbol=intent.symbol, legs=legs, contracts=qty, action="BUY",
        config=ExecutionConfig(
            initial_offset_cents=1, walk_increment_cents=1,
            walk_interval_sec=15, max_offset_pct_of_spread=0.50, timeout_sec=120,
        ),
    )

    async with db_session() as s:
        i = await s.get(TradeIntent, intent.id)
        if i and result.status == "filled":
            i.short_call_strike = None
            i.short_call_expiry = None
            i.short_call_delta_actual = None
            i.short_call_iv = None
            i.position_state = "leap_open_naked"
            cfg.pop("short_call_conid", None)
            cfg["earnings_hedge_closed_at"] = datetime.now(timezone.utc).isoformat()
            i.walking_config = cfg
        s.add(TradeAuditLog(
            intent_id=intent.id, action="auto_short_close_outcome",
            outcome=result.status, payload=result.to_dict(), error=result.error,
        ))
        await record_auto_action(
            s, loop="maintenance", action_type=f"pmcc_close_{result.status}",
            gate_result=_synthetic_passed_gate(),
            symbol=intent.symbol, intent_id=intent.id,
            payload={"reason": reason, "execution": result.to_dict(), "kind": "earnings_hedge"},
            outcome=result.status,
        )

    if result.status == "filled":
        await alert(level="warning",
                    title=f"Short call CLOSED (earnings hedge): {intent.symbol}",
                    body=f"@ ${result.fill_price:.2f}. LEAP runs uncapped through print.")


async def _execute_auto_short_call_roll(intent: TradeIntent, decision, ib: Any) -> None:
    """Auto-fire roll: BUY-TO-CLOSE old short + SELL-TO-OPEN new short
    as one atomic combo through the walking-limit executor."""
    if not decision.new_leg or not decision.new_leg.conid:
        return
    if await _maintenance_cap_hit("roll"):
        await alert(level="warning", title=f"Roll skipped (daily cap): {intent.symbol}",
                    body=decision.reason)
        return

    cfg = dict((intent.walking_config or {}))
    cur_short_conid = int(cfg.get("short_call_conid", 0))
    if not cur_short_conid:
        try:
            q = await ib.get_option_quote(
                symbol=intent.symbol, expiry=intent.short_call_expiry,
                strike=float(intent.short_call_strike), right="C",
            )
            cur_short_conid = int(q.get("conid", 0))
        except Exception:
            return
    if not (cur_short_conid and decision.new_leg.conid):
        return

    qty = int(intent.qty or 1)
    legs = [
        {"conid": cur_short_conid,         "ratio": 1, "action": "BUY"},
        {"conid": decision.new_leg.conid,  "ratio": 1, "action": "SELL"},
    ]
    async with db_session() as s:
        s.add(TradeAuditLog(
            intent_id=intent.id, action="auto_short_roll_attempt",
            payload={"symbol": intent.symbol, "qty": qty, "legs": legs,
                     "reason": decision.reason,
                     "old_strike": intent.short_call_strike,
                     "new_strike": decision.new_leg.strike,
                     "estimated_credit_capture": decision.estimated_credit_capture,
                     "estimated_net_debit": decision.estimated_net_debit},
        ))

    # Net direction: credit roll → action=SELL (we walk DOWN from credit-mid).
    # Debit roll (cost guard let it through) → action=BUY (walk UP).
    action = "BUY" if (decision.estimated_net_debit and decision.estimated_net_debit > 0) else "SELL"

    from tradingagents.strategies.execution import ExecutionConfig, submit_pmcc_combo
    result = await submit_pmcc_combo(
        ibkr=ib, symbol=intent.symbol, legs=legs, contracts=qty, action=action,
        config=ExecutionConfig(
            initial_offset_cents=1, walk_increment_cents=1,
            walk_interval_sec=15, max_offset_pct_of_spread=0.50, timeout_sec=120,
        ),
    )

    async with db_session() as s:
        i = await s.get(TradeIntent, intent.id)
        if i and result.status == "filled":
            i.short_call_expiry = decision.new_leg.expiry
            i.short_call_strike = decision.new_leg.strike
            i.short_call_delta_actual = decision.new_leg.delta
            i.short_call_iv = decision.new_leg.iv
            i.short_call_open_interest = decision.new_leg.open_interest
            cfg["short_call_conid"] = decision.new_leg.conid
            cfg["last_roll_at"] = datetime.now(timezone.utc).isoformat()
            cfg["last_roll_reason"] = decision.reason
            i.walking_config = cfg
        s.add(TradeAuditLog(
            intent_id=intent.id, action="auto_short_roll_outcome",
            outcome=result.status, payload=result.to_dict(), error=result.error,
        ))
        await record_auto_action(
            s, loop="maintenance", action_type=f"pmcc_roll_{result.status}",
            gate_result=_synthetic_passed_gate(),
            symbol=intent.symbol, intent_id=intent.id,
            payload={"reason": decision.reason,
                     "new_strike": decision.new_leg.strike,
                     "new_expiry": decision.new_leg.expiry,
                     "execution": result.to_dict()},
            outcome=result.status,
        )

    if result.status == "filled":
        await alert(level="info", title=f"Short call ROLLED: {intent.symbol}",
                    body=(f"to ${decision.new_leg.strike:.0f} {decision.new_leg.expiry} "
                          f"@ ${result.fill_price:.2f}. {decision.reason}"))


async def _execute_auto_leap_forward_roll(intent: TradeIntent, decision, ib: Any) -> None:
    """SELL old LEAP + BUY new LEAP, walked at net debit through the executor."""
    if not decision.new_leg or not decision.new_leg.conid:
        return
    if await _maintenance_cap_hit("roll"):
        return

    cfg = dict((intent.walking_config or {}))
    cur_leap_conid = int(cfg.get("leap_conid", 0))
    if not cur_leap_conid:
        try:
            q = await ib.get_option_quote(
                symbol=intent.symbol, expiry=intent.leap_expiry,
                strike=float(intent.leap_strike), right="C",
            )
            cur_leap_conid = int(q.get("conid", 0))
        except Exception:
            return
    if not cur_leap_conid:
        return

    qty = int(intent.qty or 1)
    legs = [
        {"conid": cur_leap_conid,          "ratio": 1, "action": "SELL"},
        {"conid": decision.new_leg.conid,  "ratio": 1, "action": "BUY"},
    ]
    async with db_session() as s:
        s.add(TradeAuditLog(
            intent_id=intent.id, action="auto_leap_roll_attempt",
            payload={"symbol": intent.symbol, "qty": qty, "legs": legs,
                     "old_strike": intent.leap_strike, "old_expiry": intent.leap_expiry,
                     "new_strike": decision.new_leg.strike, "new_expiry": decision.new_leg.expiry,
                     "reason": decision.reason},
        ))

    from tradingagents.strategies.execution import ExecutionConfig, submit_pmcc_combo
    result = await submit_pmcc_combo(
        ibkr=ib, symbol=intent.symbol, legs=legs, contracts=qty, action="BUY",
        config=ExecutionConfig(
            initial_offset_cents=1, walk_increment_cents=2,
            walk_interval_sec=30, max_offset_pct_of_spread=0.30, timeout_sec=300,
        ),
    )

    async with db_session() as s:
        i = await s.get(TradeIntent, intent.id)
        if i and result.status == "filled":
            i.leap_expiry = decision.new_leg.expiry
            i.leap_strike = decision.new_leg.strike
            i.leap_delta_actual = decision.new_leg.delta
            i.leap_iv = decision.new_leg.iv
            i.leap_open_interest = decision.new_leg.open_interest
            cfg["leap_conid"] = decision.new_leg.conid
            cfg["last_leap_roll_at"] = datetime.now(timezone.utc).isoformat()
            i.walking_config = cfg
        s.add(TradeAuditLog(
            intent_id=intent.id, action="auto_leap_roll_outcome",
            outcome=result.status, payload=result.to_dict(), error=result.error,
        ))
        await record_auto_action(
            s, loop="maintenance", action_type=f"pmcc_roll_{result.status}",
            gate_result=_synthetic_passed_gate(),
            symbol=intent.symbol, intent_id=intent.id,
            payload={"reason": decision.reason, "execution": result.to_dict(), "kind": "leap_forward"},
            outcome=result.status,
        )

    if result.status == "filled":
        await alert(level="info", title=f"LEAP rolled forward: {intent.symbol}",
                    body=(f"to ${decision.new_leg.strike:.0f} {decision.new_leg.expiry} "
                          f"@ ${result.fill_price:.2f}"))
