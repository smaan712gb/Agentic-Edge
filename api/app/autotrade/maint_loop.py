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
# Hard ceiling on a single tick. A full tick walks ~20+ positions with
# per-name provider fetches and legitimately runs 1.5-4 min; this ceiling
# sits well above that. It exists only so a stalled IBKR await (the
# 2026-06-29 "competing live session" freeze that wedged this loop for 17h)
# is cancelled instead of hanging the loop forever. On timeout the tick is
# cancelled and the loop continues at the next poll.
_TICK_TIMEOUT_SEC = 600


async def start_maintenance_loop() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop_forever(), name="maint_loop")
    from .supervisor import supervise
    supervise(_TASK, start_maintenance_loop, "maint_loop")
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
            await asyncio.wait_for(_tick(), timeout=_TICK_TIMEOUT_SEC)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.error(
                "maintenance tick exceeded %ds and was cancelled — likely a "
                "stalled IBKR market-data call. Loop continues at next poll.",
                _TICK_TIMEOUT_SEC,
            )
            try:
                await alert(
                    level="warning",
                    title="Maintenance loop tick timed out",
                    body=(f"Tick exceeded {_TICK_TIMEOUT_SEC}s and was cancelled "
                          "to prevent a permanent hang. Loop continues — check for "
                          "a competing IBKR market-data session. Exits are paused "
                          "until the next tick completes."),
                )
            except Exception:  # noqa: BLE001 — never let alerting wedge the loop
                pass
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

    # KILL SWITCH = "place no orders", NOT "stop watching the book".
    #
    # This used to `return` here, which silently disabled the ENTIRE exit layer:
    # no orphan adoption, no exit-pressure scoring, no 8-K/thesis-break
    # detection, no phantom reconcile — while `start-all.ps1 disarm` advertises
    # itself as "emergency: pause entries" and circuit_breaker's docstring
    # promises "the maintenance/exit loop is unaffected". Disarming during an
    # incident therefore blinded position management at the worst moment.
    #
    # Safe to continue instead: EVERY order-placing path in this module already
    # re-checks `_autotrade_active()` independently — _execute_off_theme_close,
    # _execute_stock_exit, _execute_stock_trim, _flag_pmcc_close (downgrades
    # auto-execute to flag+alert), _execute_auto_short_call_close/_roll, and
    # _execute_auto_leap_forward_roll. With the switch off the loop observes,
    # adopts, scores and ALERTS, but cannot trade.
    trading_enabled = bool(settings.AUTOTRADE_ENABLED)
    if trading_enabled:
        async with db_session() as s:
            state = await s.get(SystemState, 1)
            trading_enabled = bool(state is not None and state.autotrade_enabled)
    if not trading_enabled:
        logger.info(
            "maint loop: kill switch OFF — running in OBSERVE-ONLY mode "
            "(positions still monitored, exits flagged + alerted, no orders placed)"
        )

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

    # Keep the Position snapshot table fresh (stocks only) so the research
    # replay harness + dashboard don't read a stale snapshot. Best-effort —
    # the maint loop reads IBKR live and never depends on this table itself.
    try:
        from api.app.positions import snapshot_positions
        await snapshot_positions(positions)
    except Exception as e:
        logger.debug("maint loop: position snapshot failed: %s", e)

    # Adopt any IBKR position that doesn't have a TradeIntent yet — gives
    # the maintenance loop ownership of "orphan" positions (existing
    # holdings imported into the system, manual fills, etc.) so they get
    # monitored for exit triggers like everything else.
    orphans_adopted = await _adopt_orphan_positions(positions)

    # Adopt OPTION orphans too — a long-dated long call held at the broker with
    # no intent (manual fill, import, a lost intent row) is otherwise invisible
    # to exits/rotation forever (the silent-unmanaged-LEAP failure mode). The
    # stock adopter deliberately skips options; this handles bare LEAPs safely.
    try:
        orphans_adopted += await _adopt_orphan_leaps(positions)
    except Exception as e:
        logger.exception("leap orphan adoption failed: %s", e)

    # Exit-side reconcile: sync a managed leap intent's qty DOWN to broker truth
    # when the broker holds fewer contracts than the intent believes — catches a
    # trim/close that filled after the walker marked it abandoned (the late-fill
    # race), so the loop never manages or re-trims a stale quantity.
    try:
        await _reconcile_leap_qty(positions)
    except Exception as e:
        logger.exception("leap qty reconcile failed: %s", e)

    # Off-theme sweep: any stock long whose symbol isn't in any current
    # theme gets walked out so the capital can recycle into theme-aligned
    # candidates. Runs after orphan adoption so newly-adopted positions
    # have already been screened against the universe.
    try:
        off_theme_closed = await _sweep_off_theme_stocks(positions, ib)
        if off_theme_closed:
            logger.info("off-theme sweep: %d position(s) submitted for close", off_theme_closed)
    except Exception as e:
        logger.exception("off-theme sweep failed: %s", e)

    # Orphan PMCC reconciliation: catches the case where the walker's
    # per-call post-walk reconcile didn't fire (older intents abandoned
    # before that fix landed; or unusual race where reconcile itself
    # erred). For every PMCC intent in abandoned/error state, check if
    # its LEAP + short-call conids actually exist in the IBKR account —
    # if so, the trade filled and we need to flip the intent to filled
    # so maintenance logic can manage it. Without this, abandoned-but-
    # filled PMCCs sit naked, unmanaged.
    try:
        reconciled = await _reconcile_orphan_pmcc_intents(positions)
        if reconciled:
            logger.info("PMCC reconcile: marked %d previously-abandoned intent(s) as filled", reconciled)
    except Exception as e:
        logger.exception("PMCC reconcile failed: %s", e)

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

    # Daily IV snapshot capture — builds the front-month IV-percentile
    # distribution used by the momentum-exhaustion exit signal. This probes
    # ~30-day options across the whole universe, which is a SHORT-DATED
    # construct: irrelevant to a LEAPS-only book (we hold 2027/2028 LEAPs and
    # never trade front-month). Skip it entirely in LEAPS-only mode — it was
    # the source of the option-chain churn / Error 200 noise, and exit
    # pressure runs fine on its underlying-based signals without it.
    if not get_settings().LEAPS_ONLY:
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
                .where(TradeIntent.position_state.in_(
                    # PMCC states + LEAPS-only states. `leap_open` was MISSING,
                    # so the entire LEAPS-only book was invisible to the
                    # maintenance loop (no exits/rolls/rotation). leap_open is
                    # the filled bare-LEAP state set by _process_leaps_only.
                    ["pmcc_full", "leap_pending", "leap_open", "leap_open_naked", "closing"]))
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
            elif intent.structure in ("pmcc", "pmcc_sequenced", "leap_only"):
                # leap_only (LEAPS-only strategy) shares the LEAP close +
                # forward-roll logic; the short-call branches inside are guarded
                # by short_call presence, so they no-op for a bare long LEAP.
                await _evaluate_pmcc(intent, latest_decisions.get(sym), ib,
                                     pos=pos_by_symbol.get(sym),
                                     filing_thesis_break=filing_breaks_by_symbol.get(sym))
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
    # Iterate per-position, not per-symbol — a PMCC has two positions on
    # the same underlying (LEAP long + short call short) and a sym-keyed
    # dict would lose one of them, then the survivor gets misadopted as
    # a stock at the option's premium-per-contract price.
    stock_positions: list[dict] = []
    for p in ibkr_positions:
        sym = (p.get("symbol") or "").strip()
        if not sym:
            continue
        # Provider returns `secType` (camelCase from ib_insync); we also
        # accept snake_case for callers that already lower-cased it. If
        # the key is missing we treat it as UNKNOWN (skip), NOT as STK —
        # the old default-to-STK behaviour is what was misadopting LEAP
        # legs as stock orphans and causing accidental short sales.
        sec_type = str(p.get("secType") or p.get("sec_type") or "").upper()
        if sec_type != "STK":
            continue
        stock_positions.append(p)
    if not stock_positions:
        return 0

    syms = sorted({(p.get("symbol") or "").upper() for p in stock_positions})

    from api.app.db import ThemeSymbol

    async with db_session() as s:
        # Symbols already covered by a non-terminal intent. Include
        # PMCC-related states so that an abandoned/error PMCC intent
        # (which may flip to filled on the same tick via the orphan-PMCC
        # reconcile) still blocks a fake stock-orphan from being created
        # for the LEAP leg's underlying.
        rows = (
            await s.execute(
                select(TradeIntent.symbol)
                .where(TradeIntent.symbol.in_(syms))
                .where(TradeIntent.status.in_(
                    ["filled", "submitted", "submitting", "abandoned", "error", "closing"]))
                .where(TradeIntent.position_state.in_(
                    ["pmcc_full", "leap_pending", "leap_open", "leap_open_naked",
                     "pending", "abandoned", "closing"]))
            )
        ).all()
        owned = {(r[0] or "").upper() for r in rows}

        # Theme universe — only adopt positions in this set.
        theme_rows = (
            await s.execute(
                select(ThemeSymbol.symbol).where(ThemeSymbol.symbol.in_(syms))
            )
        ).all()
        in_theme_universe = {(r[0] or "").upper() for r in theme_rows if r[0]}

        new_count = 0
        skipped_non_theme = 0
        for pos in stock_positions:
            sym = (pos.get("symbol") or "").upper()
            if sym in owned:
                continue
            if sym not in in_theme_universe:
                skipped_non_theme += 1
                continue
            qty = float(pos.get("qty") or 0)
            if qty == 0:
                continue
            sec_type = "STK"  # Already filtered above; recorded for clarity below.
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


