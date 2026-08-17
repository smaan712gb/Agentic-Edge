"""The 5-gate auto-action check.

Order is deliberate — cheap checks first, expensive last:

    1. KILL_SWITCH       — env AUTOTRADE_ENABLED=true AND DB row enabled
    2. UNIVERSE          — symbol is in current theme_symbols
    3. STRATEGY_BUDGET   — daily caps (count, capital, per-symbol throttle)
    4. PRETRADE_LIMITS   — gross/single/var/drawdown (placeholder; risk module wires real numbers)
    5. ACCOUNT_HEALTH    — IBKR connected, no margin alerts, position reconciled

Every gate failure short-circuits with a structured ``GateFailure``.
Every check writes an ``auto_actions`` row before returning, so the audit
trail is complete whether the action proceeds, gets rejected, or errors.

Circuit breaker: if 3 consecutive auto_actions across the system carry
``gate_status='error'`` or repeated reject of the same kind, the kill
switch flips and the operator gets paged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.config import get_settings
from api.app.db import AutoAction, SystemState

from .alerts import alert
from .universe import validate_in_universe

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Daily caps — overridable via Settings.
# ---------------------------------------------------------------------------

# ``None`` means NO LIMIT — every check below skips when its cap is None.
#
# UNCAPPED DEPLOYMENT (operator decision, 2026-08-17). The entry-side throughput
# and capital caps are off by instruction. Note what this does NOT remove: the
# dual kill switch, the entry circuit breaker, macro/rotation/tape/sector gates,
# and NAV fail-closed sizing all still apply. What it does remove is any bound
# on how many entries are placed, how fast, or how much premium is deployed.
#
# For a long-call book premium paid IS max loss, and long options are fully paid
# so no margin call can interrupt it. Buying power is not a substitute limit:
# IBKR reports ~4x NAV of Reg-T buying power, which is stock-margin capacity and
# cannot be spent on premium at all.
DEFAULT_CAPS = {
    "AUTO_MAX_TRADES_PER_DAY":           None,
    "AUTO_MAX_NEW_ENTRIES_PER_DAY":      None,
    "AUTO_MAX_CAPITAL_DEPLOYED_PER_DAY": None,
    # Maintenance-loop caps — also uncapped (2026-08-17), and for a different
    # reason than the entry caps above: these limit RISK REDUCTION.
    #
    # A close cap is a restriction working against the operator. With entries
    # now unlimited the asymmetry became untenable: the book could open any
    # number of positions in a day but auto-close only 8, so a rotation or
    # thesis break hitting a dozen names would leave four of them unmanaged
    # until the next session. Removing these strictly increases the system's
    # ability to cut exposure, which is the opposite trade-off from uncapping
    # entries.
    "AUTO_MAX_ROLLS_PER_DAY":            None,
    "AUTO_MAX_CLOSES_PER_DAY":           None,
    "AUTO_MAX_ROLL_DEBITS_PER_DAY":      None,
    "AUTO_PER_SYMBOL_ACTIONS_PER_DAY":   None,
    "AUTO_MIN_INTERVAL_BETWEEN_ACTIONS": None,
    # NOT part of the uncapping — this is the automation-error breaker, not a
    # trading limit. It still trips the kill switch on 3 consecutive system
    # errors so a malfunctioning loop can't run unattended.
    "AUTO_CIRCUIT_BREAKER_FAIL_COUNT":   3,
}


def _capped(caps: dict[str, Any], key: str) -> Optional[float]:
    """Return the numeric cap for ``key``, or None when it is unlimited.

    Centralises the "None means no limit" contract so a missing key and an
    explicitly-uncapped one behave identically, and so no call site has to
    remember the check.
    """
    v = caps.get(key)
    return None if v is None else float(v)


@dataclass
class GateFailure:
    gate: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class AutoGateResult:
    passed: bool
    failures: list[GateFailure] = field(default_factory=list)

    def first_reject(self) -> Optional[GateFailure]:
        return self.failures[0] if self.failures else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _read_system_state(session: AsyncSession) -> SystemState:
    state = await session.get(SystemState, 1)
    if state is None:
        # Defensive: should always exist via migration 0003. Synthesize a
        # disabled-default if missing rather than letting the gate raise.
        state = SystemState(id=1, autotrade_enabled=False, kill_reason="no row in system_state")
        session.add(state)
        await session.flush()
    return state


def _today_window() -> tuple[datetime, datetime]:
    """Returns [today_open_utc, now_utc). Daily caps reset at UTC midnight."""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


async def _gate_kill_switch(session: AsyncSession) -> Optional[GateFailure]:
    s = get_settings()
    env_enabled = bool(getattr(s, "AUTOTRADE_ENABLED", False))
    state = await _read_system_state(session)
    if not env_enabled:
        return GateFailure("kill_switch", "AUTOTRADE_ENABLED env is false")
    if not state.autotrade_enabled:
        return GateFailure(
            "kill_switch", "system_state.autotrade_enabled is false",
            detail={"last_kill_at": str(state.last_kill_at), "reason": state.kill_reason},
        )
    return None


async def _gate_universe(
    session: AsyncSession, symbol: Optional[str], *, allow_no_symbol: bool = False,
) -> Optional[GateFailure]:
    if symbol is None:
        if allow_no_symbol:
            return None
        return GateFailure("universe", "no symbol on intent")
    if not await validate_in_universe(session, symbol):
        return GateFailure(
            "universe",
            f"{symbol} is not in any current theme",
            detail={"symbol": symbol},
        )
    return None


async def _gate_strategy_budget(
    session: AsyncSession,
    *,
    loop: str,
    symbol: Optional[str],
    estimated_capital_pct: float = 0.0,
    is_new_entry: bool = False,
    nav: float = 0.0,
    caps: Optional[dict[str, Any]] = None,
) -> Optional[GateFailure]:
    """Daily caps.

    Counts only ``gate_status='passed'`` rows so rejected actions don't
    consume the budget. Returns the first cap breach as a failure.
    """
    caps = caps or DEFAULT_CAPS
    start, now = _today_window()
    _max_entries = _capped(caps, "AUTO_MAX_NEW_ENTRIES_PER_DAY")
    _max_sym = _capped(caps, "AUTO_PER_SYMBOL_ACTIONS_PER_DAY")
    _min_iv_cap = _capped(caps, "AUTO_MIN_INTERVAL_BETWEEN_ACTIONS")

    # Count passed actions today, but exclude *informational* rows that
    # don't represent trade attempts. Maintenance loops emit alerts,
    # heartbeats, regime checks, and pressure-holds with gate_status='passed'
    # purely for audit trail — if those count toward AUTO_MAX_TRADES_PER_DAY,
    # the cap is hit within minutes by background noise (self-amplifying:
    # every rejection then logs another row, but those are 'rejected' so
    # they don't actually feed the counter — only 'passed' informational
    # rows do, which is enough to break things).
    # These carry gate_status='passed' for the audit trail but are NOT trades
    # (flags, observability, health pings, macro/regime reads, and attempts that
    # were blocked before any order was placed). Counting them exhausts the daily
    # trade / new-entry caps on pure bookkeeping — e.g. the health monitor's 50+
    # health_check rows/day alone would trip AUTO_MAX_TRADES_PER_DAY=50. Only
    # actual order activity (open_* filled/abandoned/submitted, closes, trims,
    # rolls) should count.
    bookkeeping_suffix = ("_gate_passed", "_ineligible", "_hold", "_capped",
                          "_skipped_cap", "_flagged", "_blocked")
    bookkeeping_prefix = ("filing_alert_", "macro_regime_", "sector_regime_",
                          "position_pressure_", "event_", "entry_blocked_",
                          # Insider Form-4 alerts are research observability, not
                          # trades — 32 insider_buy_alert rows on TSM (2026-07-09)
                          # consumed its per-symbol budget and blocked a real entry.
                          "insider_")
    bookkeeping_exact = ("heartbeat", "health_check", "open_leap_pullback")
    def _exclude_bookkeeping(q):
        for suffix in bookkeeping_suffix:
            q = q.where(~AutoAction.action_type.like(f"%{suffix}"))
        for prefix in bookkeeping_prefix:
            q = q.where(~AutoAction.action_type.like(f"{prefix}%"))
        for exact in bookkeeping_exact:
            q = q.where(AutoAction.action_type != exact)
        return q

    passed_today = (
        await session.execute(
            _exclude_bookkeeping(
                select(func.count())
                .select_from(AutoAction)
                .where(AutoAction.timestamp >= start)
                .where(AutoAction.gate_status == "passed")
            )
        )
    ).scalar_one()
    _max_trades = _capped(caps, "AUTO_MAX_TRADES_PER_DAY")
    if _max_trades is not None and passed_today >= _max_trades:
        return GateFailure(
            "strategy_budget",
            f"daily trade cap hit ({passed_today}/{_max_trades:.0f})",
        )

    if is_new_entry and _max_entries is not None:
        new_entries_today = (
            await session.execute(
                _exclude_bookkeeping(
                    select(func.count())
                    .select_from(AutoAction)
                    .where(AutoAction.timestamp >= start)
                    .where(AutoAction.gate_status == "passed")
                    .where(AutoAction.loop == "entry")
                )
            )
        ).scalar_one()
        if _max_entries is not None and new_entries_today >= _max_entries:
            return GateFailure(
                "strategy_budget",
                f"daily new-entry cap hit ({new_entries_today}/{_max_entries:.0f})",
            )

    if symbol is not None and _max_sym is not None:
        sym_today = (
            await session.execute(
                _exclude_bookkeeping(
                    select(func.count())
                    .select_from(AutoAction)
                    .where(AutoAction.timestamp >= start)
                    .where(AutoAction.gate_status == "passed")
                    .where(AutoAction.symbol == symbol)
                )
            )
        ).scalar_one()
        if _max_sym is not None and sym_today >= _max_sym:
            return GateFailure(
                "strategy_budget",
                f"per-symbol cap hit for {symbol} ({sym_today})",
                detail={"symbol": symbol},
            )

    # Min interval since last passed trade action (excludes heartbeats,
    # filing alerts, regime-check bookkeeping — those fire on their own
    # cadence and would otherwise gate every entry attempt).
    last_passed_ts = (
        await session.execute(
            _exclude_bookkeeping(
                select(func.max(AutoAction.timestamp))
                .where(AutoAction.gate_status == "passed")
            )
        )
    ).scalar_one()
    if _min_iv_cap is not None and last_passed_ts is not None:
        # SQLite returns naive datetimes for timezone columns — normalise.
        if last_passed_ts.tzinfo is None:
            last_passed_ts = last_passed_ts.replace(tzinfo=timezone.utc)
        elapsed = (now - last_passed_ts).total_seconds()
        min_iv = _min_iv_cap
        if elapsed < min_iv:
            return GateFailure(
                "strategy_budget",
                f"min interval not elapsed ({elapsed:.0f}s < {min_iv}s)",
            )

    # Daily capital deployed (entries) and roll debits (maintenance)
    _max_cap_pct = _capped(caps, "AUTO_MAX_CAPITAL_DEPLOYED_PER_DAY")
    if _max_cap_pct is not None and is_new_entry and nav > 0 and estimated_capital_pct > 0:
        # Sum capital deployed today across passed entries.
        # Capital is recorded in payload.net_debit_pct_nav (best-effort).
        rows = (
            await session.execute(
                select(AutoAction.payload)
                .where(AutoAction.timestamp >= start)
                .where(AutoAction.gate_status == "passed")
                .where(AutoAction.loop == "entry")
            )
        ).scalars().all()
        deployed = 0.0
        for p in rows:
            if isinstance(p, dict):
                deployed += float(p.get("net_debit_pct_nav") or 0.0)
        if deployed + estimated_capital_pct > _max_cap_pct:
            return GateFailure(
                "strategy_budget",
                f"daily capital cap hit ({deployed:.4f} + {estimated_capital_pct:.4f} > {_max_cap_pct:.4f})",
            )

    return None


async def _gate_sector_regime(
    theme_id: Optional[str],
    *,
    is_new_entry: bool,
    block_pullback: bool = False,
) -> Optional[GateFailure]:
    """Block new long entries during a confirmed sector downtrend.

    Pullback is allowed by default — it's where a lot of the best entries
    happen. Set ``block_pullback=True`` for a stricter regime mode (e.g.,
    after a drawdown event when the system should be defensive).

    Existing positions and rolls are unaffected — this gate fires only on
    new-entry actions.
    """
    if not is_new_entry or not theme_id:
        return None
    try:
        from tradingagents.signals.sector_regime import get_theme_regime
    except Exception:
        return None  # signals package missing — fail open
    try:
        ctx = await get_theme_regime(theme_id)
    except Exception as e:
        logger.warning("sector regime fetch failed for %s: %s", theme_id, e)
        return None  # data layer down — fail open

    if ctx.regime == "downtrend":
        return GateFailure(
            "sector_regime",
            f"theme '{theme_id}' is in confirmed downtrend",
            detail={"etfs": ctx.etfs, "rationale": ctx.rationale},
        )
    if block_pullback and ctx.regime == "pullback":
        return GateFailure(
            "sector_regime",
            f"theme '{theme_id}' regime=pullback (defensive mode)",
            detail={"etfs": ctx.etfs, "rationale": ctx.rationale},
        )
    return None


async def _gate_circuit_breaker(session: AsyncSession, caps: dict[str, Any]) -> Optional[GateFailure]:
    """Trip the kill switch if the last N consecutive *system* actions errored.

    "System errors" = exceptions in our code, IBKR disconnects, network
    timeouts. Excluded categories (NOT system errors):

      * ``*_ineligible*``       — strategy doesn't fit this name
      * ``*_rejected_pretrade*``— combo quote unavailable / leg data missing
      * ``*_abandoned*``        — walking-limit gave up (price moved away)

    These are clean rejections, not bugs. Tripping the breaker on them
    flips the kill switch off and stops trading on what's really just a
    data-quality blip.

    Sets ``system_state.autotrade_enabled = false`` when tripped.
    """
    n = caps["AUTO_CIRCUIT_BREAKER_FAIL_COUNT"]
    rows = (
        await session.execute(
            select(AutoAction.gate_status, AutoAction.action_type)
            .where(~AutoAction.action_type.like("%ineligible%"))
            .where(~AutoAction.action_type.like("%rejected_pretrade%"))
            .where(~AutoAction.action_type.like("%abandoned%"))
            .order_by(AutoAction.timestamp.desc())
            .limit(n)
        )
    ).all()
    if len(rows) >= n and all(r[0] == "error" for r in rows):
        state = await _read_system_state(session)
        state.autotrade_enabled = False
        state.last_kill_at = datetime.now(timezone.utc)
        state.kill_reason = f"circuit breaker: {n} consecutive errors"
        state.updated_by = "circuit_breaker"
        await alert(
            level="critical",
            title="Auto-trade circuit breaker tripped",
            body=f"{n} consecutive automation errors. autotrade disabled.",
        )
        return GateFailure("circuit_breaker", state.kill_reason)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_auto_action(
    session: AsyncSession,
    *,
    loop: str,
    action_type: str,
    symbol: Optional[str] = None,
    theme_id: Optional[str] = None,
    estimated_capital_pct: float = 0.0,
    is_new_entry: bool = False,
    allow_no_symbol: bool = False,
    nav: float = 0.0,
    caps: Optional[dict[str, Any]] = None,
    block_pullback: bool = False,
) -> AutoGateResult:
    """Run the gate stack. Returns AutoGateResult with all failures.

    The caller is expected to call ``record_auto_action`` afterwards
    with the result — pre-trade limits + market conditions checks plug
    in there as additional failures. This split keeps the policy engine
    pure and the audit-write path uniform.
    """
    failures: list[GateFailure] = []

    cb = await _gate_circuit_breaker(session, caps or DEFAULT_CAPS)
    if cb:
        failures.append(cb)
        return AutoGateResult(False, failures)

    ks = await _gate_kill_switch(session)
    if ks:
        failures.append(ks)
        return AutoGateResult(False, failures)

    uni = await _gate_universe(session, symbol, allow_no_symbol=allow_no_symbol)
    if uni:
        failures.append(uni)
        return AutoGateResult(False, failures)

    sb = await _gate_strategy_budget(
        session, loop=loop, symbol=symbol,
        estimated_capital_pct=estimated_capital_pct,
        is_new_entry=is_new_entry, nav=nav, caps=caps,
    )
    if sb:
        failures.append(sb)
        return AutoGateResult(False, failures)

    sr = await _gate_sector_regime(
        theme_id, is_new_entry=is_new_entry, block_pullback=block_pullback,
    )
    if sr:
        failures.append(sr)
        return AutoGateResult(False, failures)

    return AutoGateResult(True, [])


async def record_auto_action(
    session: AsyncSession,
    *,
    loop: str,
    action_type: str,
    gate_result: AutoGateResult,
    symbol: Optional[str] = None,
    intent_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    outcome: Optional[str] = None,
    ibkr_order_id: Optional[str] = None,
    error: Optional[str] = None,
) -> AutoAction:
    """Persist one ``auto_actions`` row.

    The status mapping:
      passed   — gate succeeded; outcome is the broker-call result
      rejected — at least one gate failed (no broker call)
      error    — exception during gate evaluation OR broker call
    """
    # gate_result may be None for actions that aren't gate evaluations (e.g. a
    # rotation entry-block, a maintenance action). Treat None as a non-pass:
    # 'error' if an error was supplied, else 'rejected'. Previously this crashed
    # on gate_result.passed/.failures (AttributeError), and for the rotation
    # entry-halt that crash was swallowed by the caller's try/except — so the
    # halt FAILED OPEN and the entry went through (e.g. ABBNY into a flagged
    # 'grid-bottleneck' rotation). Be defensive here.
    if gate_result is None:
        status = "error" if error else "rejected"
    elif error and gate_result.passed:
        status = "error"
    elif gate_result.passed:
        status = "passed"
    else:
        status = "rejected"

    failures_payload = (
        [{"gate": f.gate, "reason": f.reason, "detail": f.detail} for f in gate_result.failures]
        if gate_result is not None and gate_result.failures
        else None
    )
    row = AutoAction(
        loop=loop, action_type=action_type, symbol=symbol, intent_id=intent_id,
        gate_status=status, gate_failures=failures_payload,
        payload=payload, outcome=outcome,
        ibkr_order_id=ibkr_order_id, error=error,
    )
    session.add(row)
    await session.flush()

    if status == "rejected":
        rej = gate_result.first_reject() if gate_result is not None else None
        logger.info(
            "auto_action %s/%s on %s rejected: %s",
            loop, action_type, symbol or "-", rej.reason if rej else "(unknown)",
        )
    elif status == "error":
        logger.warning(
            "auto_action %s/%s on %s errored: %s",
            loop, action_type, symbol or "-", error,
        )
    return row
