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
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.config import get_settings
from api.app.db import (
    AutoAction,
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
# Hard ceiling on a single tick. A legitimate tick — even when slowed to a
# few minutes by provider rate-limiting — never approaches this; it exists
# solely so a stalled IBKR await (e.g. a market-data request frozen by
# "competing live session") can't wedge the loop forever the way it did on
# 2026-06-29 (17h silent outage). On timeout the tick is cancelled and the
# loop continues at the next poll.
_TICK_TIMEOUT_SEC = 300
# The pullback scan does per-symbol daily-bar fetches across the theme universe,
# so throttle it to every ~5 min rather than every 60s entry tick.
_LAST_PULLBACK_SCAN: float = 0.0
_PULLBACK_SCAN_INTERVAL_SEC = 300


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def start_entry_loop() -> None:
    """Spawn the background task. Idempotent."""
    global _TASK
    if _TASK and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop_forever(), name="entry_loop")
    from .supervisor import supervise
    supervise(_TASK, start_entry_loop, "entry_loop")
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
            await asyncio.wait_for(_tick(), timeout=_TICK_TIMEOUT_SEC)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.error(
                "entry_loop tick exceeded %ds and was cancelled — likely a "
                "stalled IBKR market-data call. Loop continues at next poll.",
                _TICK_TIMEOUT_SEC,
            )
            try:
                await alert(
                    level="warning",
                    title="Entry loop tick timed out",
                    body=(f"Tick exceeded {_TICK_TIMEOUT_SEC}s and was cancelled "
                          "to prevent a permanent hang. Loop continues — check for "
                          "a competing IBKR market-data session."),
                )
            except Exception:  # noqa: BLE001 — never let alerting wedge the loop
                pass
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

    # Block-on-mismatch: don't add NEW risk while the book is in an inconsistent
    # state (a live position with no managing intent). The maint loop auto-adopts
    # such orphans within a tick, so this is a short fail-safe pause, not a stall.
    try:
        from api.app.positions import _ibkr as _ibkr2
        if await _has_unmanaged_orphan(await _ibkr2()):
            logger.warning("auto-entry: unmanaged position(s) present — pausing new entries "
                           "until reconciled (maint loop adopts orphans each tick)")
            return
    except Exception as e:
        logger.debug("auto-entry: orphan-parity check failed (%s) — continuing", e)

    # Pull today's completed runs and queue Buy decisions we haven't acted on.
    candidates = await _find_unprocessed_buys()
    if not candidates and not get_settings().PULLBACK_ADD_ENABLED:
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

    # ---- Pullback adds ("buy support") --------------------------------------
    # Average into still-favored theme names that have dipped to SMA20/SMA50 on
    # orderly volume with RSI holding — gated on theme in-contact + no hedge-fund
    # rotation-out, and routed through the SAME capped entry path (per-name /
    # aggregate / margin / daily caps all still apply). One add/name/day.
    global _LAST_PULLBACK_SCAN
    import time as _time
    if (get_settings().PULLBACK_ADD_ENABLED and sizing_factor > 0
            and _time.monotonic() - _LAST_PULLBACK_SCAN >= _PULLBACK_SCAN_INTERVAL_SEC):
        _LAST_PULLBACK_SCAN = _time.monotonic()
        try:
            from api.app.positions import _ibkr as _ibkr_pb
            ib_pb = await _ibkr_pb()
            already = {c[2].upper() for c in candidates}
            for run_id, theme_id, symbol, composite in await _find_pullback_adds(ib_pb, already):
                if await _daily_cap_exhausted():
                    break
                async with db_session() as s:
                    await record_auto_action(
                        s, loop="entry", action_type="open_leap_pullback", gate_result=None,
                        symbol=symbol, payload={"run_id": run_id, "note": "pullback add at MA support"},
                        outcome="attempt")
                await _process_one(run_id, theme_id, symbol, composite,
                                   sizing_factor=sizing_factor, macro_regime=macro_regime,
                                   pullback_add=True)
        except Exception as e:
            logger.debug("auto-entry: pullback-add pass failed: %s", e)


def _latest_run_per_theme(
    runs: list[tuple[str, str]],
) -> dict[str, str]:
    """{theme_id: run_id} keeping only the FIRST run seen per theme.

    Pure + testable. ``runs`` must be pre-sorted newest-first (finished_at
    desc), so the first occurrence of each theme is its latest completed run.
    Ensures we score off ONE (the freshest) run per theme, never a stale one
    or duplicate candidates across two runs of the same theme.
    """
    latest: dict[str, str] = {}
    for run_id, theme_id in runs:
        latest.setdefault(theme_id, run_id)
    return latest