async def _adopt_orphan_leaps(ibkr_positions: list[dict]) -> int:
    """Adopt bare long-dated LONG CALLS held at the broker with no managing
    intent into `leap_only` intents, so exits/rotation/roll manage them.

    Guardrails (never misadopt a PMCC short leg or a random short-dated option):
      * long CALLS only (right C/CALL, qty > 0) — never a short/put leg;
      * long-dated only (DTE > 365) — genuine LEAPs, not front-month;
      * no active intent already covers the conid or symbol;
      * symbol is in the theme universe (same policy as the stock adopter).
    Premium (per-share) comes from the provider's normalized avg_price.
    """
    from api.app.db import ThemeSymbol

    cands: list[dict] = []
    for p in ibkr_positions:
        sec = str(p.get("secType") or p.get("sec_type") or "").upper()
        right = str(p.get("right") or "").upper()
        qty = float(p.get("qty") or 0)
        if sec != "OPT" or right not in ("C", "CALL") or qty <= 0:
            continue
        dte = _dte_from_str(str(p.get("expiry") or ""))
        if dte is None or dte <= 365:      # genuine LEAP only
            continue
        if not int(p.get("conid") or 0):
            continue
        cands.append(p)
    if not cands:
        return 0

    syms = sorted({(p.get("symbol") or "").upper() for p in cands})
    new_count = 0
    async with db_session() as s:
        owned_rows = (await s.execute(
            select(TradeIntent.symbol, TradeIntent.walking_config)
            .where(TradeIntent.status.in_(["filled", "submitting", "submitted", "closing"]))
            .where(TradeIntent.position_state.in_(
                ["pmcc_full", "leap_pending", "leap_open", "leap_open_naked", "closing"]))
        )).all()
        owned_syms = {(r[0] or "").upper() for r in owned_rows}
        owned_conids = {int((cfg or {}).get("leap_conid") or 0) for _sym, cfg in owned_rows}
        theme_syms = {
            (r[0] or "").upper() for r in (await s.execute(
                select(ThemeSymbol.symbol).where(ThemeSymbol.symbol.in_(syms)))).all() if r[0]
        }
        for p in cands:
            sym = (p.get("symbol") or "").upper()
            conid = int(p.get("conid") or 0)
            if conid in owned_conids or sym in owned_syms or sym not in theme_syms:
                continue
            qty = float(p.get("qty") or 0)
            premium = round(float(p.get("avg_price") or 0), 2) or None
            intent = TradeIntent(
                symbol=sym, side="BUY", qty=qty, order_type="LMT", limit_px=premium,
                status="filled", structure="leap_only", position_state="leap_open",
                leap_strike=float(p.get("strike") or 0) or None,
                leap_expiry=str(p.get("expiry") or "") or None,
                leap_qty=int(qty), leap_fill_price=premium, net_debit_filled=premium,
                leap_filled_at=datetime.now(timezone.utc),
                entry_strategy="adopted_orphan_leap",
                rationale=(f"Adopted orphan LEAP: {qty:.0f}x {sym} "
                           f"{p.get('strike')}C {p.get('expiry')} @ ${premium}/sh — held at "
                           f"broker with no intent; adopting into exit management."),
                walking_config={"leap_conid": conid, "source": "maint_loop_orphan_leap_adopt",
                                "adopted_at": datetime.now(timezone.utc).isoformat()},
            )
            s.add(intent)
            await s.flush()
            s.add(TradeAuditLog(
                intent_id=intent.id, action="orphan_leap_adopted", outcome="filled",
                payload={"symbol": sym, "conid": conid, "qty": qty, "leap_premium": premium}))
            owned_syms.add(sym); owned_conids.add(conid)
            new_count += 1
    if new_count:
        logger.warning("maint loop: adopted %d orphan LEAP(s) into exit management", new_count)
    return new_count


async def _reconcile_leap_qty(ibkr_positions: list[dict]) -> None:
    """Sync a managed leap_only intent's qty DOWN to broker truth when the
    broker holds fewer contracts than the intent believes. Catches a trim/close
    that filled after the walker marked it abandoned (late-fill race) so the
    loop never re-trims or manages a stale quantity. Never syncs UP (an increase
    is a fresh position the orphan-adopt path owns)."""
    by_conid: dict[int, float] = {}
    for p in ibkr_positions:
        cid = int(p.get("conid") or 0)
        if cid:
            by_conid[cid] = float(p.get("qty") or 0)
    async with db_session() as s:
        rows = (await s.execute(
            select(TradeIntent)
            .where(TradeIntent.structure == "leap_only")
            .where(TradeIntent.status == "filled")
            .where(TradeIntent.position_state.in_(["leap_open", "leap_open_naked", "closing"]))
        )).scalars().all()
        for i in rows:
            conid = int((i.walking_config or {}).get("leap_conid") or 0)
            if not conid or conid not in by_conid:
                continue           # phantom (no broker pos) handled in _evaluate_pmcc
            broker_qty = by_conid[conid]
            cur = float(i.qty or 0)
            if broker_qty > 0 and broker_qty < cur - 1e-6:
                old = cur
                i.qty = broker_qty
                if i.leap_qty is not None:
                    i.leap_qty = int(broker_qty)
                s.add(TradeAuditLog(
                    intent_id=i.id, action="leap_qty_reconciled_to_broker", outcome="reconciled",
                    payload={"symbol": i.symbol, "old_qty": old, "broker_qty": broker_qty,
                             "note": "broker holds fewer contracts than intent — likely a "
                                     "trim/close that filled after being marked abandoned"}))
                logger.warning("maint loop: %s qty reconciled %g -> %g (broker truth)",
                               i.symbol, old, broker_qty)


# ---------------------------------------------------------------------------
# Off-theme position sweep
# ---------------------------------------------------------------------------


async def _sweep_off_theme_stocks(ibkr_positions: list[dict], ib: Any) -> int:
    """Close stock positions whose symbol is not in any current theme.

    Off-theme positions consume buying power that could be redeployed
    against high-conviction theme candidates. The orphan-adopt step
    already excludes them (no TradeIntent is created), so they sit in
    the account untracked forever. This sweep walks them out with a
    marketable limit sell.

    Closes regardless of unrealized P&L — operator policy is "capital
    follows mandate"; anchoring on a single lot's drawdown defeats the
    rotation. Lots that subsequently rejoin a theme can be re-bought via
    the normal entry loop.

    Skips:
      * non-equity sec_types
      * positions with an active TradeIntent (handled by _evaluate_stock)
      * negative qty (shorts — separate flow)

    Returns the count of close orders submitted.
    """
    from api.app.db import ThemeSymbol
    from sqlalchemy import distinct

    # Build (sym, qty, pos) for longs only, sec_type=STK. Note: the IBKR
    # provider returns positions with key "secType" (camelCase from
    # ib_insync), not "sec_type" — orphan_adopt's snake_case lookup works
    # by accident because it defaults to "STK" on miss.
    stock_longs: list[tuple[str, float, dict]] = []
    for p in ibkr_positions:
        sec_type = str(p.get("secType") or p.get("sec_type") or "").upper()
        if sec_type != "STK":
            continue
        qty = float(p.get("qty") or 0)
        if qty <= 0:
            continue
        sym = (p.get("symbol") or "").upper()
        if not sym:
            continue
        stock_longs.append((sym, qty, p))
    if not stock_longs:
        return 0

    syms = [s for s, _, _ in stock_longs]
    async with db_session() as s:
        # Symbols in any active theme universe.
        in_universe_rows = (
            await s.execute(
                select(distinct(ThemeSymbol.symbol)).where(ThemeSymbol.symbol.in_(syms))
            )
        ).all()
        in_universe = {(r[0] or "").upper() for r in in_universe_rows if r[0]}

        # Symbols already under management (active intent). Those route
        # through _evaluate_stock / _evaluate_pmcc; don't double-close.
        managed_rows = (
            await s.execute(
                select(TradeIntent.symbol)
                .where(TradeIntent.symbol.in_(syms))
                .where(TradeIntent.status.in_(["filled", "submitted", "submitting", "closing"]))
                .where(TradeIntent.position_state.in_(
                    ["pmcc_full", "leap_pending", "leap_open", "leap_open_naked",
                     "pending", "closing"]
                ))
            )
        ).all()
        managed = {(r[0] or "").upper() for r in managed_rows}

    off_theme = [(sym, qty, pos) for sym, qty, pos in stock_longs
                 if sym not in in_universe and sym not in managed]
    if not off_theme:
        return 0

    logger.info(
        "off-theme sweep: %d position(s) not in any theme: %s",
        len(off_theme), [s for s, _, _ in off_theme],
    )

    closed = 0
    for sym, qty, pos in off_theme:
        try:
            # Gate the close behind a real exit signal. Leaving a theme is not
            # itself a reason to dump a high-beta name on a red day — only
            # rotate out when the name is ALSO weakening (thesis broken or
            # momentum rolled over below its 20d MA). Off-theme winners that
            # still hold their trend are retained; the operator can re-home
            # them into a theme.
            exit_signal = await _off_theme_exit_signal(sym, pos)
            if exit_signal is None:
                logger.info("off-theme sweep: retaining %s — no exit signal (off-theme but holding trend)", sym)
                continue
            await _execute_off_theme_close(sym=sym, qty=qty, pos=pos, ib=ib, exit_signal=exit_signal)
            closed += 1
        except Exception as e:
            logger.exception("off-theme close failed for %s: %s", sym, e)
    return closed


