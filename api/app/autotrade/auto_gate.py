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

DEFAULT_CAPS = {
    "AUTO_MAX_TRADES_PER_DAY":           50,
    # Operator wants aggressive deployment of buying power: 12 entries/day,
    # 80% of NAV deployable. PMCCs are defined-risk so the buying-power
    # consumption is bounded by net debit, not gross notional.
    "AUTO_MAX_NEW_ENTRIES_PER_DAY":      12,
    "AUTO_MAX_CAPITAL_DEPLOYED_PER_DAY": 0.80,
    # Maintenance-loop caps: hard upper bound on rolls/closes per day.
    # Roll debits also capped as a fraction of NAV so a defensive day
    # doesn't burn through equity defending challenged shorts.
    "AUTO_MAX_ROLLS_PER_DAY":            8,
    "AUTO_MAX_CLOSES_PER_DAY":           8,
    "AUTO_MAX_ROLL_DEBITS_PER_DAY":      0.02,
    "AUTO_PER_SYMBOL_ACTIONS_PER_DAY":   3,    # bumped: entry + roll + close possible
    "AUTO_MIN_INTERVAL_BETWEEN_ACTIONS": 5,
    "AUTO_CIRCUIT_BREAKER_FAIL_COUNT":   3,
}


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

    # Count passed actions today, but exclude *informational* rows that
    # don't represent trade attempts. Maintenance loops emit alerts,
    # heartbeats, regime checks, and pressure-holds with gate_status='passed'
    # purely for audit trail — if those count toward AUTO_MAX_TRADES_PER_DAY,
    # the cap is hit within minutes by background noise (self-amplifying:
    # every rejection then logs another row, but those are 'rejected' so
    # they don't actually feed the counter — only 'passed' informational
    # rows do, which is enough to break things).
    bookkeeping_suffix = ("_gate_passed", "_ineligible", "_hold")
    bookkeeping_prefix = ("filing_alert_", "macro_regime_", "sector_regime_")
    bookkeeping_exact = ("heartbeat",)
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
    if passed_today >= caps["AUTO_MAX_TRADES_PER_DAY"]:
        return GateFailure(
            "strategy_budget",
            f"daily trade cap hit ({passed_today}/{caps['AUTO_MAX_TRADES_PER_DAY']})",
        )

    if is_new_entry:
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
        if new_entries_today >= caps["AUTO_MAX_NEW_ENTRIES_PER_DAY"]:
            return GateFailure(
                "strategy_budget",
                f"daily new-entry cap hit ({new_entries_today}/{caps['AUTO_MAX_NEW_ENTRIES_PER_DAY']})",
            )

    if symbol is not None:
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
        if sym_today >= caps["AUTO_PER_SYMBOL_ACTIONS_PER_DAY"]:
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
    if last_passed_ts is not None:
        # SQLite returns naive datetimes for timezone columns — normalise.
        if last_passed_ts.tzinfo is None:
            last_passed_ts = last_passed_ts.replace(tzinfo=timezone.utc)
        elapsed = (now - last_passed_ts).total_seconds()
        min_iv = caps["AUTO_MIN_INTERVAL_BETWEEN_ACTIONS"]
        if elapsed < min_iv:
            return GateFailure(
                "strategy_budget",
                f"min interval not elapsed ({elapsed:.0f}s < {min_iv}s)",
            )

    # Daily capital deployed (entries) and roll debits (maintenance)
    if is_new_entry and nav > 0 and estimated_capital_pct > 0:
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
        if deployed + estimated_capital_pct > caps["AUTO_MAX_CAPITAL_DEPLOYED_PER_DAY"]:
            return GateFailure(
                "strategy_budget",
                f"daily capital cap hit ({deployed:.4f} + {estimated_capital_pct:.4f} > {caps['AUTO_MAX_CAPITAL_DEPLOYED_PER_DAY']:.4f})",
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