async def _find_unprocessed_buys() -> list[tuple[str, str, str, float]]:
    """Return (run_id, theme_id, symbol, composite) for un-traded Buy decisions
    on the LATEST completed run per theme within ENTRY_RUN_LOOKBACK_HOURS.
    Sorted by composite desc.

    Using a lookback window (not strictly "today") means a late or recovered
    daily run still yields candidates instead of a zero-entry day — the gap that
    left 2026-06-29 with no entries. De-dup still prevents re-entry churn:
      * skip (run_id, symbol) that already has a TradeIntent — already routed
      * skip any symbol attempted (open_*) anywhere in the window

    Does NOT skip names already held in the IBKR account: the operator
    explicitly wants the system to *stack* into a working thesis.
    """
    lookback_h = get_settings().ENTRY_RUN_LOOKBACK_HOURS
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_h)

    async with db_session() as s:
        # Latest completed run per theme within the window.
        run_rows = (
            await s.execute(
                select(Run.id, Run.theme_id)
                .where(Run.status == "done")
                .where(Run.finished_at >= cutoff)
                .order_by(Run.finished_at.desc())
            )
        ).all()
        latest_by_theme = _latest_run_per_theme([(r[0], r[1]) for r in run_rows])
        run_to_theme = {rid: tid for tid, rid in latest_by_theme.items()}
        run_ids = set(run_to_theme.keys())
        if not run_ids:
            return []

        # Minimum-conviction floor: a weak "Buy" (degraded/noisy run) must not
        # auto-trade real money identically to a strong one. 0 disables.
        _min_comp = float(get_settings().ENTRY_MIN_COMPOSITE)
        _q = (
            select(TickerScore.run_id, TickerScore.symbol, TickerScore.composite)
            .where(TickerScore.run_id.in_(run_ids))
            .where(TickerScore.decision == "Buy")
        )
        if _min_comp > 0:
            _q = _q.where(TickerScore.composite >= _min_comp)
        rows = (await s.execute(_q.order_by(TickerScore.composite.desc()))).all()

        # Already submitted (any TradeIntent for one of these runs + symbol).
        existing = {
            (i.run_id, i.symbol)
            for i in (
                await s.execute(
                    select(TradeIntent).where(TradeIntent.run_id.in_(run_ids))
                )
            ).scalars().all()
        }

        # Already-attempted within the window (any open_* on this symbol). Bound
        # to the same lookback so a name entered yesterday isn't re-tried today,
        # while a genuinely new Buy still routes.
        attempted = {
            r[0]
            for r in (
                await s.execute(
                    select(AutoAction.symbol)
                    .where(AutoAction.timestamp >= cutoff)
                    .where(AutoAction.symbol.is_not(None))
                    .where(AutoAction.action_type.like("open_%"))
                    .where(AutoAction.gate_status.in_(["passed", "error"]))
                )
            ).all()
        }

        return [
            (r[0], run_to_theme[r[0]], r[1], float(r[2] or 0))
            for r in rows
            if (r[0], r[1]) not in existing and r[1] not in attempted
        ]