async def _off_theme_exit_signal(sym: str, pos: dict) -> Optional[str]:
    """Return an exit reason if an off-theme name is ALSO weakening, else None.

    Reuses the same signals the managed-position path uses: a thesis break is
    a hard rotate-out; otherwise a name trading below its 20-day MA has lost
    momentum and is fair to recycle. A name still above its 20d MA (or with
    too little history to judge) is retained — we don't dump trend-holding
    winners just because they left a theme."""
    from tradingagents.strategies.maintenance.theme_health import get_thesis_break_signal
    async with db_session() as s:
        thesis_break = await get_thesis_break_signal(s, sym)
    if thesis_break:
        return f"thesis broken: {thesis_break}"
    signals = await _compute_daily_signals(sym)
    ma20 = signals.get("ma_20d")
    last = float(pos.get("last_price") or 0)
    if ma20 and last > 0 and last < ma20:
        return f"momentum rolled over (last ${last:.2f} < 20d MA ${ma20:.2f})"
    return None


async def _autotrade_active() -> bool:
    """Live dual kill-switch check, re-read immediately before each order so an
    operator ``disable`` halts within ONE order — not after the whole 5-10 min
    tick. The tick's single top-of-loop check let exits/trims/rolls keep firing
    for minutes after a disable. Fails CLOSED (unreadable state → no order)."""
    if not get_settings().AUTOTRADE_ENABLED:
        return False
    try:
        async with db_session() as s:
            st = await s.get(SystemState, 1)
            return bool(st and st.autotrade_enabled)
    except Exception:
        return False


async def _execute_off_theme_close(*, sym: str, qty: float, pos: dict, ib: Any,
                                   exit_signal: str = "") -> None:
    """Submit a marketable LMT SELL to close one off-theme stock position.

    Only called once the off-theme name has *also* shown an exit signal
    (``exit_signal``) — see ``_off_theme_exit_signal``. Creates a synthetic
    TradeIntent so the close has a full audit trail (TradeAuditLog +
    auto_actions) symmetric with every other system trade.
    """
    if not await _autotrade_active():
        logger.info("maint: kill switch active — skipping off-theme close for %s", sym)
        return
    from ib_insync import Stock  # type: ignore
    from tradingagents.dataflows.providers.base import TradeIntent as IntentDC

    ib_inst = await ib._ensure_connected()
    contract = Stock(sym, "SMART", "USD")
    qualified = await ib_inst.qualifyContractsAsync(contract)
    if not qualified:
        logger.warning("off-theme close: could not qualify %s", sym)
        return
    contract = qualified[0]
    ticker = ib_inst.reqMktData(contract, "", False, False)
    await asyncio.sleep(1.5)
    bid = float(ticker.bid or 0)
    ask = float(ticker.ask or 0)
    last = float(ticker.last or 0)
    try: ib_inst.cancelMktData(contract)
    except Exception: pass

    if bid > 0:
        limit = round(bid - 0.01, 2)
    elif last > 0:
        limit = round(last - 0.01, 2)
    else:
        logger.warning("off-theme close: no quote for %s; skipping", sym)
        return

    avg = float(pos.get("avg_price") or 0)
    async with db_session() as s:
        intent = TradeIntent(
            symbol=sym, side="SELL", qty=int(qty),
            limit_px=limit,
            status="closing", structure="stock",
            position_state="closing",
            entry_strategy="off_theme_close",
            rationale=(
                f"Off-theme rotation: {sym} not in any active theme AND showing "
                f"an exit signal ({exit_signal or 'n/a'}). Closing {qty:.0f} sh "
                f"(cost basis ${avg:.2f}) to free capital for theme-aligned "
                f"candidates. (Off-theme names still holding their trend are retained.)"
            ),
            walking_config={
                "source": "off_theme_sweep",
                "exit_signal": exit_signal,
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "avg_price": avg, "bid_at_close": bid, "last_at_close": last,
            },
        )
        s.add(intent)
        await s.flush()
        intent_id = intent.id
        s.add(TradeAuditLog(
            intent_id=intent_id, action="off_theme_close_attempt",
            payload={"symbol": sym, "qty": qty, "limit": limit, "avg": avg,
                     "bid": bid, "ask": ask},
        ))

    intent_dc = IntentDC(
        ticker=sym, side="SELL", qty=int(qty),
        order_type="LMT", limit_px=limit, tif="DAY", account_mode=get_settings().IBKR_MODE,
    )
    try:
        result = await ib.submit_trade(intent_dc)
    except Exception as e:
        logger.exception("off-theme close submit failed for %s: %s", sym, e)
        async with db_session() as s:
            s.add(TradeAuditLog(
                intent_id=intent_id, action="off_theme_close_outcome",
                outcome="error", payload={"error": str(e)},
            ))
        return

    order_id = str(result.get("ibkr_order_id") or "")
    async with db_session() as s:
        s.add(TradeAuditLog(
            intent_id=intent_id, action="off_theme_close_outcome",
            outcome="submitted",
            payload={"order_id": order_id, "limit": limit, "qty": qty},
        ))
        await record_auto_action(
            s, loop="maintenance", action_type="off_theme_stock_close",
            gate_result=_synthetic_passed_gate(), symbol=sym,
            intent_id=intent_id,
            payload={"qty": qty, "limit": limit, "avg": avg,
                     "reason": "off_theme_rotation", "exit_signal": exit_signal},
            outcome="submitted", ibkr_order_id=order_id,
        )
    await alert(
        level="info",
        title=f"Off-theme rotation: {sym}",
        body=(f"{qty:.0f} sh @ LMT ${limit} (cost basis ${avg:.2f}). Off-theme "
              f"AND weakening: {exit_signal or 'n/a'}."),
    )


# ---------------------------------------------------------------------------
# Orphan PMCC reconciliation
# ---------------------------------------------------------------------------


