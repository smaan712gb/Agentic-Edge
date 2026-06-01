"""15-minute system health monitor — the 'no unknowns' guardrail.

A live automated fund must never silently drift into a bad state. This
module runs a fixed battery of invariant checks every 15 minutes and
fires an alert the moment any of them breaks. It NEVER trades and never
mutates positions — it only observes and reports.

The checks are ordered by how dangerous a silent failure would be:

  1. Broker reachable        — blind = can't manage anything (critical)
  2. Kill-switch / breaker    — is auto-trading armed? breaker latched?
  3. Position<->intent parity — every live position has a managing intent
                                (orphan = unmanaged = the silent-drawdown
                                failure mode we just fixed) and every
                                'filled' intent still has a live position
                                (phantom = stale bookkeeping).
  4. Margin cushion           — approaching the breaker's hard floor
  5. Stuck intents            — 'submitting' wedged past the walk timeout
  6. Signal freshness         — newest scorecard older than a trading day

Anything at warning/critical fans out through ``alert(...)`` (logs always,
Slack if configured). A clean pass logs a one-line heartbeat and records a
``health_check`` row in ``auto_actions`` so the dashboard shows the monitor
is alive.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from ..config import get_settings
from ..db import SystemState, TradeIntent, Run, get_session as db_session
from .alerts import alert
from .auto_gate import AutoGateResult, record_auto_action

logger = logging.getLogger("agentic_edge.health")

# Intent states that mean "we are actively holding / managing a position".
_ACTIVE_STATES = ("filled",)
_ACTIVE_POS_STATES = ("leap_open", "leap_open_naked", "pmcc_full", "leap_pending", "closing")

# Thresholds.
_STUCK_SUBMIT_MIN = 10          # 'submitting' longer than this is wedged
_SIGNAL_STALE_HOURS = 36        # newest done run older than this = stale
_MARGIN_WARN_BUFFER = 0.05      # warn this far ABOVE the breaker's hard floor


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def run_health_check() -> dict[str, Any]:
    """Run every invariant check, alert on anomalies, return a summary dict.

    Each check is independently guarded — one failing probe must not blind
    the others. The function itself never raises.
    """
    settings = get_settings()
    findings: list[dict[str, str]] = []   # {level, title, detail}
    metrics: dict[str, Any] = {}

    def add(level: str, title: str, detail: str = "") -> None:
        findings.append({"level": level, "title": title, "detail": detail})

    # --- 1. Broker reachable + snapshot ---------------------------------
    positions: list[dict] = []
    acct: dict[str, str] = {}
    ib_ok = False
    try:
        from ..positions import _ibkr
        prov = await _ibkr()
        positions = await prov.get_positions()
        acct = await prov.get_account_summary()
        ib_ok = True
    except Exception as e:
        add("critical", "Broker unreachable", f"IBKR snapshot failed: {e}")

    live = [p for p in (positions or []) if float(p.get("qty") or 0) != 0]
    live_syms = sorted({(p.get("symbol") or "").upper() for p in live})
    metrics["live_positions"] = len(live)
    metrics["broker_ok"] = ib_ok

    # --- 2. Kill-switch / breaker state ---------------------------------
    armed = None
    breaker_tripped = None
    try:
        async with db_session() as s:
            state = await s.get(SystemState, 1)
            if state is not None:
                armed = bool(state.autotrade_enabled)
                breaker_tripped = bool(getattr(state, "entry_breaker_tripped", False))
                metrics["armed"] = armed
                metrics["breaker_tripped"] = breaker_tripped
                if breaker_tripped:
                    add("warning", "Entry circuit-breaker is LATCHED",
                        f"reason: {getattr(state, 'entry_breaker_reason', None)} "
                        f"(new entries halted; positions still managed). Re-arm when resolved.")
                if not settings.AUTOTRADE_ENABLED:
                    add("warning", "AUTOTRADE_ENABLED=false (env kill switch)",
                        "Loops are running but the env flag blocks all new entries.")
                elif armed is False:
                    add("warning", "DB kill switch is OFF (autotrade_enabled=false)",
                        "No new entries will be placed until re-armed.")
    except Exception as e:
        add("warning", "Could not read system_state", str(e))

    # --- 3. Position <-> intent parity (the orphan/phantom guard) -------
    # Only meaningful with a trustworthy position snapshot. If the broker
    # is unreachable we have positions=[] artificially, which would make
    # every holding look like a phantom — the 'Broker unreachable' critical
    # above already covers that case, so skip parity entirely here.
    if not ib_ok:
        metrics["parity_check"] = "skipped (broker down)"
    else:
      try:
        async with db_session() as s:
            rows = (
                await s.execute(
                    select(TradeIntent.symbol, TradeIntent.structure,
                           TradeIntent.position_state, TradeIntent.walking_config)
                    .where(TradeIntent.status.in_(_ACTIVE_STATES))
                    .where(TradeIntent.position_state.in_(_ACTIVE_POS_STATES))
                )
            ).all()
        intent_syms = {(r[0] or "").upper() for r in rows}
        # conid set from intents (leap_conid lives in walking_config)
        intent_conids: set[int] = set()
        for _sym, _struct, _ps, cfg in rows:
            try:
                cid = int((cfg or {}).get("leap_conid") or 0)
                if cid:
                    intent_conids.add(cid)
            except Exception:
                pass

        # ORPHANS: live position with no active managing intent. Match on
        # both conid (exact) and symbol (covers stock / pre-conid intents).
        orphans = []
        for p in live:
            sym = (p.get("symbol") or "").upper()
            cid = int(p.get("conid") or 0)
            if cid and cid in intent_conids:
                continue
            if sym in intent_syms:
                continue
            orphans.append(sym or f"conid:{cid}")
        if orphans:
            add("critical", f"{len(orphans)} UNMANAGED position(s)",
                f"Live at broker with no active intent: {', '.join(sorted(set(orphans)))}. "
                f"No exit/rotation logic is watching these — reconcile immediately.")
        metrics["orphans"] = sorted(set(orphans))

        # PHANTOMS: active 'filled' intent with no matching live position.
        phantoms = sorted(intent_syms - set(live_syms))
        if phantoms:
            add("warning", f"{len(phantoms)} phantom intent(s)",
                f"Marked filled but no live position: {', '.join(phantoms)}. "
                f"Likely closed outside the system; should be reconciled to closed.")
        metrics["phantoms"] = phantoms
        metrics["managed_intents"] = len(intent_syms)
      except Exception as e:
        add("warning", "Position/intent parity check failed", str(e))

    # --- 4. Margin cushion vs breaker floor -----------------------------
    try:
        nav = float(acct.get("NetLiquidation") or 0)
        maint = float(acct.get("MaintMarginReq") or 0)
        if nav > 0:
            cushion = (nav - maint) / nav
            metrics["nav"] = round(nav)
            metrics["margin_cushion_pct"] = round(cushion * 100, 1)
            floor = float(settings.BREAKER_MIN_MARGIN_CUSHION_PCT)
            if cushion < floor:
                add("critical", "Margin cushion BELOW breaker floor",
                    f"cushion {cushion:.1%} < floor {floor:.0%} — breaker will halt entries.")
            elif cushion < floor + _MARGIN_WARN_BUFFER:
                add("warning", "Margin cushion thin",
                    f"cushion {cushion:.1%} approaching the {floor:.0%} breaker floor.")
    except Exception as e:
        add("warning", "Margin cushion check failed", str(e))

    # --- 5. Stuck 'submitting' intents ----------------------------------
    try:
        cutoff = _utcnow() - timedelta(minutes=_STUCK_SUBMIT_MIN)
        async with db_session() as s:
            stuck = (
                await s.execute(
                    select(TradeIntent.symbol, TradeIntent.updated_at)
                    .where(TradeIntent.status == "submitting")
                )
            ).all()
        wedged = [sym for sym, upd in stuck if (_aware(upd) or _utcnow()) < cutoff]
        if wedged:
            add("warning", f"{len(wedged)} intent(s) stuck submitting",
                f"> {_STUCK_SUBMIT_MIN} min: {', '.join(sorted(set(wedged)))}. "
                f"The entry-loop watchdog should abandon these; verify it's running.")
        metrics["stuck_submitting"] = len(wedged)
    except Exception as e:
        add("warning", "Stuck-intent check failed", str(e))

    # --- 6. Signal freshness --------------------------------------------
    try:
        async with db_session() as s:
            last = (
                await s.execute(
                    select(Run.finished_at).where(Run.status == "done")
                    .order_by(Run.finished_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
        if last is not None:
            age_h = (_utcnow() - _aware(last)).total_seconds() / 3600.0
            metrics["newest_scorecard_age_h"] = round(age_h, 1)
            if age_h > _SIGNAL_STALE_HOURS:
                add("warning", "Scorecards are stale",
                    f"Newest completed run is {age_h:.0f}h old (> {_SIGNAL_STALE_HOURS}h). "
                    f"Entries/exits are acting on aging signals — check the theme scheduler.")
        else:
            add("warning", "No completed runs found", "No scorecard signal exists yet.")
    except Exception as e:
        add("warning", "Signal-freshness check failed", str(e))

    # --- Dispatch -------------------------------------------------------
    crit = [f for f in findings if f["level"] == "critical"]
    warn = [f for f in findings if f["level"] == "warning"]
    for f in crit + warn:
        await alert(level=f["level"], title=f"[health] {f['title']}", body=f["detail"])

    status = "critical" if crit else ("warning" if warn else "ok")
    summary = (
        f"health={status} | broker={'ok' if ib_ok else 'DOWN'} | "
        f"positions={len(live)} managed={metrics.get('managed_intents', '?')} "
        f"orphans={len(metrics.get('orphans', []))} phantoms={len(metrics.get('phantoms', []))} | "
        f"armed={armed} breaker={breaker_tripped} | "
        f"nav={metrics.get('nav', '?')} cushion={metrics.get('margin_cushion_pct', '?')}%"
    )
    if status == "ok":
        logger.info("health check OK | %s", summary)
    else:
        logger.warning("health check %s | %s", status.upper(), summary)

    # Audit row so the dashboard shows the heartbeat.
    try:
        async with db_session() as s:
            await record_auto_action(
                s, loop="monitor", action_type="health_check",
                gate_result=AutoGateResult(passed=True, failures=[]),
                payload={"metrics": metrics,
                         "findings": [{"level": f["level"], "title": f["title"]} for f in findings]},
                outcome=status,
            )
    except Exception as e:
        logger.warning("health check: could not persist audit row: %s", e)

    return {"status": status, "metrics": metrics, "findings": findings, "summary": summary}
