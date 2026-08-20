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

from sqlalchemy import func, select

from ..config import get_settings
from ..db import AutoAction, SystemState, TradeIntent, Run, get_session as db_session
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

# Process start. The maint-loop liveness check compares against the newest
# heartbeat IN THE DB, which after a restart still belongs to the previous run —
# so a healthy boot pages CRITICAL ("heartbeat 10083 min old") ~8 seconds in,
# before the loop's first tick has had any chance to write one. Crying wolf on
# every start is what trains an operator to scroll past the alert that matters;
# this system already lost 78h of exit coverage inside that noise.
_BOOT_TS = datetime.now(timezone.utc)
# Maint loop sleeps 15s then ticks every 300s, so give it two full cycles.
_BOOT_GRACE_MIN = 11.0


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

    # --- 1b. Long-only invariant ----------------------------------------
    # Ranked directly under "broker reachable" because it outranks every
    # check below it: an undefined-risk short leg in a long-only book is a
    # worse state than a thin margin cushion or a stale signal, and unlike
    # those it can never be explained by market conditions.
    if ib_ok and settings.LEAPS_ONLY:
        try:
            from .position_guard import (
                short_option_positions, describe_short_options, halt_on_short_options,
            )
            shorts = short_option_positions(live)
            metrics["short_options"] = len(shorts)
            if shorts:
                # LATCH here, don't merely report. This monitor is the only
                # always-on detector of the breach: the entry loop returns on
                # the DB kill switch before it ever reaches check_entry_breaker,
                # so while the book is disarmed nothing else evaluates the
                # invariant. On 2026-08-18 a short FN call sat open 5h30m
                # raising this CRITICAL every 15 min with entry_breaker_tripped
                # = False throughout — the alert told the operator entries were
                # halted by the breaker when the only thing halting them was a
                # hand-thrown kill switch. Latching makes the message true and
                # makes the breach outlive a re-arm, which is the whole point:
                # a mandate breach should need a human to look at the book.
                await halt_on_short_options(live, source="health_monitor")
                metrics["short_options_latched"] = True
                add("critical", f"LONG-ONLY BREACH — {len(shorts)} short option position(s)",
                    f"{describe_short_options(shorts)}. Undefined risk in a long-call-only "
                    f"book; entry breaker LATCHED. Flatten manually, then re-arm.")
        except Exception as e:
            add("warning", "Long-only invariant check failed", str(e))

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
            sec = str(p.get("secType") or p.get("sec_type") or "").upper()
            if cid and cid in intent_conids:
                continue                       # exact conid match — managed
            # For OPTIONS a bare symbol match is NOT sufficient: a second contract
            # on an already-held underlying (different conid) is its own orphan
            # and must be surfaced, not hidden behind the symbol. Only fall back
            # to a symbol match for non-option (stock) positions.
            if sec != "OPT" and sym in intent_syms:
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

    # --- 5b. Maintenance-loop liveness ----------------------------------
    # A dead maint loop = exits stop firing while everything else looks green.
    # The supervisor auto-restarts a crashed loop, but if that also fails this
    # catches it: during RTH the loop writes a heartbeat auto_action each ~5-min
    # tick; a newest heartbeat older than ~15 min means the loop is not running.
    try:
        from api.app.autotrade.market_conditions import gate_rth, is_first_15_min
        if gate_rth() is None and not is_first_15_min():   # only meaningful during RTH
            async with db_session() as s:
                last_hb = (
                    await s.execute(
                        select(func.max(AutoAction.timestamp))
                        .where(AutoAction.loop == "maintenance")
                        .where(AutoAction.action_type == "heartbeat")
                    )
                ).scalar_one_or_none()
            age_min = None if last_hb is None else (_utcnow() - _aware(last_hb)).total_seconds() / 60.0
            metrics["maint_heartbeat_age_min"] = None if age_min is None else round(age_min, 1)
            # Boot grace covers "this process is young". It does NOT cover the
            # case that actually fires every morning: the server boots premarket,
            # so by 09:30 it is well past _BOOT_GRACE_MIN, but the maintenance
            # loop is RTH-gated and cannot have written a heartbeat yet — the
            # newest one is still yesterday's. On 2026-08-20 that produced a
            # CRITICAL "loop STALLED" at 09:30:00 for a loop that was healthy and
            # heartbeated normally at 09:34:24. Suppressing the first 15 minutes
            # of RTH (the loop polls every 300s, so its first tick can legitimately
            # be a full interval after the bell) is handled by the is_first_15_min
            # guard above. A CRITICAL that is routinely wrong is one the operator
            # learns to skip past, which costs more than the alarm is worth.
            uptime_min = (_utcnow() - _BOOT_TS).total_seconds() / 60.0
            metrics["uptime_min"] = round(uptime_min, 1)
            if uptime_min < _BOOT_GRACE_MIN:
                # Still inside the boot window — the newest heartbeat is the
                # PREVIOUS process's, which says nothing about this one.
                metrics["maint_liveness"] = f"grace ({uptime_min:.1f}/{_BOOT_GRACE_MIN:.0f} min)"
            elif age_min is None or age_min > 15.0:
                add("critical", "Maintenance loop appears STALLED",
                    f"newest maint heartbeat is {('never' if age_min is None else f'{age_min:.0f} min')} old "
                    f"(expect ≤5 min during RTH) — exits may not be firing. Check the loop/supervisor.")
    except Exception as e:
        add("warning", "Maint-loop liveness check failed", str(e))

    # --- 5c. Rotation-detector freshness --------------------------------
    # The entry/exit loops ignore rotation flags older than ROTATION_MAX_AGE_HOURS
    # (stale flags describe a market that no longer exists). That fail-open is
    # correct, but it means a sweep that has stopped running leaves the book with
    # NO rotation protection at all — and nothing else would say so. The sweep
    # itself takes ~80 min per pass, so a single failure is easy to miss.
    try:
        from ..db import ThemeRotation
        async with db_session() as s:
            newest = (
                await s.execute(select(func.max(ThemeRotation.computed_at)))
            ).scalar_one_or_none()
        max_age_h = float(getattr(settings, "ROTATION_MAX_AGE_HOURS", 6.0))
        rot_age_h = None if newest is None else (
            (_utcnow() - _aware(newest)).total_seconds() / 3600.0)
        metrics["rotation_age_h"] = None if rot_age_h is None else round(rot_age_h, 1)
        if rot_age_h is None:
            add("warning", "Rotation detector has never run",
                "No theme_rotation rows exist — new entries are NOT rotation-gated.")
        elif rot_age_h > max_age_h:
            add("warning", "Rotation signals are STALE — book is un-gated",
                f"Newest rotation state is {rot_age_h:.1f}h old (ignored above "
                f"{max_age_h:.0f}h). Entries are proceeding with NO rotation "
                f"protection and exits get no rotation pressure. Check the sweep.")
    except Exception as e:
        add("warning", "Rotation freshness check failed", str(e))

    # --- 5d. Macro volatility guardrail is actually reading ---------------
    # A blind macro read classifies as 'calm' -> sizing x1.0, which looks
    # identical to a genuinely quiet tape. If the guardrail is inert, that must
    # be stated, not inferred.
    try:
        async with db_session() as s:
            row = (
                await s.execute(
                    select(AutoAction.payload)
                    .where(AutoAction.action_type.like("macro_regime_%"))
                    .order_by(AutoAction.timestamp.desc()).limit(1)
                )
            ).scalar_one_or_none()
        if isinstance(row, dict):
            blind = row.get("vix") is None and row.get("spx_change_pct") is None
            metrics["macro_vix"] = row.get("vix")
            metrics["macro_blind"] = blind
            if blind:
                add("critical", "Macro volatility guardrail is BLIND",
                    "No VIX and no SPX from broker or fallback — regime defaults "
                    "to 'calm' (sizing x1.0), so elevated/defensive/panic can "
                    "never fire and entries size full into any tape.")
    except Exception as e:
        add("warning", "Macro-guardrail check failed", str(e))

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

    # --- 7. Feed integrity ----------------------------------------------
    # Checks 1-6 all answer "is the machine running". They stayed green through
    # six defects in which the machine ran perfectly and computed garbage — a
    # dead feed is indistinguishable from a quiet one unless something watches
    # the DATA rather than the process. 5c and 5d are hand-written instances of
    # exactly that idea; this is the general form, so the next dead feed does
    # not need someone to have anticipated it.
    try:
        from .feed_integrity import run_feed_integrity_check
        fi = await run_feed_integrity_check()
        metrics["feeds_observed"] = fi.get("feeds_observed", 0)
        metrics["feed_anomalies"] = len(fi.get("anomalies", []))
        for a in fi.get("anomalies", []):
            add(a["level"], f"Feed '{a['feed']}' — {a['kind']}", a["detail"])
    except Exception as e:
        add("warning", "Feed-integrity check failed", str(e))

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