async def _reconcile_orphan_pmcc_intents(ibkr_positions: list[dict]) -> int:
    """Flip abandoned/error PMCC intents to filled when broker holds the legs.

    The walker now has a post-walk reconcile inside each execution call,
    but two cases still need a sweep-level catch-up:

      1. Intents abandoned BEFORE the per-walker reconcile fix landed
         (legacy state from yesterday's session). Without this sweep
         they'd never re-enter the maintenance flow.

      2. Edge cases where the per-walker reconcile itself errored
         (IBKR temporarily unreachable during the post-walk check).
         The position is in the account; the intent is stuck.

    Match strategy: intent.walking_config carries ``leap_conid`` and
    ``short_call_conid``. If both conids appear in the current IBKR
    positions list with non-zero qty in the expected directions
    (LEAP qty>0, short call qty<0), the trade actually filled.

    Returns the count of intents reconciled.
    """
    # Build a conid -> position map from the current IBKR snapshot.
    by_conid: dict[int, dict] = {}
    for p in ibkr_positions:
        cid = int(p.get("conid") or 0)
        if cid:
            by_conid[cid] = p
    if not by_conid:
        return 0

    async with db_session() as s:
        # Pull every abandoned/error PMCC intent. Cheap — the table is small
        # and we filter by status.
        rows = (
            await s.execute(
                select(TradeIntent)
                .where(TradeIntent.structure.in_(["pmcc", "pmcc_sequenced"]))
                .where(TradeIntent.status.in_(["abandoned", "error"]))
                .where(TradeIntent.position_state.in_(["abandoned", "pending"]))
            )
        ).scalars().all()

        reconciled_count = 0
        for intent in rows:
            cfg = intent.walking_config or {}
            leap_conid = int(cfg.get("leap_conid") or 0)
            short_conid = int(cfg.get("short_call_conid") or 0)
            if not leap_conid or not short_conid:
                continue
            leap_pos = by_conid.get(leap_conid)
            short_pos = by_conid.get(short_conid)
            if leap_pos is None or short_pos is None:
                continue
            leap_qty = float(leap_pos.get("qty") or 0)
            short_qty = float(short_pos.get("qty") or 0)
            if leap_qty <= 0 or short_qty >= 0:
                # Wrong direction — not a clean PMCC pair on these conids.
                continue

            # IbkrProvider.get_positions already normalizes option avg_price to
            # PER-SHARE premium (see ibkr.py). The old comment/code here divided
            # by 100 AGAIN, storing a cost basis 100× too small. Use avg_price
            # directly, consistent with the leap-only branch below.
            leap_premium = round(float(leap_pos.get("avg_price") or 0), 2) or None
            short_premium = round(float(short_pos.get("avg_price") or 0), 2) or None
            net_debit = None
            if leap_premium is not None and short_premium is not None:
                net_debit = round(leap_premium - short_premium, 2)

            now = datetime.now(timezone.utc)
            intent.status = "filled"
            intent.position_state = "pmcc_full"
            intent.leap_fill_price = leap_premium
            intent.short_call_fill_price = short_premium
            intent.net_debit_filled = net_debit
            if intent.leap_filled_at is None:
                intent.leap_filled_at = now
            if intent.short_call_filled_at is None:
                intent.short_call_filled_at = now
            cfg = dict(cfg)
            cfg["reconciled_at"] = now.isoformat()
            cfg["reconcile_source"] = "maint_loop_orphan_pmcc"
            intent.walking_config = cfg

            s.add(TradeAuditLog(
                intent_id=intent.id, action="orphan_pmcc_reconciled",
                outcome="filled",
                payload={
                    "leap_conid": leap_conid, "short_call_conid": short_conid,
                    "leap_qty": leap_qty, "short_qty": short_qty,
                    "leap_premium": leap_premium, "short_premium": short_premium,
                    "net_debit": net_debit,
                    "note": (
                        "Broker positions show this PMCC actually filled; "
                        "intent was marked abandoned by walker timeout. "
                        "Flipping to filled so maintenance can manage it."
                    ),
                },
            ))
            await record_auto_action(
                s, loop="maintenance",
                action_type="orphan_pmcc_reconciled",
                gate_result=_synthetic_passed_gate(),
                symbol=intent.symbol, intent_id=intent.id,
                payload={"leap_premium": leap_premium, "short_premium": short_premium,
                         "net_debit": net_debit, "qty_leap": leap_qty, "qty_short": short_qty},
                outcome="reconciled",
            )
            reconciled_count += 1
            logger.warning(
                "PMCC reconcile: %s intent %s flipped to filled "
                "(broker has both legs; net debit ${:.2f})".format(net_debit or 0.0),
                intent.symbol, intent.id[:8],
            )

        # ---- LEAPS-only orphans -------------------------------------
        # Same race as PMCC, but a bare long LEAP has NO short call, so
        # the pair-match above (which requires short_qty < 0) can never
        # fire for it. Match on leap_conid alone with qty > 0. Without
        # this, an Adaptive LEAP order that the walker timed-out on but
        # IBKR filled afterward sits unmanaged — no exits, no rotation
        # protection — which is exactly the silent-drawdown failure mode.
        leap_rows = (
            await s.execute(
                select(TradeIntent)
                .where(TradeIntent.structure == "leap_only")
                .where(TradeIntent.status.in_(["abandoned", "error"]))
                .where(TradeIntent.position_state.in_(["abandoned", "pending"]))
            )
        ).scalars().all()

        for intent in leap_rows:
            cfg = intent.walking_config or {}
            leap_conid = int(cfg.get("leap_conid") or 0)
            if not leap_conid:
                continue
            leap_pos = by_conid.get(leap_conid)
            if leap_pos is None:
                continue
            leap_qty = float(leap_pos.get("qty") or 0)
            if leap_qty <= 0:
                continue

            # IbkrProvider.get_positions reports avg_price as the PER-SHARE
            # option premium (its P&L uses (last-avg)*100*qty), so the entry
            # path stores leap_fill_price = avg_price directly. Do the same
            # here — do NOT divide by 100 (that older convention, still in the
            # PMCC branch above, mismatches this provider's units).
            leap_premium = round(float(leap_pos.get("avg_price") or 0), 2) or None

            now = datetime.now(timezone.utc)
            intent.status = "filled"
            intent.position_state = "leap_open"
            intent.leap_fill_price = leap_premium
            intent.net_debit_filled = leap_premium
            if intent.leap_filled_at is None:
                intent.leap_filled_at = now
            cfg = dict(cfg)
            cfg["reconciled_at"] = now.isoformat()
            cfg["reconcile_source"] = "maint_loop_orphan_leap"
            intent.walking_config = cfg

            s.add(TradeAuditLog(
                intent_id=intent.id, action="orphan_leap_reconciled",
                outcome="filled",
                payload={
                    "leap_conid": leap_conid, "leap_qty": leap_qty,
                    "leap_premium": leap_premium,
                    "note": (
                        "Broker holds this LEAP; intent was marked abandoned "
                        "by the Adaptive-order walker timeout but IBKR filled "
                        "it afterward. Flipping to filled so maintenance "
                        "(exits/rotation/roll) can manage it."
                    ),
                },
            ))
            await record_auto_action(
                s, loop="maintenance",
                action_type="orphan_leap_reconciled",
                gate_result=_synthetic_passed_gate(),
                symbol=intent.symbol, intent_id=intent.id,
                payload={"leap_premium": leap_premium, "qty_leap": leap_qty},
                outcome="reconciled",
            )
            reconciled_count += 1
            logger.warning(
                "LEAP reconcile: %s intent %s flipped to filled "
                "(broker holds %g contracts; unmanaged orphan recovered)",
                intent.symbol, intent.id[:8], leap_qty,
            )

    return reconciled_count


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
    # Freshness gate: if no live tick arrived, last_price is a cost-basis
    # substitution (looks flat) — acting on it would SUPPRESS a real drawdown's
    # exit. Skip price-driven exit logic this tick and surface it rather than
    # deciding on a frozen price. (Absent field = assume fresh, e.g. tests.)
    if pos.get("price_fresh", True) is False:
        logger.warning("maint: %s has no fresh price (cost-basis fallback) — "
                       "skipping price-driven exit this tick", intent.symbol)
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

    # ---- Rotation overlay -------------------------------------------
    # If any theme this name lives in is flagged "rotating out", take profit
    # on winners harder (treat as not-hot so the profit-preservation ladder
    # trims) and tighten exit-pressure below. Never forces a loser out — the
    # trim ladder is profit-gated and the exit-pressure bump is bounded.
    rotation_active = False
    rotation_themes: list[str] = []
    try:
        from .rotation_detector import any_theme_rotating
        from api.app.db import ThemeSymbol as _ThemeSymbol
        async with db_session() as s:
            sym_theme_ids = [
                r[0] for r in (await s.execute(
                    select(_ThemeSymbol.theme_id).where(_ThemeSymbol.symbol == intent.symbol)
                )).all() if r[0]
            ]
        rotation_active, rotation_themes = await any_theme_rotating(sym_theme_ids)
        if rotation_active:
            theme_hot = False  # let the profit ladder lock gains on winners
            logger.info("maint: %s in rotating theme(s) %s — taking profit on strength + tightening exit",
                        intent.symbol, rotation_themes)
    except Exception as e:
        logger.debug("rotation overlay failed for %s: %s", intent.symbol, e)

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
        DEFAULT_WEIGHTS,
        LEAPS_WEIGHTS,
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

    # IV signal (8th exhaustion indicator) — front-month ATM IV percentile.
    # This is a SHORT-DATED construct (30-day options); a LEAPS-only book
    # doesn't trade front-month, so skip the probe entirely. Momentum
    # exhaustion still fires on its price / RSI / volume / auction / insider /
    # analyst inputs (all underlying-based and more appropriate for a LEAP).
    iv_signal_obj = None
    if not get_settings().LEAPS_ONLY:
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

    # Fold the theme-rotation signal into exit pressure: take the stronger of
    # the name-level rotation candidate delta and the theme-rotation delta, so
    # a confirmed sector rotation tightens the existing (bounded) exit signal.
    _name_rot = rotation_candidate.score_delta if rotation_candidate else 0.0
    _theme_rot = get_settings().ROTATION_EXIT_PRESSURE_DELTA if rotation_active else 0.0
    _rotation_delta = max(_name_rot or 0.0, _theme_rot) or None
    # LEAPS-only book: drop the structurally-dead options_risk weight and
    # renormalise the surviving four (see exit_pressure.LEAPS_WEIGHTS). Keeping
    # options_risk at 15% would deflate every score by ~15% and make positions
    # under-fire across the trim/exit bands.
    _pressure_weights = LEAPS_WEIGHTS if get_settings().LEAPS_ONLY else DEFAULT_WEIGHTS
    # Quant overlay in the EXIT decision: the research factory's per-symbol edge
    # nudges exit pressure — a structurally strong name (centrality + smart
    # money + momentum) holds longer; a weak one trims sooner. Bounded by
    # QUANT_EXIT_MAX_DELTA (< rotation's 25) so it never forces a drawdown exit.
    _quant_exit_delta = None
    if get_settings().QUANT_OVERLAY_ENABLED:
        try:
            from api.app.research.overlay import symbol_exit_delta
            _quant_exit_delta = await symbol_exit_delta(intent.symbol) or None
        except Exception as e:
            logger.debug("maint loop: quant exit delta failed for %s: %s", intent.symbol, e)
    pressure = compute_exit_pressure(
        theme_composite=(best_theme.composite if best_theme else None),
        theme_streak_days=(best_theme.streak_days_below_floor if best_theme else 0),
        trim_band=trim.band,
        exhaustion_score=exhaustion.score,
        rotation_score_delta=_rotation_delta,
        quant_edge_delta=_quant_exit_delta,
        weights=_pressure_weights,
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
                "quant_exit_delta": _quant_exit_delta,
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


async def _live_intraday_change_pct(symbol: str) -> Optional[float]:
    """Current intraday % change vs prior close from a LIVE quote (decimal,
    e.g. -0.14). Daily-bar data lags a full day intraday, so any same-day
    reactive logic (the earnings-miss detector) must use a live quote to see a
    crash. None if unavailable."""
    try:
        from tradingagents.dataflows.providers.fmp import FmpProvider
        p = FmpProvider()
        body = await p._http.get_json(
            "/stable/quote", params={"symbol": symbol, "apikey": p._api_key})
        if isinstance(body, list) and body:
            ch = body[0].get("changePercentage")
            if ch is not None:
                return float(ch) / 100.0
    except Exception as e:
        logger.debug("live intraday change fetch failed for %s: %s", symbol, e)
    return None


async def _evaluate_pmcc(intent: TradeIntent, latest_decision: Optional[str], ib: Any,
                         pos: Optional[dict] = None,
                         filing_thesis_break: Optional[str] = None) -> None:
    """Roll/exit decisions for a LEAP position. Auto-executes genuine
    structural/fundamental exits (theme + filing thesis break, earnings crash,
    DTE cliff, catastrophic slow-bleed stop); flags price-driven ones (Avoid,
    delta decay, graded exit-pressure) for operator confirm."""
    from tradingagents.strategies.maintenance.exits import maybe_close_pmcc
    from tradingagents.strategies.maintenance.earnings import days_to_earnings
    from tradingagents.strategies.maintenance.theme_health import get_thesis_break_signal

    sym = (intent.symbol or "").upper()

    # PHANTOM auto-close: the broker shows no position for this filled intent
    # (closed outside the system / expired / assigned / a late-exit fill). Only
    # the stock path used to handle this; leap_only phantoms lingered forever,
    # re-quoting a position that doesn't exist. Reconcile to closed and stop.
    if pos is None:
        async with db_session() as s:
            i = await s.get(TradeIntent, intent.id)
            if i and i.status == "filled":
                i.status = "closed"
                i.position_state = "closed"
                s.add(TradeAuditLog(
                    intent_id=i.id, action="phantom_leap_auto_closed", outcome="closed",
                    payload={"symbol": sym, "note": "broker holds no matching position; "
                             "reconciled to closed by maint loop"}))
        return

    # THEME thesis-break — reacts to a broken thesis WITHOUT a discrete event.
    # Previously wired only to the stock path, so a held LEAP could ride a slow
    # thesis erosion to zero. Route it here and AUTO-CLOSE (a confirmed
    # multi-day theme break across ALL of a name's themes is fundamental).
    try:
        async with db_session() as s:
            theme_break = await get_thesis_break_signal(s, sym)
    except Exception as e:
        theme_break = None
        logger.debug("theme thesis-break check failed for %s: %s", sym, e)
    if theme_break:
        await _flag_pmcc_close(intent=intent, reason=f"theme thesis broken: {theme_break}",
                               kind="thesis_break")
        return

    # CATASTROPHIC slow-bleed STOP — the 100%-loss backstop. Fires ONLY on a
    # large premium loss CONFIRMED by a structural downtrend (underlying below
    # its 200-day MA), never on a one-day beta drop.
    try:
        entry_prem = float(intent.leap_fill_price or intent.net_debit_target or 0)
        cur_prem = float(pos.get("last_price") or 0)
        # Require a FRESH price — never auto-close on a cost-basis/frozen fallback.
        if entry_prem > 0 and cur_prem > 0 and pos.get("price_fresh", True):
            down = (entry_prem - cur_prem) / entry_prem
            if down >= get_settings().LEAP_CATASTROPHIC_STOP_PCT and await _underlying_in_downtrend(sym, ib):
                await _flag_pmcc_close(
                    intent=intent,
                    reason=(f"catastrophic stop: LEAP down {down:.0%} from entry "
                            f"(${entry_prem:.2f}->${cur_prem:.2f}) with underlying below its "
                            f"200-day MA — structural break, not a one-day drop"),
                    kind="catastrophic_stop")
                return
    except Exception as e:
        logger.debug("catastrophic-stop check failed for %s: %s", sym, e)

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

    # 8-K / earnings thesis-break (e.g. a guidance-cut or earnings-miss filing).
    # LEAP positions previously NEVER received this signal — only stocks did —
    # so a held LEAP could ride straight through a thesis-changing filing. Treat
    # a high-severity filing break like an Avoid: flag the LEAP for close. (This
    # is the EXISTING thesis-break mechanism, now correctly routed to LEAPs.)
    if filing_thesis_break:
        await _flag_pmcc_close(
            intent=intent,
            reason=f"filing thesis-break: {filing_thesis_break}",
            kind="thesis_break",
        )
        return

    # Earnings-miss crash detector (high-beta-aware). This is the ONLY exit
    # path allowed to fire on a sharp drop — and it is GATED ON EARNINGS
    # PROXIMITY, never raw drawdown, so a normal high-beta down day (no earnings
    # event) can't trip it. It arms only within EARNINGS_BREAK_SESSIONS of a
    # report AND only on a move worse than EARNINGS_BREAK_DROP_PCT. The AVGO
    # case (-25% the session after a miss) trips it; a routine -8% beta day does
    # not. Flags + alerts (operator-confirmed close), not a naked auto-dump.
    try:
        from tradingagents.strategies.maintenance.earnings import days_since_last_earnings
        _cfg = get_settings()
        dse = await days_since_last_earnings(intent.symbol)
        if dse is not None and 0 <= dse <= _cfg.EARNINGS_BREAK_SESSIONS:
            # Use a LIVE intraday quote, NOT daily bars: provider daily bars lag
            # a full day intraday, so they read yesterday's move and would miss
            # a same-day earnings crash (this is exactly why AVGO's -14% wasn't
            # seen). FMP /quote gives the live % change vs prior close.
            move = await _live_intraday_change_pct(intent.symbol)
            if move is None:  # fall back to daily bar only if live quote unavailable
                move = (await _compute_daily_signals(intent.symbol)).get("pct_move_today")
            if move is not None and move <= -abs(_cfg.EARNINGS_BREAK_DROP_PCT):
                await _flag_pmcc_close(
                    intent=intent,
                    reason=(f"earnings-miss crash: {intent.symbol} {move*100:.0f}% "
                            f"{dse} session(s) after earnings — fundamental break, "
                            f"not a normal drawdown"),
                    kind="earnings_break",
                )
                return
    except Exception as e:
        logger.debug("earnings-break check failed for %s: %s", intent.symbol, e)

    # Earnings hedge check — only meaningful when there's a SHORT call to buy
    # back. A bare long LEAP (LEAPS-only) has no short leg to hedge, so skip.
    days_to_e = await days_to_earnings(intent.symbol)
    if intent.short_call_strike and days_to_e is not None and 0 <= days_to_e <= 2:
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

    # ---- Graded exit-pressure score (incl. the quant overlay) -------
    # Runs only when no hard exit trigger above fired. Gives the LEAP the
    # unified Exit Pressure Score + per-tick observability, and folds in the
    # quant researcher's edge. Flags (never auto-dumps) on the top band.
    try:
        await _record_leap_exit_pressure(intent, ib)
    except Exception as e:
        logger.debug("leap exit-pressure eval failed for %s: %s", intent.symbol, e)

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


async def _record_leap_exit_pressure(intent: TradeIntent, ib: Any) -> None:
    """Graded Exit Pressure Score for a LEAP position — incl. the quant overlay.

    Restores the unified Exit Pressure Score + per-tick ``position_pressure``
    observability that only the stock path had (so the LEAPS-only book has had
    no graded exit scoring since 2026-05-12), and is what puts the quant
    researcher into the EXIT decision. Sub-signals: theme deterioration +
    technical exhaustion (on the UNDERLYING) + rotation + quant edge, scored
    with LEAPS_WEIGHTS (profit_preservation left neutral here — a LEAP-premium
    trim ladder is the documented next step).

    Records the row every tick. On the top ``aggressive`` band (strong
    multi-signal agreement) it FLAGS the LEAP for close — alert + operator
    confirm, NEVER an auto-dump: ``kind='exit_pressure'`` is deliberately not in
    ``_AUTO_CLOSE_KINDS``, so a normal high-beta down day can't trigger a sale.
    """
    if not get_settings().LEAP_GRADED_EXIT_ENABLED:
        return
    from tradingagents.strategies.maintenance.theme_health import is_theme_hot_for_symbol
    from tradingagents.strategies.maintenance.momentum_exhaustion import (
        evaluate_momentum_exhaustion,
    )
    from tradingagents.strategies.maintenance.exit_pressure import (
        LEAPS_WEIGHTS, compute_exit_pressure,
    )
    sym = (intent.symbol or "").upper()

    # Theme deterioration inputs.
    async with db_session() as s:
        _hot, best_theme = await is_theme_hot_for_symbol(s, sym)

    # Technical exhaustion on the UNDERLYING (not the option premium).
    exhaustion_score: Optional[float] = None
    try:
        signals = await _compute_daily_signals(sym)
        spot = await _get_spot(ib, sym)
        if spot is None and signals.get("prior_close") and signals.get("pct_move_today") is not None:
            spot = signals["prior_close"] * (1 + signals["pct_move_today"])
        if spot:
            exh = evaluate_momentum_exhaustion(
                symbol=sym, current_price=spot,
                ma_20d=signals.get("ma_20d"), rsi_14=signals.get("rsi_14"),
                volume_ratio=signals.get("volume_ratio"),
                open_today=signals.get("open_today"),
                prior_close=signals.get("prior_close"),
                atr_30d=await _compute_atr_30d(sym),
            )
            exhaustion_score = exh.score
    except Exception as e:
        logger.debug("leap exit-pressure: exhaustion calc failed for %s: %s", sym, e)

    # Rotation: tighten if any theme this name lives in is rotating out.
    rotation_delta: Optional[float] = None
    try:
        from .rotation_detector import any_theme_rotating
        from api.app.db import ThemeSymbol as _TS
        async with db_session() as s:
            theme_ids = [r[0] for r in (await s.execute(
                select(_TS.theme_id).where(_TS.symbol == sym))).all() if r[0]]
        rot_active, _themes = await any_theme_rotating(theme_ids)
        if rot_active:
            rotation_delta = get_settings().ROTATION_EXIT_PRESSURE_DELTA
    except Exception as e:
        logger.debug("leap exit-pressure: rotation check failed for %s: %s", sym, e)

    # Quant overlay — the researcher's edge in the exit decision.
    quant_delta: Optional[float] = None
    if get_settings().QUANT_OVERLAY_ENABLED:
        try:
            from api.app.research.overlay import symbol_exit_delta
            quant_delta = await symbol_exit_delta(sym) or None
        except Exception as e:
            logger.debug("leap exit-pressure: quant delta failed for %s: %s", sym, e)

    # Bearish overlay — CONFIRMATION ONLY. A single short-seller's position is
    # opinion, not a signal, so the bump applies ONLY when the name already
    # shows OTHER bearish pressure (theme deterioration, technical exhaustion, or
    # rotation). The short then AMPLIFIES a real signal; on an otherwise-healthy
    # name it does nothing (and never counts as an independent guardrail pillar).
    short_delta = 0.0
    try:
        from api.app.hedge_funds.bearish import notable_short_pressure, notable_short_exit_delta
        from api.app.db import ThemeSymbol as _TSb
        async with db_session() as _s2:
            _tids = [r[0] for r in (await _s2.execute(
                select(_TSb.theme_id).where(_TSb.symbol == sym))).all() if r[0]]
        _is_short, _sm = await notable_short_pressure(sym, _tids)
        if _is_short:
            _corroborated = (
                (best_theme is not None and (best_theme.streak_days_below_floor > 0
                 or (best_theme.composite is not None and best_theme.composite < 40)))
                or (exhaustion_score is not None and exhaustion_score > 0.3)
                or (rotation_delta is not None and rotation_delta > 0)
            )
            if _corroborated:
                short_delta = notable_short_exit_delta(True)
                logger.info("exit-pressure: %s notable-bear short CONFIRMED by other weakness "
                            "(+%.0f) — %s", sym, short_delta,
                            "; ".join(str(h.get("manager", "")) for h in _sm.get("hits", [])[:2]))
            else:
                logger.debug("exit-pressure: %s has a notable short but NO corroborating "
                             "weakness — ignoring (a short alone is not a signal)", sym)
    except Exception as e:
        logger.debug("leap exit-pressure: notable-short check failed for %s: %s", sym, e)

    # Fresh institutional selling — CONFIRMATION ONLY, same discipline as the
    # notable-short overlay. A tracked fund's recent 13F trim/exit or a 13D/G
    # holder reduction on this name AMPLIFIES pressure only when the name
    # already shows other weakness; on a healthy name it does nothing, and it
    # never counts as an independent guardrail pillar. This is the wire the
    # stake watch + 13F delta engine were built for.
    inst_sell_delta = 0.0
    try:
        from api.app.hedge_funds.conviction import recent_manager_selling
        _selling, _sell_meta = await recent_manager_selling(sym)
        if _selling:
            _corroborated_inst = (
                (best_theme is not None and (best_theme.streak_days_below_floor > 0
                 or (best_theme.composite is not None and best_theme.composite < 40)))
                or (exhaustion_score is not None and exhaustion_score > 0.3)
                or (rotation_delta is not None and rotation_delta > 0)
            )
            if _corroborated_inst:
                inst_sell_delta = float(get_settings().INSTITUTIONAL_SELL_EXIT_DELTA)
                logger.info(
                    "exit-pressure: %s fresh institutional selling CONFIRMED by other "
                    "weakness (+%.0f) — %d fund cuts, %d stake reductions", sym,
                    inst_sell_delta, len(_sell_meta.get("fund_cuts", [])),
                    len(_sell_meta.get("stake_reductions", [])),
                )
            else:
                logger.debug("exit-pressure: %s has fresh institutional selling but NO "
                             "corroborating weakness — ignoring (confirmation-only)", sym)
    except Exception as e:
        logger.debug("leap exit-pressure: institutional-sell check failed for %s: %s", sym, e)

    pressure = compute_exit_pressure(
        theme_composite=(best_theme.composite if best_theme else None),
        theme_streak_days=(best_theme.streak_days_below_floor if best_theme else 0),
        trim_band="none",
        exhaustion_score=exhaustion_score,
        rotation_score_delta=rotation_delta,
        quant_edge_delta=((quant_delta or 0.0) + short_delta + inst_sell_delta) or None,
        weights=LEAPS_WEIGHTS,
    )

    async with db_session() as s:
        await record_auto_action(
            s, loop="maintenance", action_type=f"position_pressure_{pressure.band}",
            gate_result=_synthetic_passed_gate(), symbol=sym, intent_id=intent.id,
            payload={
                "score": pressure.score, "band": pressure.band,
                "rationale": pressure.rationale, "sub_scores": pressure.sub_scores,
                "quant_exit_delta": quant_delta, "rotation_delta": rotation_delta,
                "institutional_sell_delta": inst_sell_delta or None,
                "exhaustion_score": exhaustion_score,
                "theme": ({"composite": best_theme.composite,
                           "streak": best_theme.streak_days_below_floor}
                          if best_theme else None),
                "structure": "leap_only",
            },
            outcome=pressure.band,
        )

    # Autonomous execution of the graded score (operator policy 2026-06-30):
    # `aggressive` (>75) -> full close; `trim_heavy` (60-75) -> partial de-risk
    # trim. GUARDRAIL: require >=2 contributing pillars (theme deterioration /
    # technical exhaustion incl. RSI / rotation / quant edge) so a lone noisy
    # signal can't auto-act — and raw price drawdown, which is not a pillar in
    # this score, can never trigger a sale. Falls back to flag-and-confirm when
    # autonomy is disabled or the guardrail isn't met.
    if pressure.band in ("aggressive", "trim_heavy"):
        contributing = sum(
            1 for k in ("theme_deterioration", "tech_exhaustion", "rotation_pressure")
            if (pressure.sub_scores.get(k) or 0) > 0
        ) + (1 if (quant_delta or 0) > 0 else 0)
        # (notable-short is confirmation-only — it amplifies the score but never
        # counts as an independent pillar, so a lone short can't trigger an exit.)
        auto_ok = get_settings().LEAP_AUTO_EXIT_ENABLED and contributing >= 2
        reason = (f"exit pressure {pressure.score:.0f}/100 (graded, {contributing} "
                  f"signal(s)): {pressure.rationale}")
        if pressure.band == "aggressive":
            # auto_ok -> auto full close; else -> flag for operator confirm.
            await _flag_pmcc_close(intent=intent, reason=reason,
                                   kind="exit_pressure", auto_execute=auto_ok)
        elif auto_ok:
            # trim_heavy de-risk: sell a slice, once per name per day.
            full_qty = int(intent.qty or 0)
            trim_qty = max(1, round(full_qty * get_settings().LEAP_TRIM_HEAVY_PCT))
            if full_qty > 1 and trim_qty < full_qty and not await _leap_trimmed_today(intent.id):
                await _flag_pmcc_close(intent=intent, reason=reason,
                                       kind="exit_pressure_trim", auto_execute=True,
                                       close_qty=trim_qty)


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
    if not await _autotrade_active():
        logger.info("maint: kill switch active — skipping stock exit for %s", intent.symbol)
        return
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
        order_type="LMT", limit_px=limit, tif="DAY", account_mode=get_settings().IBKR_MODE,
    )
    try:
        result = await ib.submit_trade(intent_dc)
    except Exception as e:
        logger.exception("maint exit submit failed for %s: %s", intent.symbol, e)
        async with db_session() as s:
            s.add(TradeAuditLog(
                intent_id=intent.id, action="maint_exit_outcome",
                outcome="error", payload={"error": str(e)},
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


async def _flag_pmcc_close(*, intent: TradeIntent, reason: str, kind: str,
                           auto_execute: Optional[bool] = None,
                           close_qty: Optional[int] = None,
                           operator: bool = False) -> None:
    """Close a LEAP: SELL-TO-CLOSE the long call outright, walked toward the bid
    through the single-leg executor.

    ``close_qty`` controls full-close vs partial-trim: None (default) sells the
    whole position and marks the intent closed; a value < the held quantity is a
    PARTIAL de-risk trim — only that many contracts are sold, the intent stays
    ``leap_open`` with its qty reduced, and the action is recorded as a trim.

    LEAPS-only book — there is no short leg to buy back, so this is a single
    SELL on the LEAP conid (the legacy two-leg combo close was removed when we
    dropped diagonals). The LEAP conid is resolved from ``walking_config`` and,
    as a fallback, from the live IBKR option position for this symbol.

    Whether this AUTO-EXECUTES vs FLAGS-for-operator is driven by ``kind``:
    genuine fundamental/structural breaks (earnings crash, filing thesis-break,
    DTE cliff — see ``_AUTO_CLOSE_KINDS``) auto-execute; price-drawdown proxies
    (delta decay, scorecard Avoid) flag only, so a normal high-beta down day is
    never auto-dumped. Callers may override via ``auto_execute``.

    The auto-execute path also falls back to flag+alert if pre-conditions fail
    (no conid resolvable, daily cap hit, IBKR unreachable).
    """
    if auto_execute is None:
        auto_execute = kind in _AUTO_CLOSE_KINDS

    # Re-check the kill switch immediately before an AUTO close/trim so a
    # mid-tick disable halts within one order. Flag-only still records/alerts.
    #
    # `operator=True` bypasses this: the kill switch exists to stop AUTONOMOUS
    # trading, and an authenticated operator pressing "close this position" is
    # the opposite of autonomous. Downgrading it to a flag would mean the manual
    # exit silently does nothing precisely when the switch is off — i.e. during
    # the incident you disarmed for. Same reasoning for the daily close cap below.
    if operator:
        auto_execute = True
    elif auto_execute and not await _autotrade_active():
        logger.info("maint: kill switch active — flagging instead of auto-closing %s", intent.symbol)
        auto_execute = False

    # FLAG-ONLY path: record + alert once per (intent, kind), then leave the
    # position open for the operator to close. Deduped so a signal that stays
    # true every tick doesn't spam (the AVGO 46x pattern).
    if not auto_execute:
        dedup_key = f"{intent.id}:{kind}"
        if dedup_key in _CLOSE_FLAG_ALERTED:
            return
        _CLOSE_FLAG_ALERTED.add(dedup_key)
        async with db_session() as s:
            await record_auto_action(
                s, loop="maintenance", action_type="pmcc_close_flagged",
                gate_result=_synthetic_passed_gate(),
                symbol=intent.symbol, intent_id=intent.id,
                payload={"reason": reason, "kind": kind,
                         "disposition": "flag_only_operator_confirms"},
                outcome="flagged",
            )
        await alert(
            level="warning",
            title=f"LEAP close suggested — confirm: {intent.symbol}",
            body=(f"{reason}. Price-driven (not auto-dumped on this high-beta book); "
                  f"close via /api/admin/positions/exit/{intent.id} if you agree."),
        )
        return

    cfg = dict((intent.walking_config or {}))
    leap_conid = int(cfg.get("leap_conid", 0))

    from api.app.positions import _ibkr
    from tradingagents.strategies.execution import (
        ExecutionConfig, submit_single_leg_option,
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

    # Resolve the LEAP conid from the live long-call position if walking_config
    # didn't carry it (e.g. adopted orphan). Pick the long call (qty > 0) for
    # this underlying — the LEAPS-only book holds exactly one per name.
    if not leap_conid:
        try:
            positions = await ib.get_positions()
            for p in positions:
                if (str(p.get("symbol", "")).upper() == intent.symbol.upper()
                        and str(p.get("sec_type", p.get("secType", ""))).upper() in ("OPT", "FOP")
                        and (p.get("right") in (None, "C", "CALL"))
                        and float(p.get("qty") or 0) > 0):
                    leap_conid = int(p.get("conid") or 0)
                    if leap_conid:
                        break
        except Exception as e:
            logger.debug("leap conid resolve via positions failed for %s: %s", intent.symbol, e)

    if not leap_conid:
        dedup_key = f"{intent.id}:noconid"
        if dedup_key in _CLOSE_FLAG_ALERTED:
            return
        _CLOSE_FLAG_ALERTED.add(dedup_key)
        async with db_session() as s:
            await record_auto_action(
                s, loop="maintenance", action_type="pmcc_close_flagged",
                gate_result=_synthetic_passed_gate(),
                symbol=intent.symbol, intent_id=intent.id,
                payload={"reason": reason, "kind": kind, "manual_required": "no LEAP conid"},
                outcome="flagged",
            )
        await alert(
            level="warning",
            title=f"LEAP close — manual: {intent.symbol}",
            body=f"{reason}. No LEAP conid resolvable; close via /api/admin/positions/exit/{intent.id}.",
        )
        return

    if not operator and await _maintenance_cap_hit("close"):
        await alert(level="warning",
                    title=f"LEAP close skipped (daily cap): {intent.symbol}",
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

    full_qty = int(intent.qty or 1)
    qty = int(close_qty) if close_qty else full_qty
    is_partial = close_qty is not None and 0 < qty < full_qty
    if qty <= 0:
        return

    async with db_session() as s:
        s.add(TradeAuditLog(
            intent_id=intent.id,
            action="auto_leap_trim_attempt" if is_partial else "auto_pmcc_close_attempt",
            payload={"symbol": intent.symbol, "leap_conid": leap_conid, "qty": qty,
                     "full_qty": full_qty, "partial": is_partial,
                     "reason": reason, "kind": kind},
        ))

    result = await submit_single_leg_option(
        ibkr=ib, conid=leap_conid, contracts=qty,
        action="SELL",   # sell-to-close the long LEAP
        config=ExecutionConfig(
            initial_offset_cents=1, walk_increment_cents=1,
            walk_interval_sec=20, max_offset_pct_of_spread=0.50,
            timeout_sec=180,
        ),
        # Full closes (earnings/thesis break) exit decisively; a partial
        # de-risk trim walks the limit instead of paying up across the spread.
        adaptive_priority="Normal" if is_partial else "Urgent",
    )

    async with db_session() as s:
        i = await s.get(TradeIntent, intent.id)
        if i:
            if result.status == "filled":
                if is_partial:
                    # Partial de-risk: reduce the held quantity, keep it open.
                    i.qty = max(0.0, float(i.qty or 0) - qty)
                    if i.leap_qty is not None:
                        i.leap_qty = max(0, int(i.leap_qty) - qty)
                    if i.qty <= 0:   # defensive — shouldn't happen given is_partial
                        i.status = "closed"
                        i.position_state = "closed"
                else:
                    i.status = "closed"
                    i.position_state = "closed"
            elif result.status not in ("abandoned", "rejected_pretrade"):
                i.status = "error"
        # action_type drives the daily-cap counter + trim dedup, so keep
        # 'leap_trim_*' and 'pmcc_close_*' distinct.
        prefix = "leap_trim" if is_partial else "pmcc_close"
        s.add(TradeAuditLog(
            intent_id=intent.id,
            action="auto_leap_trim_outcome" if is_partial else "auto_pmcc_close_outcome",
            outcome=result.status,
            # TradeAuditLog has no `error` column — fold it into payload
            # (result.to_dict() already carries it, but be explicit).
            payload={**result.to_dict(), "error": result.error, "qty": qty,
                     "partial": is_partial},
        ))
        await record_auto_action(
            s, loop="maintenance", action_type=f"{prefix}_{result.status}",
            gate_result=_synthetic_passed_gate(),
            symbol=intent.symbol, intent_id=intent.id,
            payload={"reason": reason, "kind": kind, "qty": qty,
                     "partial": is_partial, "execution": result.to_dict()},
            outcome=result.status,
        )

    if result.status == "filled":
        if is_partial:
            await alert(level="warning", title=f"LEAP TRIMMED: {intent.symbol}",
                        body=(f"Sold {qty} of {full_qty} contracts @ ${result.fill_price:.2f} "
                              f"(de-risk). Reason: {reason}"))
        else:
            await alert(level="warning", title=f"LEAP CLOSED: {intent.symbol}",
                        body=f"@ ${result.fill_price:.2f}. Reason: {reason}")
    elif result.status == "abandoned":
        verb = "trim" if is_partial else "close"
        await alert(level="warning", title=f"LEAP {verb} abandoned: {intent.symbol}",
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
      last_close         LATEST bar's close (today's live/partial bar when the
                         feed carries it) — the correct "where is price NOW"
                         input. prior_close is one session stale by definition;
                         comparing it against an MA that includes today's bar
                         biased the rotation breadth bearish on strong up days
                         (2026-07-09: 14/17 themes false-flagged during a rally).
    """
    out: dict[str, Optional[float]] = {
        "pct_move_today": None, "volume_ratio": None, "rsi_14": None,
        "ma_20d": None, "open_today": None, "prior_close": None,
        "last_close": None,
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

        if closes:
            out["last_close"] = closes[-1]
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


# Filings we've already alerted on, keyed by SEC link. find_new_8k_events can
# re-return the same filing every tick; without this we'd re-alert + re-audit
# the same 8-K ~once per tick (~156/day) and spam Slack. Resets on restart
# (so a filing re-alerts at most once per process), which is fine.
_ALERTED_FILING_LINKS: set[str] = set()

# Exit kinds that represent a genuine fundamental or structural break and may
# AUTO-EXECUTE the close. Everything else (delta decay, scorecard "Avoid") is a
# price-drawdown proxy and stays FLAG-ONLY so a normal high-beta down day never
# auto-dumps a position (operator policy on this book).
_AUTO_CLOSE_KINDS = {"earnings_break", "thesis_break", "dte_cliff", "catastrophic_stop"}


async def _underlying_in_downtrend(symbol: str, ib: Any) -> bool:
    """True if spot is below its 200-day MA — a structural downtrend that
    confirms a large LEAP loss is a broken thesis, not a transient beta drop.
    Fails to False (do NOT auto-close) when history is unavailable, so a data
    gap can never trigger a catastrophic sale."""
    try:
        from datetime import date, timedelta
        from tradingagents.dataflows.fallback import get_stock_data_with_fallback
        df = await get_stock_data_with_fallback(
            symbol, date.today() - timedelta(days=340), date.today(), ibkr_provider=ib)
        if df is None or len(df) < 150 or "Close" not in df.columns:
            return False
        closes = df["Close"].astype(float).tolist()
        ma200 = sum(closes[-200:]) / min(200, len(closes))
        return closes[-1] < ma200
    except Exception:
        return False

# Intents we've already flagged-for-manual-close, keyed by f"{intent_id}:{kind}".
# A flag-only close re-evaluates true every tick; without this it would re-alert
# + re-audit ~once per tick (the AVGO 46x pattern). Resets on restart.
_CLOSE_FLAG_ALERTED: set[str] = set()


async def _watch_8k_filings(pos_by_symbol: dict[str, dict]) -> dict[str, str]:
    """Sweep recent 8-K filings, write alerts, return per-symbol
    thesis-break strings for the high-severity ones.

    Returns dict mapping symbol -> reason string when a guidance- or
    after-hours-classified 8-K hit; the maint-loop's _evaluate_stock
    folds this into its existing thesis-break check.
    """
    from tradingagents.strategies.maintenance.sec_filings_watch import (
        FilingEvent, find_new_8k_events, find_new_edgar_breaks,
        thesis_break_signal_from_filings,
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

    # EDGAR-direct, CONTENT-gated sweep for HELD names (8-K + 6-K/20-F). This is
    # the authoritative, low-latency path — the FMP feed above lagged a real WDC
    # 8-K by ~25h, and its timing-only severity false-positived a benign convert
    # exchange. Only these EDGAR events (body keyword-scored) may drive an
    # auto-close; the FMP feed is alert/research-only from here on.
    try:
        from tradingagents.dataflows.providers.registry import get_provider
        edgar = get_provider("edgar")
        edgar_events = await find_new_edgar_breaks(
            edgar_provider=edgar, held_symbols=held,
        )
        events = (events or []) + edgar_events
    except Exception as e:
        logger.debug("filings watcher: EDGAR held-name sweep skipped: %s", e)

    if not events:
        return {}

    # Audit + alert per event — but only ONCE per filing (dedup by SEC link),
    # so the same 8-K doesn't re-alert/re-audit every tick. The thesis-break
    # computation below still runs over ALL events (it's a live exit signal,
    # not an alert), so deduping the alert never suppresses an exit.
    breaks_by_sym: dict[str, str] = {}
    for ev in events:
        ev_link = ev.final_link or ev.link or f"{ev.symbol}:{ev.accepted_date or ev.filing_date}"
        if ev_link in _ALERTED_FILING_LINKS:
            continue  # already flagged this filing — skip audit + alert
        _ALERTED_FILING_LINKS.add(ev_link)

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
            title=f"{ev.form_type} {ev.severity}: {ev.symbol}"
                  + (" (held)" if ev.symbol in held else " (theme universe)"),
            body=f"{ev.rationale} · {ev.final_link or ev.link or ''}",
        )

    # Auto-close thesis-breaks fire ONLY from EDGAR body-CONTENT-confirmed events
    # on held names (severity "guidance" = keyword severity >= break threshold).
    # The FMP universe feed is alert/research-only: its severity is a content-
    # blind TIMING heuristic (an after-hours 8-K), which false-positived a benign
    # WDC convertible-notes exchange. We never auto-close on timing alone — a real
    # break must be confirmed by reading the filing body.
    for ev in events:
        if (ev.symbol in held
                and ev.detail.get("source") == "edgar"
                and ev.severity == "guidance"):
            breaks_by_sym[ev.symbol] = (
                breaks_by_sym.get(ev.symbol, "")
                + ("; " if ev.symbol in breaks_by_sym else "")
                + f"{ev.form_type} content break: {ev.rationale}"
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
        FmpProvider, _is_insider_sale, _is_insider_buy,
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

    # Filter to theme-universe symbols + officer/director/10%-owner. We now
    # surface BOTH directions: sells (defensive / distribution warning) and
    # opportunistic BUYS (the bullish accumulation signal). Each row is tagged
    # 'sale' or 'buy'; awards/exercises pass neither filter.
    matches: list[tuple[str, dict[str, Any]]] = []   # (direction, row)
    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        if sym not in universe:
            continue
        owner = (r.get("typeOfOwner") or "").lower()
        if not any(k in owner for k in ("officer", "director", "10")):
            continue
        if _is_insider_sale(r):
            matches.append(("sale", r))
        elif _is_insider_buy(r):
            matches.append(("buy", r))
    if not matches:
        return 0

    # Dedup against existing insider_alert / insider_buy_alert rows from the
    # last 7 days, keyed on (symbol, filing_date, direction).
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    seven_days_ago = today - timedelta(days=7)
    async with db_session() as s:
        prior = (
            await s.execute(
                select(AutoAction.symbol, AutoAction.action_type, AutoAction.payload)
                .where(AutoAction.action_type.in_(["insider_alert", "insider_buy_alert"]))
                .where(AutoAction.timestamp >= seven_days_ago)
            )
        ).all()
    seen_keys: set[tuple[str, str, str]] = set()
    for sym, atype, payload in prior:
        if isinstance(payload, dict):
            direction = "buy" if atype == "insider_buy_alert" else "sale"
            seen_keys.add(
                ((sym or "").upper(), str(payload.get("filing_date") or ""), direction)
            )

    new_count = 0
    for direction, r in matches:
        sym = str(r.get("symbol") or "").upper()
        filing_date = str(r.get("filingDate") or r.get("transactionDate") or "")
        if (sym, filing_date, direction) in seen_keys:
            continue
        owner = r.get("typeOfOwner") or ""
        reporter = r.get("reportingName") or "?"
        try:
            qty = float(r.get("securitiesTransacted") or 0)
            price = float(r.get("price") or 0)
            usd = qty * price
        except (TypeError, ValueError):
            usd = 0.0

        is_buy = direction == "buy"
        action_type = "insider_buy_alert" if is_buy else "insider_alert"
        async with db_session() as s:
            await record_auto_action(
                s, loop="maintenance", action_type=action_type,
                gate_result=_synthetic_passed_gate(),
                symbol=sym,
                payload={
                    "direction": direction,
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
        if is_buy:
            # Confirm whether this is a CLUSTER (>=2 opportunistic buyers /30d)
            # — the high-conviction shape worth a louder alert.
            try:
                bp = await fmp.get_insider_buy_pressure(sym)
            except Exception:
                bp = {}
            clustered = bool(bp.get("clustered"))
            await alert(
                level="warning" if clustered else "info",
                title=(f"INSIDER BUY CLUSTER: {sym}" if clustered
                       else f"Insider buy: {sym} ({owner})"),
                body=(f"{reporter} bought {r.get('securitiesTransacted')} sh @ "
                      f"${r.get('price')} ({filing_date})."
                      + (f" Cluster: {bp.get('n_buyers_30d')} buyers / 30d, "
                         f"{bp.get('detail')}. Accumulation candidate." if clustered else "")),
            )
        else:
            await alert(
                level="info",
                title=f"Insider sale: {sym} ({owner})",
                body=f"{reporter} sold {r.get('securitiesTransacted')} sh @ ${r.get('price')} ({filing_date})",
            )
        new_count += 1

    if new_count:
        logger.info("maint loop: %d new insider alerts (theme-universe, buy+sell)", new_count)
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
    if not await _autotrade_active():
        logger.info("maint: kill switch active — skipping stock trim for %s", intent.symbol)
        return
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
        order_type="LMT", limit_px=limit, tif="DAY", account_mode=get_settings().IBKR_MODE,
    )
    try:
        result = await ib.submit_trade(intent_dc)
    except Exception as e:
        logger.exception("trim submit failed for %s: %s", sym, e)
        async with db_session() as s:
            s.add(TradeAuditLog(
                intent_id=intent.id, action="stock_trim_outcome",
                outcome="error", payload={"error": str(e)},
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
    """True if today's count of `kind` actions has hit the daily cap.

    A ``None`` cap means NO LIMIT and returns False immediately, skipping the
    COUNT query. Both maintenance caps are uncapped as of 2026-08-17: they
    throttled risk REDUCTION, so with entries unlimited an 8-close ceiling
    would strand positions the exit logic had already decided to close.
    """
    from api.app.autotrade.auto_gate import DEFAULT_CAPS, _capped
    from sqlalchemy import func, or_
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "close":
        # Trims and full closes both reduce exposure — count them together so a
        # busy de-risking day can't blow past the daily exit cap.
        action_filter = or_(
            AutoAction.action_type == "pmcc_close_filled",
            AutoAction.action_type == "leap_trim_filled",
        )
        cap = _capped(DEFAULT_CAPS, "AUTO_MAX_CLOSES_PER_DAY")
    elif kind == "roll":
        action_filter = AutoAction.action_type.like("pmcc_roll_filled")
        cap = _capped(DEFAULT_CAPS, "AUTO_MAX_ROLLS_PER_DAY")
    else:
        return False
    if cap is None:
        return False
    async with db_session() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(AutoAction)
                .where(AutoAction.timestamp >= today)
                .where(AutoAction.loop == "maintenance")
                .where(action_filter)
            )
        ).scalar_one()
    return n >= cap


async def _leap_trimmed_today(intent_id: str) -> bool:
    """True if this intent already had a de-risk trim ATTEMPT today (filled OR
    abandoned), so the trim_heavy band shaves a position at most once/day.

    Counting abandoned attempts too (not just fills) is deliberate: with the
    walker's cancel now real but still fallibly-confirmable, an abandoned trim's
    resting order could fill late — re-firing the trim on the next tick would
    double-trim. Waiting until the next day is the safe default for a non-urgent
    de-risk. (Genuine full closes go through the aggressive-band path, which
    removes the position, so this only gates the partial-trim band.)"""
    from sqlalchemy import func
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with db_session() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(AutoAction)
                .where(AutoAction.timestamp >= today)
                .where(AutoAction.intent_id == intent_id)
                .where(AutoAction.action_type.like("leap_trim_%"))
            )
        ).scalar_one()
    return n > 0


# ---------------------------------------------------------------------------
# Auto-fire executors — short-call close, short-call roll, LEAP forward roll
# ---------------------------------------------------------------------------


async def _execute_auto_short_call_close(intent: TradeIntent, ib: Any, reason: str) -> None:
    """Buy back the existing short call WITHOUT opening a new one.
    (Kill-switch re-checked inside via the shared guard.)

    Earnings-hedge use case: closes the short leg 2 sessions before the
    print, leaves the LEAP uncapped through the move. Next maintenance
    tick after earnings will see the short side empty and (when
    sequenced-relisting is wired) re-establish.
    """
    if not await _autotrade_active():
        logger.info("maint: kill switch active — skipping short-call close for %s", intent.symbol)
        return
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
            outcome=result.status, payload={**result.to_dict(), "error": result.error},
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
    if not await _autotrade_active():
        logger.info("maint: kill switch active — skipping short-call roll for %s", intent.symbol)
        return
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
            outcome=result.status, payload={**result.to_dict(), "error": result.error},
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
    if not await _autotrade_active():
        logger.info("maint: kill switch active — skipping LEAP roll for %s", intent.symbol)
        return
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
            outcome=result.status, payload={**result.to_dict(), "error": result.error},
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