async def _process_one(
    run_id: str, theme_id: str, symbol: str, composite: float,
    *, sizing_factor: float = 1.0, macro_regime: str = "calm",
    pullback_add: bool = False,
) -> bool:
    """Run the gate, build the PMCC, submit it. Returns True if a trade was
    placed (or attempted), False if rejected upstream."""

    # --- Rotation gate: don't add into a sector the institutions are
    # leaving. Low-regret — only blocks NEW entries; holdings are untouched.
    try:
        from .rotation_detector import is_theme_rotating
        rotating, score, signals = await is_theme_rotating(theme_id)
        if rotating:
            logger.info("auto-entry: %s skipped — theme %s rotating out %s", symbol, theme_id, signals)
            # De-dup the audit row: the loop re-evaluates the same blocked
            # candidate every 60s, which previously wrote one row per tick
            # (~800/week of pure noise). Record at most one block per
            # (symbol, day); subsequent ticks just skip silently.
            today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            async with db_session() as s:
                already = (await s.execute(
                    select(AutoAction.id)
                    .where(AutoAction.action_type == "entry_blocked_rotation")
                    .where(AutoAction.symbol == symbol)
                    .where(AutoAction.timestamp >= today)
                    .limit(1)
                )).first()
                if already is None:
                    await record_auto_action(
                        s, loop="entry", action_type="entry_blocked_rotation",
                        gate_result=None, symbol=symbol,
                        payload={"run_id": run_id, "theme_id": theme_id,
                                 "rotation_score": score, "signals": signals},
                        outcome="blocked_rotation",
                    )
            return False
    except Exception as e:
        logger.debug("rotation gate check failed for %s: %s", symbol, e)

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
        # Distinguish a TRANSIENT probe failure (empty/degraded option-params
        # from a flapping data farm — ProviderError.retryable) from a genuine
        # "this name has no usable options" (e.g. an ADR). The candidate de-dup
        # skips any symbol with an `open_*` action in the ~30h window, so
        # recording a transient blip as ineligible would lock a hot name out for
        # over a day — exactly how SNDK (which has a real Jan-2028 LEAP) was
        # silently dropped. On a retryable failure, record NOTHING and return
        # False so the name stays a candidate and is re-probed next tick.
        if getattr(e, "retryable", False):
            logger.warning("auto-entry: %s option-chain probe transient failure — "
                           "will retry next tick (not recorded ineligible): %s", symbol, e)
            return False
        logger.info("auto-entry: %s PMCC probe failed — %s", symbol, e)
        async with db_session() as s:
            await record_auto_action(
                s, loop="entry", action_type="open_pmcc_ineligible",
                gate_result=gate, symbol=symbol,
                payload={"run_id": run_id, "reason": f"probe_error: {e}"},
                outcome="ineligible",
            )
        return True  # genuinely ineligible (no listed options); don't retry today
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
        # LEAPS-only mode: long LEAP calls ONLY — no stock fallback.
        if composite >= 7.0 and is_liquidity_fail and not get_settings().LEAPS_ONLY:
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

    # LEAPS-only strategy: buy the long LEAP outright, no short call. Diverges
    # here from the PMCC combo path below.
    if get_settings().LEAPS_ONLY:
        return await _process_leaps_only(
            run_id=run_id, theme_id=theme_id, symbol=symbol, composite=composite,
            cand=cand, gate=gate, ib=ib, sizing_factor=sizing_factor, macro_regime=macro_regime,
            pullback_add=pullback_add,
        )

    # Manager-conviction tilt: names tracked legendary investors hold with
    # cross-fund confirmation get a bounded size boost (never a gate). Neutral
    # (1.0) for untracked names.
    conviction_factor, conviction_meta = await _manager_conviction(symbol)

    # Quant overlay tilt: the research factory's per-symbol edge sizes the entry
    # up (structurally strong) or down (weak), bounded by QUANT_ENTRY_MAX_TILT.
    # Never a gate. 1.0 (neutral) for names with no feature snapshot. This is the
    # entry-side counterpart to symbol_exit_delta on the exit path.
    quant_factor, quant_edge_score = 1.0, 50.0
    if get_settings().QUANT_OVERLAY_ENABLED:
        try:
            from api.app.research.overlay import symbol_entry_factor
            quant_factor, quant_edge_score = await symbol_entry_factor(symbol)
        except Exception as e:
            logger.debug("auto-entry: quant entry-factor failed for %s: %s", symbol, e)

    # NAV-aware sizing: target ~7% NAV per spread × macro sizing_factor ×
    # conviction × quant tilt, capped at $250k absolute.
    nav = await _fetch_nav(ib)
    n_contracts = _size_pmcc_contracts(
        net_debit_per_spread=cand.net_debit, nav=nav,
        sizing_factor=sizing_factor, conviction_factor=conviction_factor,
        quant_factor=quant_factor,
    )
    if n_contracts <= 0:
        logger.info("auto-entry: %s sized to 0 contracts (macro=%s blocks)",
                    symbol, macro_regime)
        return False
    total_debit = round(cand.net_debit * n_contracts * 100, 2)
    logger.info(
        "auto-entry: %s sized to %d contracts (NAV=$%.0f × sf=%.2f × conv=%.2f × quant=%.2f[edge %.0f], debit/spread=$%.2f, total=$%.0f, macro=%s%s)",
        symbol, n_contracts, nav, sizing_factor, conviction_factor, quant_factor, quant_edge_score,
        cand.net_debit, total_debit, macro_regime,
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
        "quant_factor": quant_factor, "quant_edge_score": quant_edge_score,
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


async def _process_leaps_only(
    *, run_id: str, theme_id: str, symbol: str, composite: float,
    cand: Any, gate, ib: Any, sizing_factor: float = 1.0, macro_regime: str = "calm",
    pullback_add: bool = False,
) -> bool:
    """LEAPS-only entry: buy the long LEAP call outright (no short call, no
    combo). Sized on the full LEAP premium (no short-call credit offsets it),
    boosted by manager conviction, capped by the same NAV/$ limits."""
    from tradingagents.strategies.execution import ExecutionConfig, submit_single_leg_option

    leap_px = cand.leap.mid
    if not leap_px or leap_px <= 0:
        async with db_session() as s:
            await record_auto_action(
                s, loop="entry", action_type="open_leap_ineligible", gate_result=gate,
                symbol=symbol, payload={"run_id": run_id, "reason": "no LEAP price"},
                outcome="ineligible")
        return False

    conviction_factor, conviction_meta = await _manager_conviction(symbol)
    nav = await _fetch_nav(ib)
    # Size on the LEAP's full per-share cost (×100 = per contract).
    n_contracts = _size_pmcc_contracts(
        net_debit_per_spread=leap_px, nav=nav,
        sizing_factor=sizing_factor, conviction_factor=conviction_factor)
    if n_contracts <= 0:
        logger.info("auto-entry LEAPS: %s sized to 0 (macro=%s)", symbol, macro_regime)
        return False
    total_cost = round(leap_px * n_contracts * 100, 2)
    logger.info(
        "auto-entry LEAPS-only: %s BUY %d× LEAP $%.0f %s @ ~$%.2f "
        "(NAV=$%.0f sf=%.2f conv=%.2f total=$%.0f)",
        symbol, n_contracts, cand.leap.strike, cand.leap.expiry, leap_px,
        nav, sizing_factor, conviction_factor, total_cost,
    )

    # ---- Capped-pyramiding guardrails (option B) -----------------------
    _scfg = get_settings()
    # (a) Liquidity: skip illiquid LEAPs whose bid/ask spread is too wide —
    #     they fill far from fair value (the CRDO overpay).
    _bid = getattr(cand.leap, "bid", None)
    _ask = getattr(cand.leap, "ask", None)
    if _bid and _ask and float(_ask) > 0 and leap_px > 0:
        _spr = (float(_ask) - float(_bid)) / leap_px
        if _spr > _scfg.ENTRY_MAX_LEAP_SPREAD_PCT:
            async with db_session() as s:
                await record_auto_action(
                    s, loop="entry", action_type="open_leap_capped", gate_result=gate,
                    symbol=symbol, payload={"run_id": run_id,
                        "reason": f"illiquid: spread {_spr:.0%} > {_scfg.ENTRY_MAX_LEAP_SPREAD_PCT:.0%} of mid"},
                    outcome="capped")
            logger.info("auto-entry LEAPS: %s skipped — wide spread %.0f%%", symbol, _spr * 100)
            return False
    # Size-to-fit the free-funds cushion: rather than block all-or-nothing when a
    # full-size entry would dip below the cushion floor, buy the LARGEST size that
    # still leaves the floor. (Concentration caps below still block, not resize —
    # you don't shrink into an over-concentrated name; that's a real limit.)
    try:
        _acct = await ib.get_account_summary()
        _avail = float(_acct.get("AvailableFunds") or 0)
        _netliq = float(_acct.get("NetLiquidation") or nav)
        _room = _avail - get_settings().ENTRY_MIN_MARGIN_CUSHION * _netliq
        _max_by_margin = int(_room / (leap_px * 100)) if leap_px > 0 else 0
        if 0 < _max_by_margin < n_contracts:
            logger.info("auto-entry LEAPS: %s size-to-fit margin room: %d -> %d contracts "
                        "(avail $%.0f, floor %.0f%%)", symbol, n_contracts, _max_by_margin,
                        _avail, get_settings().ENTRY_MIN_MARGIN_CUSHION * 100)
            n_contracts = _max_by_margin
            total_cost = round(leap_px * n_contracts * 100, 2)
    except Exception as e:
        logger.debug("size-to-fit failed for %s: %s", symbol, e)

    # (b) Per-name + aggregate + per-theme concentration, daily capital, margin.
    _cap = await _check_entry_caps(ib=ib, symbol=symbol, new_cost=total_cost, nav=nav,
                                   theme_id=theme_id)
    if _cap is not None:
        async with db_session() as s:
            await record_auto_action(
                s, loop="entry", action_type="open_leap_capped", gate_result=gate,
                symbol=symbol, payload={"run_id": run_id, "reason": _cap}, outcome="capped")
        logger.info("auto-entry LEAPS: %s capped — %s", symbol, _cap)
        return False

    walking_cfg = {
        "initial_offset_cents": 5, "walk_increment_cents": 5,
        "walk_interval_sec": 30, "max_offset_pct_of_spread": 0.30, "timeout_sec": 300,
        "leap_conid": cand.leap.conid, "spot_at_build": cand.spot,
        "auto_origin": "entry_loop_leaps_only", "nav_at_build": nav,
        "conviction_factor": conviction_factor, "manager_conviction": conviction_meta,
    }
    async with db_session() as s:
        intent = TradeIntent(
            run_id=run_id, symbol=symbol, side="BUY", qty=n_contracts,
            order_type="LMT", status="submitting",
            structure="leap_only", position_state="leap_pending",
            entry_strategy="leaps_pullback_add" if pullback_add else "leaps_only",
            leap_expiry=cand.leap.expiry, leap_strike=cand.leap.strike,
            leap_delta_actual=cand.leap.delta, leap_iv=cand.leap.iv,
            leap_open_interest=cand.leap.open_interest, leap_qty=n_contracts,
            net_debit_target=leap_px, max_loss=total_cost,
            walking_config=walking_cfg, rationale=cand.rationale,
        )
        s.add(intent)
        await s.flush()
        intent_id = intent.id

    await alert(level="info", title=f"Auto-submit LEAP: {symbol}",
                body=f"Long LEAP ${cand.leap.strike:.0f} {cand.leap.expiry} × {n_contracts} "
                     f"@ ~${leap_px:.2f} (LEAPS-only, long calls)")

    fvc = cand.leap.ask if (cand.leap.ask and cand.leap.ask > 0) else round(leap_px * 1.05, 2)
    # Institutional execution: IBKR Adaptive algo works the order toward mid,
    # capped near mid (LEAP_ENTRY_CAP_PCT of the half-spread) so we get price
    # improvement without paying through the cap. Disciplined — abandons if the
    # market won't come to us, rather than crossing the full spread.
    _s = get_settings()
    exec_cfg = ExecutionConfig(initial_offset_cents=5, walk_increment_cents=5,
                               walk_interval_sec=5,
                               # 180s (was 120): the Adaptive algo works toward
                               # mid over minutes; a short budget made us abandon
                               # before the fill landed (then reconcile caught it).
                               max_offset_pct_of_spread=_s.LEAP_ENTRY_CAP_PCT, timeout_sec=180)
    try:
        result = await submit_single_leg_option(
            ibkr=ib, conid=cand.leap.conid, contracts=n_contracts,
            action="BUY", config=exec_cfg, fair_value_ceiling=fvc,
            adaptive_priority=_s.LEAP_ENTRY_ADAPTIVE_PRIORITY)
    except Exception as e:
        logger.exception("LEAP submit failed for %s: %s", symbol, e)
        async with db_session() as s:
            i = await s.get(TradeIntent, intent_id)
            if i:
                i.status = "error"; i.position_state = "error"
            await record_auto_action(
                s, loop="entry", action_type="open_leap", gate_result=gate, symbol=symbol,
                intent_id=intent_id, payload={"run_id": run_id}, outcome="error", error=str(e))
        return True

    async with db_session() as s:
        i = await s.get(TradeIntent, intent_id)
        if result.status == "filled":
            if i:
                i.status = "filled"; i.position_state = "leap_open"
                i.leap_fill_price = result.fill_price
                i.leap_filled_at = datetime.now(timezone.utc)
                i.net_debit_filled = result.fill_price
            await record_auto_action(
                s, loop="entry", action_type="open_leap", gate_result=gate, symbol=symbol,
                intent_id=intent_id,
                payload={"run_id": run_id, "fill_price": result.fill_price,
                         "contracts": n_contracts, "walk_steps": result.walk_steps,
                         "conviction": conviction_meta,
                         # Drives the daily-capital cap in _check_entry_caps.
                         "net_debit_pct_nav": round(total_cost / nav, 4) if nav > 0 else 0.0,
                         "total_cost": total_cost},
                outcome="filled", ibkr_order_id=str(result.order_id or ""))
        else:
            if i:
                i.status = "abandoned" if result.status == "abandoned" else "rejected"
                i.position_state = "abandoned"
            await record_auto_action(
                s, loop="entry", action_type="open_leap", gate_result=gate, symbol=symbol,
                intent_id=intent_id,
                payload={"run_id": run_id, "status": result.status, "reason": result.error},
                outcome=result.status)

    if result.status == "filled":
        await alert(level="info", title=f"LEAP filled: {symbol}",
                    body=f"@${result.fill_price:.2f} × {n_contracts} after {result.walk_steps} steps")
    else:
        await alert(level="warning", title=f"LEAP {result.status}: {symbol}", body=result.error or "")
    return True


async def _manager_conviction(symbol: str) -> tuple[float, dict]:
    """Bounded smart-money sizing tilt for a symbol (1.0 = neutral) — combines
    13F manager conviction with clustered opportunistic insider buying. Lazy
    import + fail-open so the tracker never blocks an entry."""
    try:
        from api.app.hedge_funds.conviction import accumulation_conviction
        return await accumulation_conviction(symbol)
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


async def _has_unmanaged_orphan(ib: Any) -> bool:
    """True if the broker holds an OPTION position whose conid isn't covered by
    any active managing intent (an unmanaged orphan). Used to pause NEW entries
    while the book is inconsistent; returns False (don't block) if positions are
    unreadable — the circuit breaker already fails closed on account issues."""
    try:
        positions = await ib.get_positions()
    except Exception:
        return False
    opt_conids = {int(p.get("conid") or 0) for p in positions
                  if str(p.get("secType") or p.get("sec_type") or "").upper() == "OPT"
                  and float(p.get("qty") or 0) != 0}
    opt_conids.discard(0)
    if not opt_conids:
        return False
    async with db_session() as s:
        rows = (await s.execute(
            select(TradeIntent.walking_config)
            .where(TradeIntent.status.in_(["filled", "submitting", "submitted", "closing"]))
            .where(TradeIntent.position_state.in_(
                ["leap_open", "leap_open_naked", "pmcc_full", "leap_pending", "closing"]))
        )).all()
    managed: set[int] = set()
    for (cfg,) in rows:
        cfg = cfg or {}
        for k in ("leap_conid", "short_call_conid"):
            managed.add(int(cfg.get(k) or 0))
    managed.discard(0)
    return bool(opt_conids - managed)


def _rsi_14(closes: list, period: int = 14) -> "float | None":
    """Wilder-style RSI(14) from a close series. None if too little history."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(-period, 0):
        d = float(closes[i]) - float(closes[i - 1])
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return round(100.0 - 100.0 / (1.0 + rs), 1)


async def _pullback_add_signals(symbol: str, ib: Any) -> "dict | None":
    """Technicals for a pullback-add decision: spot vs SMA20/SMA50, RSI, volume
    ratio, dip off the 20-day high, and whether spot is AT support (within
    PULLBACK_ADD_MA_PROXIMITY_PCT of either MA, from above). None on data gap."""
    from datetime import date, timedelta
    from tradingagents.dataflows.fallback import get_stock_data_with_fallback
    try:
        df = await get_stock_data_with_fallback(
            symbol, date.today() - timedelta(days=140), date.today(), ibkr_provider=ib)
        if df is None or len(df) < 55 or "Close" not in df.columns:
            return None
        closes = df["Close"].astype(float).tolist()
        spot, sma20, sma50 = closes[-1], sum(closes[-20:]) / 20, sum(closes[-50:]) / 50
        high20 = max(closes[-20:])
        rsi = _rsi_14(closes)
        vol_ratio = None
        if "Volume" in df.columns:
            vols = df["Volume"].astype(float).tolist()
            if len(vols) >= 21:
                avg20 = sum(vols[-21:-1]) / 20
                vol_ratio = round(vols[-1] / avg20, 2) if avg20 > 0 else None
        prox = get_settings().PULLBACK_ADD_MA_PROXIMITY_PCT
        # "At support" = spot within prox of the MA and not broken materially below it.
        near20 = sma20 > 0 and abs(spot - sma20) / sma20 <= prox and spot >= sma20 * (1 - prox)
        near50 = sma50 > 0 and abs(spot - sma50) / sma50 <= prox and spot >= sma50 * (1 - prox)
        return {"spot": spot, "sma20": sma20, "sma50": sma50, "high20": high20,
                "rsi": rsi, "vol_ratio": vol_ratio,
                "dip": (high20 - spot) / high20 if high20 > 0 else 0.0,
                "at_support": bool(near20 or near50),
                "which_ma": "sma20" if near20 else ("sma50" if near50 else None)}
    except Exception as e:
        logger.debug("pullback signals failed for %s: %s", symbol, e)
        return None


async def _hedge_funds_rotating_out(symbol: str) -> bool:
    """True if a tracked institution recently reduced/exited this holding
    (stake_watch stake_reduction_flagged in the last 10 days) — a rotation-out
    cue that BLOCKS a pullback add. Fail-open (False) if unavailable."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=10)
        async with db_session() as s:
            row = (await s.execute(
                select(AutoAction.id)
                .where(AutoAction.symbol == symbol)
                .where(AutoAction.action_type == "stake_reduction_flagged")
                .where(AutoAction.timestamp >= cutoff).limit(1))).first()
        return row is not None
    except Exception:
        return False


async def _pullback_added_today(symbol: str) -> bool:
    """At most one pullback add per name per day."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with db_session() as s:
        row = (await s.execute(
            select(AutoAction.id)
            .where(AutoAction.symbol == symbol)
            .where(AutoAction.action_type == "open_leap_pullback")
            .where(AutoAction.timestamp >= start).limit(1))).first()
    return row is not None


async def _find_pullback_adds(ib: Any, exclude: set) -> list:
    """Held theme names dipping to SMA20/SMA50 support that still pass every gate:
    thesis not broken (latest decision != Avoid), theme in-contact, no hedge-fund
    rotation-out, RSI favorable, orderly volume. Returns (run_id, theme_id, symbol,
    composite) tuples routed through the normal capped entry path."""
    s = get_settings()
    if not s.PULLBACK_ADD_ENABLED:
        return []
    from tradingagents.strategies.maintenance.theme_health import is_theme_hot_for_symbol
    from api.app.db import ThemeSymbol
    # UNIVERSE-WIDE: watch EVERY theme symbol (held or not) that carries a recent
    # favorable thesis (latest decision != Avoid, composite >= floor) — not just
    # names we already hold, and nothing symbol-hardcoded. The pullback gates
    # below then pick the ones actually AT support with favorable RSI/volume, so
    # the system acts on a dip opportunity anywhere in the themes.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=s.ENTRY_RUN_LOOKBACK_HOURS)
    async with db_session() as sess:
        rows = (await sess.execute(
            select(TickerScore.symbol, TickerScore.composite, TickerScore.run_id, Run.theme_id)
            .join(Run, Run.id == TickerScore.run_id)
            .join(ThemeSymbol, ThemeSymbol.symbol == TickerScore.symbol)
            .where(Run.status == "done").where(Run.finished_at >= cutoff)
            .where(TickerScore.decision != "Avoid")
            .where(TickerScore.composite >= s.ENTRY_MIN_COMPOSITE)
            .order_by(Run.finished_at.desc()))).all()
    seen: set = set()
    universe: list = []
    excl = {x.upper() for x in exclude}
    for sym, comp, run_id, theme_id in rows:
        u = (sym or "").upper()
        if u in seen or u in excl:
            continue
        seen.add(u)
        universe.append((u, float(comp or 0), run_id, theme_id))
    out: list = []
    for sym, composite, run_id, theme_id in universe:
        try:
            if await _pullback_added_today(sym):
                continue
            async with db_session() as sess:
                hot, _bt = await is_theme_hot_for_symbol(sess, sym)
            if not hot:                                   # theme not in-contact
                continue
            if await _hedge_funds_rotating_out(sym):      # institutions exiting
                continue
            # Notable-bear short = CONTEXT, not a veto. A single short-seller's
            # opinion isn't confirmation, and this setup already passed the
            # bullish gates (theme in-contact, no HF rotation, at support, RSI/
            # volume favorable). Log it so the operator sees the disagreement,
            # but don't block — the short only bites via the exit path, and only
            # when corroborated by real weakness.
            try:
                from api.app.hedge_funds.bearish import notable_short_pressure
                _short, _sm = await notable_short_pressure(sym, [theme_id] if theme_id else None)
                if _short:
                    logger.info("pullback-add: %s — note: notable bear short (%s), but bullish "
                                "setup intact; proceeding (short is context, not a veto)", sym,
                                "; ".join(str(h.get("manager", "")) for h in _sm.get("hits", [])[:2]))
            except Exception as e:
                logger.debug("notable-short check failed for %s: %s", sym, e)
            sig = await _pullback_add_signals(sym, ib)
            if not sig or not sig["at_support"]:
                continue
            if sig["dip"] < s.PULLBACK_ADD_MIN_DIP_PCT:   # not a real pullback
                continue
            if sig["rsi"] is None or not (s.PULLBACK_ADD_RSI_MIN <= sig["rsi"] <= s.PULLBACK_ADD_RSI_MAX):
                continue
            if sig["vol_ratio"] is not None and sig["vol_ratio"] > s.PULLBACK_ADD_MAX_VOLUME_RATIO:
                continue                                   # distribution / panic volume
            logger.info("pullback-add candidate: %s at %s support (spot %.2f, RSI %.0f, vol %.1fx, dip %.0f%%) "
                        "— theme in-contact, no HF rotation",
                        sym, sig["which_ma"], sig["spot"], sig["rsi"] or 0,
                        sig["vol_ratio"] or 0, sig["dip"] * 100)
            out.append((run_id, theme_id, sym, composite))
        except Exception as e:
            logger.debug("pullback-add eval failed for %s: %s", sym, e)
    return out


async def _check_entry_caps(*, ib: Any, symbol: str, new_cost: float, nav: float,
                            theme_id: "str | None" = None) -> "str | None":
    """Pre-submit exposure guardrails. Returns a reason string to BLOCK the
    entry, or None to allow. Checked (in order): per-name, aggregate gross
    premium-at-risk, per-theme concentration, daily capital deployed, and free-
    funds cushion. Fails CLOSED — an unreadable NAV / positions / account blocks
    the entry rather than placing it blind.
    """
    s = get_settings()
    # Fail closed: never size or gate on a blind/failed NAV read.
    if nav <= 0:
        return "NAV unreadable — blocking entry (fail closed)"
    try:
        positions = await ib.get_positions()
    except Exception as e:
        return f"cap check: positions unreadable ({e})"

    def _prem(p: dict) -> float:
        return abs(float(p.get("qty") or 0)) * float(p.get("avg_price") or 0) * 100.0

    held = [p for p in positions if float(p.get("qty") or 0) != 0]
    name_cost = sum(_prem(p) for p in held if (p.get("symbol") or "").upper() == symbol.upper())
    gross_cost = sum(_prem(p) for p in held)

    # 1. Per-name concentration.
    if (name_cost + new_cost) / nav > s.ENTRY_MAX_NAME_PCT_OF_NAV:
        return (f"per-name cap: {symbol} would be {(name_cost + new_cost) / nav:.0%} of NAV "
                f"(cap {s.ENTRY_MAX_NAME_PCT_OF_NAV:.0%}; held ${name_cost:,.0f} + new ${new_cost:,.0f})")

    # 2. AGGREGATE gross premium-at-risk across the whole (correlated) book.
    if (gross_cost + new_cost) / nav > s.AUTO_MAX_GROSS_PREMIUM_PCT_NAV:
        return (f"aggregate exposure cap: total open premium would be "
                f"{(gross_cost + new_cost) / nav:.0%} of NAV "
                f"(cap {s.AUTO_MAX_GROSS_PREMIUM_PCT_NAV:.0%}; held ${gross_cost:,.0f} + new ${new_cost:,.0f})")

    # 3. Per-theme concentration (correlated cluster).
    if theme_id:
        try:
            from api.app.db import ThemeSymbol
            async with db_session() as sess:
                theme_syms = {
                    (r[0] or "").upper() for r in (await sess.execute(
                        select(ThemeSymbol.symbol).where(ThemeSymbol.theme_id == theme_id)
                    )).all() if r[0]
                }
            theme_cost = sum(_prem(p) for p in held if (p.get("symbol") or "").upper() in theme_syms)
            if (theme_cost + new_cost) / nav > s.AUTO_MAX_THEME_PREMIUM_PCT_NAV:
                return (f"theme concentration cap: {theme_id} would be "
                        f"{(theme_cost + new_cost) / nav:.0%} of NAV "
                        f"(cap {s.AUTO_MAX_THEME_PREMIUM_PCT_NAV:.0%})")
        except Exception as e:
            logger.debug("theme-cap check failed for %s/%s: %s", symbol, theme_id, e)

    # 4. Daily capital deployed (the previously-dead 80%/day cap). Sum today's
    #    FILLED entries' recorded net_debit_pct_nav; block if this entry pushes
    #    the day past the ceiling.
    try:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        async with db_session() as sess:
            rows = (await sess.execute(
                select(AutoAction.payload)
                .where(AutoAction.timestamp >= start)
                .where(AutoAction.loop == "entry")
                .where(AutoAction.outcome == "filled")
            )).scalars().all()
        deployed = sum(float(p.get("net_debit_pct_nav") or 0.0) for p in rows if isinstance(p, dict))
        new_pct = new_cost / nav
        cap = get_settings_cap("AUTO_MAX_CAPITAL_DEPLOYED_PER_DAY")
        if deployed + new_pct > cap:
            return (f"daily capital cap: {deployed:.0%} deployed + {new_pct:.0%} new "
                    f"> {cap:.0%}/day")
    except Exception as e:
        logger.debug("daily-capital cap check failed for %s: %s", symbol, e)

    # 5. Free-funds cushion after the buy.
    try:
        acct = await ib.get_account_summary()
        netliq = float(acct.get("NetLiquidation") or 0)
        avail = float(acct.get("AvailableFunds") or 0)
    except Exception as e:
        return f"cap check: account unreadable ({e})"
    if netliq <= 0:
        return "account NetLiquidation unreadable — blocking entry (fail closed)"
    cushion_after = (avail - new_cost) / netliq
    if cushion_after < s.ENTRY_MIN_MARGIN_CUSHION:
        return (f"margin headroom: entry would leave {cushion_after:.1%} cushion "
                f"(min {s.ENTRY_MIN_MARGIN_CUSHION:.0%}; avail ${avail:,.0f} - cost ${new_cost:,.0f})")
    return None


def get_settings_cap(name: str) -> float:
    """Read a numeric cap from auto_gate.DEFAULT_CAPS (single source of truth)."""
    from .auto_gate import DEFAULT_CAPS
    return float(DEFAULT_CAPS[name])


def _size_pmcc_contracts(
    *, net_debit_per_spread: float, nav: float,
    sizing_factor: float = 1.0, conviction_factor: float = 1.0,
    quant_factor: float = 1.0,
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
        return 0   # fail closed: no valid price / blind NAV → do not size an entry
    if sizing_factor <= 0:
        return 0
    # Conviction lifts the % target and the quant edge tilts it (bidirectional:
    # strong name sizes up, weak sizes down), THEN the absolute $ cap clamps,
    # THEN the macro factor tightens — order matters so the cap is never
    # exceeded even when conviction × quant both boost.
    target = min(nav * PMCC_TARGET_PCT_NAV * max(conviction_factor, 1.0) * max(quant_factor, 0.0),
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
        limit_px=limit, tif="DAY", account_mode=get_settings().IBKR_MODE,
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
                outcome="error", payload={"error": str(e)},
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
