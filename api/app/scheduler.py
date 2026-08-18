"""Daily theme-runner scheduler.

Phase A of the automation roadmap: scheduled signal generation. The
scheduler:

  * Reads the cron expression from ``system_state.scheduler_cron``.
  * Default fires every weekday at 09:00 ET — 30 minutes before US open,
    so analysis is fresh when the market starts and the maintenance
    window we identified for entries (10:00–14:00) has scorecards ready.
  * Iterates every theme in ``themes`` table; for each, kicks off a Run
    via the same ``real_run`` (or ``_simulate_run`` if ``USE_MOCK_RUN=1``)
    code path the manual button uses. Idempotency-keyed so re-fires the
    same day are no-ops.
  * Updates ``scheduler_next_run_at`` / ``_last_run_at`` / ``_last_status``
    so the UI can show whether the auto-pilot is alive.

This module never trades. It only generates signals (scorecards). Trade
execution lives in a future ``autotrade/loops.py`` and is gated by
``AUTOTRADE_ENABLED`` + ``system_state.autotrade_enabled``, both off by
default.

Single-instance assumption: APScheduler is in-process. For multi-instance
deployments, swap to an external trigger (e.g. Render cron hitting
``POST /api/admin/scheduler/run-now``) and disable the in-process job.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .config import get_settings
from .db import SystemState, Theme, Run, get_session as db_session
from .repos import RunRepo, ThemeRepo

logger = logging.getLogger("agentic_edge.scheduler")


_SCHEDULER: Optional[AsyncIOScheduler] = None
_JOB_ID = "theme_daily_run"
_CBA_JOB_ID = "closing_accumulation_hourly"
# Hourly during US RTH, Mon-Fri. CBA gate 2 (AH follow-through) only
# populates after 16:00 ET, so the 16:00 + 17:00 firings are the most
# meaningful for entry signal. Earlier-hour runs still capture gate 1
# (RVOL, VWAP position) so the operator can see intraday accumulation
# building before the close.
_CBA_CRON = "0 10-17 * * 1-5"

_ROTATION_JOB_ID = "theme_rotation_sweep"
# 4x/day during RTH, deliberately slow. Rotation is a MULTI-DAY institutional
# move; sampling it every few minutes just measures normal intraday pullbacks
# and manufactures flags that aren't real. The previous 30-min cron was also
# fictional in practice — one sweep takes ~80 min, so with max_instances=1 most
# fires were dropped and the true cadence was ~90 min by accident rather than
# by design. This states the intent explicitly and leaves no overlap.
#
# Pairs with ROTATION_CONFIRM_SWEEPS: a theme must look like it's rotating on
# consecutive sweeps before entries are halted, so confirmation takes hours (as
# a multi-day signal should) instead of one noisy reading.
_ROTATION_CRON = "5 10,12,14,16 * * 1-5"

_NEWS_JOB_ID = "chokepoint_news_sweep"
# Hourly during US RTH + an hour either side, Mon-Fri. News flows during the
# session; the sweep dedups so re-runs are cheap.
_NEWS_CRON = "30 8-17 * * 1-5"

_HEALTH_JOB_ID = "system_health_monitor"
# Every 15 min across the extended trading day (4:00-20:00 ET), Mon-Fri.
# The monitor never trades — it checks invariants (broker reachable,
# kill-switch state, position<->intent parity, margin cushion, stuck
# intents, signal freshness) and alerts on anomalies so nothing drifts
# silently. Always-on, independent of the auto-fire flag.
_HEALTH_CRON = "*/15 4-20 * * 1-5"

_EDGAR_JOB_ID = "edgar_signal_sweep"
# Every 15 min during extended hours, Mon-Fri. EDGAR filings post on a slow
# cadence (13F quarterly; 13D/Form-4 sporadic) and the sweep skips anything
# already stored, so this is well under SEC's fair-access limit while still
# catching a new 13D within minutes during the trading day.
_EDGAR_CRON = "*/15 8-20 * * 1-5"

_STAKE_JOB_ID = "stake_watch_sweep"
# Subject-company 13D/13G watch over holdings + universe. Heavier than the
# manager poller (one list_filings per subject), so every 30 min during
# extended hours is a sane cadence — still catches a same-day 13D/13G/A while
# staying well under SEC's fair-access limit (idempotent via accession_no).
_STAKE_CRON = "5,35 8-20 * * 1-5"

_FEATURE_SNAPSHOT_JOB_ID = "feature_snapshot"
# Once daily after the close (16:30 ET, Mon-Fri) — capture each theme symbol's
# point-in-time feature row on settled EOD data. Idempotent per (symbol, day).
_FEATURE_SNAPSHOT_CRON = "30 16 * * 1-5"

_FEATURE_LABEL_JOB_ID = "feature_label_backfill"
# Daily at 17:00 ET — backfill realised forward returns onto snapshots whose
# forward window has elapsed. Idempotent; only touches non-final rows.
_FEATURE_LABEL_CRON = "0 17 * * 1-5"

_OVERLAY_RECAL_JOB_ID = "quant_overlay_recalibrate"
# Daily at 17:15 ET — AFTER the label backfill, so the IC harness sees the
# freshest forward returns. Re-blends each quant signal's weight off its theory
# prior toward measured predictiveness. Fully autonomous self-tuning.
_OVERLAY_RECAL_CRON = "15 17 * * 1-5"

_PORTFOLIO_JOB_ID = "portfolio_daily_decision"
# 16:20 ET — after the close, before the 16:30 feature snapshot. Deliberately
# ONCE a day and on closing data: re-deciding strategic exposure intraday is
# how a fund becomes a day trader. The 2026-08-18 tape gate halting every entry
# on a -4.8% session, while the weekly picture read mid-range, is the failure
# this cadence exists to prevent. Intraday information informs the NEXT
# scheduled decision rather than pre-empting this one.
_PORTFOLIO_CRON = "20 16 * * 1-5"

_MORNING_REPORT_JOB_ID = "morning_report_dispatch"
# 08:45 ET weekdays — after the overnight EDGAR/stake/insider sweeps, before
# the 09:00 theme run. The brief reads only persisted artifacts (yesterday's
# scorecards + pressure bands, overnight filings), so it is deliberately
# pre-market; the /api/report/morning endpoint always assembles fresh on
# demand for the dashboard.
_MORNING_REPORT_CRON = "45 8 * * 1-5"

_PULSE_JOB_ID = "intraday_pulse_dispatch"
# 10:30 ET — institutions typically finish opening repositioning by 10:00-10:30;
# this is the first read of the day that isn't opening noise. The /api/report/
# pulse endpoint serves it live all session with a 3-minute cache.
_PULSE_CRON = "30 10 * * 1-5"

_PULSE_MONITOR_JOB_ID = "intraday_pulse_monitor"
# Every 15 min during RTH: refresh the pulse and alert ONLY when the state
# CHANGES (day type or semis-rotation direction) — a live monitor, not spam.
_PULSE_MONITOR_CRON = "*/15 9-16 * * 1-5"

_MISSED_RUN_JOB_ID = "missed_run_watchdog"
# Every 30 min, 09:00-15:30 ET weekdays. Recovers the daily theme run if it was
# missed (process down at 09:00, or scheduler enabled AFTER 09:00) — the gap
# that left 2026-06-29 with zero signals. Idempotent: no-ops if a completed run
# already exists today. Registered unconditionally; checks scheduler_enabled
# itself, so it recovers even when the scheduler was enabled after boot.
_MISSED_RUN_CRON = "*/30 9-15 * * 1-5"


# ---------------------------------------------------------------------------
# Public API — start / stop / fire
# ---------------------------------------------------------------------------


async def start_scheduler() -> None:
    """Bring up the in-process scheduler. Idempotent."""
    global _SCHEDULER
    if _SCHEDULER is not None:
        return

    state = await _read_state_or_seed()

    # Pin the scheduler to the CURRENTLY RUNNING uvicorn loop. Without an
    # explicit event_loop, AsyncIOScheduler falls back to asyncio.get_event_loop()
    # at start(), which under uvicorn + Python 3.12/3.13 can resolve to a loop
    # that isn't the one being driven — jobs then get scheduled (next_run
    # computed, scheduler reports "running") but their coroutines never fire.
    # This is the same loop-binding gotcha the IBKR provider hit.
    _SCHEDULER = AsyncIOScheduler(
        timezone="America/New_York",
        event_loop=asyncio.get_running_loop(),
    )
    if state.scheduler_enabled:
        _add_or_update_job(state.scheduler_cron)
    # Closing-Bell Accumulation sweep: hourly during RTH, always-on
    # (independent of theme_daily_run's enabled flag — operator wants
    # this signal whether or not auto-fire is on).
    _SCHEDULER.add_job(
        _run_cba_sweep_job,
        trigger=CronTrigger.from_crontab(_CBA_CRON, timezone="America/New_York"),
        id=_CBA_JOB_ID, replace_existing=True,
        misfire_grace_time=300, max_instances=1, coalesce=True,
    )
    # Hedge Fund Signal Tracker sweep — always-on, independent of the
    # auto-fire flag (this is decision-support, not execution).
    if get_settings().EDGAR_POLL_ENABLED:
        _SCHEDULER.add_job(
            _run_edgar_sweep_job,
            trigger=CronTrigger.from_crontab(_EDGAR_CRON, timezone="America/New_York"),
            id=_EDGAR_JOB_ID, replace_existing=True,
            misfire_grace_time=300, max_instances=1, coalesce=True,
        )
        # Subject-company 13D/13G stake watch over holdings + universe —
        # near-real-time institutional-stake + abrupt-exit signal.
        _SCHEDULER.add_job(
            _run_stake_watch_job,
            trigger=CronTrigger.from_crontab(_STAKE_CRON, timezone="America/New_York"),
            id=_STAKE_JOB_ID, replace_existing=True,
            misfire_grace_time=300, max_instances=1, coalesce=True,
        )
    # Phase 3 chokepoint news sweep — always-on (decision-support).
    if get_settings().NEWS_SWEEP_ENABLED:
        _SCHEDULER.add_job(
            _run_news_sweep_job,
            trigger=CronTrigger.from_crontab(_NEWS_CRON, timezone="America/New_York"),
            id=_NEWS_JOB_ID, replace_existing=True,
            misfire_grace_time=300, max_instances=1, coalesce=True,
        )
    # Theme rotation detector — always-on (feeds entry + maint loops).
    if get_settings().ROTATION_DETECTOR_ENABLED:
        _SCHEDULER.add_job(
            _run_rotation_sweep_job,
            trigger=CronTrigger.from_crontab(_ROTATION_CRON, timezone="America/New_York"),
            id=_ROTATION_JOB_ID, replace_existing=True,
            misfire_grace_time=300, max_instances=1, coalesce=True,
        )
    # Quant Research Factory — nightly feature snapshot + label backfill.
    # Always-on decision-support; never trades, no gate reads its output.
    if get_settings().FEATURE_FACTORY_ENABLED:
        _SCHEDULER.add_job(
            _run_feature_snapshot_job,
            trigger=CronTrigger.from_crontab(_FEATURE_SNAPSHOT_CRON, timezone="America/New_York"),
            id=_FEATURE_SNAPSHOT_JOB_ID, replace_existing=True,
            misfire_grace_time=600, max_instances=1, coalesce=True,
        )
        _SCHEDULER.add_job(
            _run_feature_label_job,
            trigger=CronTrigger.from_crontab(_FEATURE_LABEL_CRON, timezone="America/New_York"),
            id=_FEATURE_LABEL_JOB_ID, replace_existing=True,
            misfire_grace_time=600, max_instances=1, coalesce=True,
        )
        if get_settings().QUANT_OVERLAY_ENABLED:
            _SCHEDULER.add_job(
                _run_overlay_recalibrate_job,
                trigger=CronTrigger.from_crontab(_OVERLAY_RECAL_CRON, timezone="America/New_York"),
                id=_OVERLAY_RECAL_JOB_ID, replace_existing=True,
                misfire_grace_time=600, max_instances=1, coalesce=True,
            )
    # System health monitor — always-on 'no unknowns' guardrail. Never
    # trades; alerts on any broken invariant every 15 min.
    _SCHEDULER.add_job(
        _run_health_monitor_job,
        trigger=CronTrigger.from_crontab(_HEALTH_CRON, timezone="America/New_York"),
        id=_HEALTH_JOB_ID, replace_existing=True,
        misfire_grace_time=300, max_instances=1, coalesce=True,
    )
    # Portfolio-level exposure decision — one instruction per day, post-close.
    if get_settings().PORTFOLIO_LAYER_ENABLED:
        _SCHEDULER.add_job(
            _run_portfolio_decision_job,
            trigger=CronTrigger.from_crontab(_PORTFOLIO_CRON, timezone="America/New_York"),
            id=_PORTFOLIO_JOB_ID, replace_existing=True,
            misfire_grace_time=900, max_instances=1, coalesce=True,
        )
    # Morning Report — daily CIO brief pushed through the alert fan-out.
    # Always-on decision-support; never trades.
    _SCHEDULER.add_job(
        _run_morning_report_job,
        trigger=CronTrigger.from_crontab(_MORNING_REPORT_CRON, timezone="America/New_York"),
        id=_MORNING_REPORT_JOB_ID, replace_existing=True,
        misfire_grace_time=600, max_instances=1, coalesce=True,
    )
    # Intraday pulse — 10:30 ET day-type read pushed to the alert channel.
    _SCHEDULER.add_job(
        _run_pulse_dispatch_job,
        trigger=CronTrigger.from_crontab(_PULSE_CRON, timezone="America/New_York"),
        id=_PULSE_JOB_ID, replace_existing=True,
        misfire_grace_time=600, max_instances=1, coalesce=True,
    )
    # 15-minute live monitor — refresh + alert on day-type/rotation CHANGES.
    _SCHEDULER.add_job(
        _run_pulse_monitor_job,
        trigger=CronTrigger.from_crontab(_PULSE_MONITOR_CRON, timezone="America/New_York"),
        id=_PULSE_MONITOR_JOB_ID, replace_existing=True,
        misfire_grace_time=300, max_instances=1, coalesce=True,
    )
    # Missed-run watchdog — always-on safety net that recovers the daily theme
    # run if it was skipped (process down at 09:00, or scheduler enabled after
    # 09:00). Registered unconditionally; _ensure_todays_run checks the enabled
    # flag itself. Idempotent.
    _SCHEDULER.add_job(
        _run_missed_run_watchdog,
        trigger=CronTrigger.from_crontab(_MISSED_RUN_CRON, timezone="America/New_York"),
        id=_MISSED_RUN_JOB_ID, replace_existing=True,
        misfire_grace_time=300, max_instances=1, coalesce=True,
    )
    _SCHEDULER.start()
    logger.info(
        "scheduler started (enabled=%s, cron=%s)",
        state.scheduler_enabled, state.scheduler_cron,
    )
    # Startup catch-up — if the 09:00 ET daily run was missed because the
    # server wasn't running then, fire it shortly after boot (no-ops if today's
    # run already exists). Registered UNCONDITIONALLY now — _ensure_todays_run
    # checks scheduler_enabled itself, so a server that boots with the scheduler
    # off and has it enabled moments later still recovers today's signals.
    _SCHEDULER.add_job(
        _startup_catchup_job, trigger="date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=45),
        id="startup_catchup", replace_existing=True, misfire_grace_time=300,
    )
    await _refresh_next_run_at()


async def stop_scheduler() -> None:
    global _SCHEDULER
    if _SCHEDULER is None:
        return
    try:
        _SCHEDULER.shutdown(wait=False)
    finally:
        _SCHEDULER = None
        logger.info("scheduler stopped")


async def enable_scheduler(actor: str = "operator") -> dict:
    """Turn the cron on and persist the change."""
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is None:
            raise RuntimeError("system_state singleton missing")
        state.scheduler_enabled = True
        state.updated_by = actor
        cron = state.scheduler_cron
    if _SCHEDULER is not None:
        _add_or_update_job(cron)
        await _refresh_next_run_at()
        # Same-day catch-up: if enabled after 09:00 ET with no run yet today,
        # recover it now (background one-shot so this call returns immediately).
        _SCHEDULER.add_job(
            _run_missed_run_watchdog,
            trigger="date", run_date=datetime.now(timezone.utc) + timedelta(seconds=5),
            id=f"enable-catchup-{int(datetime.now(timezone.utc).timestamp())}",
            replace_existing=False, misfire_grace_time=120,
        )
    logger.info("scheduler enabled by %s", actor)
    return await scheduler_status()


async def disable_scheduler(actor: str = "operator") -> dict:
    """Turn the cron off and clear the next-run hint. The in-process
    scheduler keeps running so re-enabling is instant."""
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is None:
            raise RuntimeError("system_state singleton missing")
        state.scheduler_enabled = False
        state.scheduler_next_run_at = None
        state.updated_by = actor
    if _SCHEDULER is not None and _SCHEDULER.get_job(_JOB_ID) is not None:
        _SCHEDULER.remove_job(_JOB_ID)
    logger.info("scheduler disabled by %s", actor)
    return await scheduler_status()


async def update_cron(new_cron: str, actor: str = "operator") -> dict:
    """Replace the cron expression. Validates by attempting to build the trigger."""
    # Validate before persisting.
    CronTrigger.from_crontab(new_cron, timezone="America/New_York")
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is None:
            raise RuntimeError("system_state singleton missing")
        state.scheduler_cron = new_cron
        state.updated_by = actor
        if state.scheduler_enabled and _SCHEDULER is not None:
            _add_or_update_job(new_cron)
    await _refresh_next_run_at()
    return await scheduler_status()


async def fire_now() -> str:
    """Fire the job immediately (in addition to the cron). Returns a label
    for the operator. Useful for ad-hoc triggers that should still flow
    through the persisted scheduled-run path (e.g., to verify the cron
    plumbing without waiting for the next cron tick)."""
    if _SCHEDULER is None:
        raise RuntimeError("scheduler is not running in this process")
    _SCHEDULER.add_job(_run_all_themes_job, id=f"manual-fire-{int(datetime.now().timestamp())}",
                       replace_existing=False, misfire_grace_time=60)
    return "scheduled to fire"


async def scheduler_status() -> dict:
    """Snapshot used by the admin endpoint and the frontend badge."""
    async with db_session() as s:
        state = await s.get(SystemState, 1)
    job = _SCHEDULER.get_job(_JOB_ID) if _SCHEDULER else None
    next_fire = job.next_run_time if job and job.next_run_time else None
    return {
        "running":              _SCHEDULER is not None and _SCHEDULER.running,
        "enabled":              bool(state and state.scheduler_enabled),
        "cron":                 state.scheduler_cron if state else None,
        "next_run_at":          next_fire.isoformat() if next_fire else None,
        "last_run_at":          state.scheduler_last_run_at.isoformat() if state and state.scheduler_last_run_at else None,
        "last_status":          state.scheduler_last_status if state else None,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _add_or_update_job(cron: str) -> None:
    assert _SCHEDULER is not None
    trigger = CronTrigger.from_crontab(cron, timezone="America/New_York")
    if _SCHEDULER.get_job(_JOB_ID) is not None:
        _SCHEDULER.reschedule_job(_JOB_ID, trigger=trigger)
    else:
        _SCHEDULER.add_job(
            _run_all_themes_job, trigger=trigger, id=_JOB_ID,
            replace_existing=True, misfire_grace_time=600,
            max_instances=1, coalesce=True,
        )


async def _read_state_or_seed() -> SystemState:
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is None:
            state = SystemState(id=1, autotrade_enabled=False)
            s.add(state)
            await s.flush()
        return state


async def _refresh_next_run_at() -> None:
    """Persist the next-run timestamp for the UI (informational)."""
    if _SCHEDULER is None:
        return
    job = _SCHEDULER.get_job(_JOB_ID)
    next_fire = job.next_run_time if job and job.next_run_time else None
    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is not None:
            state.scheduler_next_run_at = next_fire


# ---------------------------------------------------------------------------
# Closing-Bell Accumulation sweep — hourly during RTH
# ---------------------------------------------------------------------------


async def _run_cba_sweep_job() -> None:
    """Fire run_closing_accumulation_sweep on its hourly cron tick.

    Idempotent: the sweep upserts one row per (symbol, today's-date), so
    each hour's run overwrites the prior hour's snapshot with the latest
    intraday state. After 16:00 ET the AH metrics populate; runs before
    that still capture gate 1 (RVOL, VWAP position) which is useful for
    seeing accumulation building during the day.
    """
    try:
        from .autotrade.maint_loop import run_closing_accumulation_sweep
        result = await run_closing_accumulation_sweep()
        logger.info(
            "CBA sweep tick: %d themes, %d symbols, %d setups",
            result.get("themes_scanned", 0),
            result.get("symbols_evaluated", 0),
            result.get("setups_found", 0),
        )
    except Exception as e:
        logger.exception("CBA sweep tick failed: %s", e)


async def _run_rotation_sweep_job() -> None:
    """Fire the theme rotation detector on its cron tick."""
    try:
        from .autotrade.rotation_detector import run_rotation_sweep
        result = await run_rotation_sweep()
        if result.get("skipped_reason"):
            logger.info("rotation sweep tick skipped: %s", result["skipped_reason"])
        else:
            logger.info("rotation sweep tick: %d themes, %d flagged",
                        result.get("themes", 0), result.get("flagged", 0))
    except Exception as e:
        logger.exception("rotation sweep tick failed: %s", e)


async def _run_health_monitor_job() -> None:
    """Fire the system health monitor on its cron tick. Never trades."""
    try:
        from .autotrade.health_monitor import run_health_check
        result = await run_health_check()
        logger.info("health monitor tick: %s", result.get("summary", result.get("status")))
    except Exception as e:
        logger.exception("health monitor tick failed: %s", e)


async def _run_news_sweep_job() -> None:
    """Fire the chokepoint news sweep on its cron tick. Idempotent (deduped)."""
    try:
        from .hedge_funds.news import run_news_sweep
        result = await run_news_sweep()
        if result.get("skipped_reason"):
            logger.info("news sweep tick skipped: %s", result["skipped_reason"])
        else:
            logger.info("news sweep tick: %d symbols, %d hits, %d stored",
                        result.get("symbols", 0), result.get("hits", 0), result.get("stored", 0))
    except Exception as e:
        logger.exception("news sweep tick failed: %s", e)


async def _run_feature_snapshot_job() -> None:
    """Nightly quant-feature snapshot across the theme universe. Idempotent
    per (symbol, day). Research-only — never trades, no gate reads it."""
    try:
        from .research.features import build_snapshot
        result = await build_snapshot()
        logger.info("feature snapshot tick: %d symbols, %d written",
                    result.get("symbols", 0), result.get("written", 0))
    except Exception as e:
        logger.exception("feature snapshot tick failed: %s", e)


async def _run_feature_label_job() -> None:
    """Backfill realised forward returns onto elapsed snapshots. Idempotent."""
    try:
        from .research.labeler import run_labeler
        result = await run_labeler()
        logger.info("feature label tick: %d symbols, %d rows updated, %d finalised",
                    result.get("symbols", 0), result.get("rows_updated", 0),
                    result.get("finalised", 0))
    except Exception as e:
        logger.exception("feature label tick failed: %s", e)


async def _run_overlay_recalibrate_job() -> None:
    """Autonomously re-tune the quant-overlay signal weights toward measured IC.
    Runs after the label backfill so the harness sees the freshest returns."""
    try:
        from .research.overlay import recalibrate_weights
        rec = await asyncio.to_thread(recalibrate_weights, None)
        logger.info("overlay recalibrate tick: source=%s n_labelled=%s",
                    rec.get("source"), rec.get("n_labelled"))
    except Exception as e:
        logger.exception("overlay recalibrate tick failed: %s", e)


async def _run_edgar_sweep_job() -> None:
    """Fire the Hedge Fund Signal Tracker EDGAR sweep on its 15-min tick.

    Idempotent: new filings are keyed by accession number, so each tick only
    ingests what's appeared since the last. No-ops cleanly when
    EDGAR_USER_AGENT_EMAIL is unset (logged in the sweep)."""
    try:
        from .hedge_funds.poller import run_edgar_sweep
        result = await run_edgar_sweep()
        if result.get("skipped_reason"):
            logger.info("EDGAR sweep tick skipped: %s", result["skipped_reason"])
        else:
            logger.info(
                "EDGAR sweep tick: %d managers, %d new filings, %d holdings, %d changes",
                result.get("managers_scanned", 0), result.get("new_filings", 0),
                result.get("holdings_upserted", 0), result.get("changes_computed", 0),
            )
    except Exception as e:
        logger.exception("EDGAR sweep tick failed: %s", e)


async def _run_portfolio_decision_job() -> None:
    """Post-close: one instruction for the whole book, persisted for the loops."""
    try:
        from .portfolio.daily import dispatch_daily_decision
        from .positions import _ibkr
        d = await dispatch_daily_decision(await _ibkr())
        logger.info("portfolio decision tick: %s (%s)", d["instruction"], d["state"])
    except Exception as e:
        logger.exception("portfolio decision tick failed: %s", e)


async def _run_morning_report_job() -> None:
    """Assemble the Morning Report and push the digest to the alert channel."""
    try:
        from .report.morning import dispatch_morning_report
        report = await dispatch_morning_report()
        ideas = report.get("top_ideas")
        holdings = report.get("holdings")
        logger.info(
            "morning report dispatched: %d ideas, %d holdings",
            len(ideas) if isinstance(ideas, list) else 0,
            len(holdings) if isinstance(holdings, list) else 0,
        )
    except Exception as e:
        logger.exception("morning report dispatch failed: %s", e)


_last_pulse_state: Optional[tuple[str, str]] = None


async def _run_pulse_monitor_job() -> None:
    """15-min live monitor: refresh the pulse; alert only on STATE CHANGES
    (day type or semis-rotation direction). The entry loop reads the same
    cached pulse as its live tape gate, so this refresh is also what keeps
    the trading desk's view current."""
    global _last_pulse_state
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30):
            return  # pre-open quotes are stale prints — skip until the bell
        from .report.pulse import build_intraday_pulse
        p = await build_intraday_pulse(refresh=True)
        state = (p.get("day_type", "?"), p.get("semis_rotation", "?"))
        if _last_pulse_state is not None and state != _last_pulse_state:
            from .autotrade.alerts import alert
            await alert(
                level="warning" if state[0] == "distribution" or state[1] == "out_of_semis" else "info",
                title=f"Tape change — {state[0]} day, {state[1].replace('_', ' ')}",
                body=(f"was: {_last_pulse_state[0]} / {_last_pulse_state[1].replace('_', ' ')}. "
                      f"{p.get('breadth_up_pct', 0):.0f}% of universe up, "
                      f"avg {p.get('avg_change_pct', 0):+.1f}%. {p.get('semis_rotation_note', '')}"),
            )
        _last_pulse_state = state
        logger.info("pulse monitor: %s / %s (%.0f%% up, avg %+.1f%%)",
                    state[0], state[1], p.get("breadth_up_pct", 0),
                    p.get("avg_change_pct", 0))
    except Exception as e:
        logger.exception("pulse monitor tick failed: %s", e)


async def _run_pulse_dispatch_job() -> None:
    """10:30 ET: compute the intraday pulse and push the day-type call."""
    try:
        from .report.pulse import build_intraday_pulse
        p = await build_intraday_pulse(refresh=True)
        from .autotrade.alerts import alert
        await alert(
            level="info",
            title=(f"10:30 read — {p['day_type']} day "
                   f"({p['status_0_10']}/10)"),
            body=(f"{p['breadth_up_pct']:.0f}% of universe up, avg "
                  f"{p['avg_change_pct']:+.1f}%. "
                  + (f"Leaders: "
                     + ", ".join(t["theme"] for t in p.get("leaders", [])) + "."
                     if p.get("leaders") else "")),
        )
        logger.info("intraday pulse dispatched: %s (%s/10)",
                    p["day_type"], p["status_0_10"])
    except Exception as e:
        logger.exception("intraday pulse dispatch failed: %s", e)


async def _run_stake_watch_job() -> None:
    """Fire the subject-company 13D/13G stake watch on its 30-min tick.

    Idempotent (accession-keyed). Surfaces activist 13Ds, new/added passive
    stakes, and — the key signal — abrupt institutional reductions/exits on
    names we hold (flag-only, never auto-closes)."""
    try:
        from .hedge_funds.stake_watch import run_stake_watch
        result = await run_stake_watch()
        if result.get("skipped_reason"):
            logger.info("stake watch tick skipped: %s", result["skipped_reason"])
        else:
            logger.info(
                "stake watch tick: %d subjects, %d new 13D/G, %d reductions, %d activist",
                result.get("subjects_scanned", 0), result.get("new_filings", 0),
                result.get("reductions_flagged", 0), result.get("activist_flagged", 0),
            )
    except Exception as e:
        logger.exception("stake watch tick failed: %s", e)


# ---------------------------------------------------------------------------
# The job itself — runs every theme, idempotency-keyed by date
# ---------------------------------------------------------------------------


async def _run_all_themes_job() -> None:
    """Iterate themes and trigger one run per theme (sequential)."""
    started = datetime.now(timezone.utc)
    today = started.date().isoformat()
    settings = get_settings()
    use_mock = settings.USE_MOCK_RUN
    logger.info("scheduled run starting (mock=%s)", use_mock)

    # Snapshot the theme list once so we don't hold a session across
    # potentially long ThemeRunner.run() calls.
    async with db_session() as s:
        themes = list((await s.execute(select(Theme).order_by(Theme.created_at))).scalars().all())
        themes_snapshot = [
            (t.id, t.name, t.thesis, t.chokepoint or "",
             [sy.symbol for sy in sorted(t.symbols, key=lambda x: x.position)])
            for t in themes
        ]

    # Quant overlay: build TODAY's feature snapshot before scoring so the
    # scorecard reads same-day signals. Idempotent + best-effort — a failure
    # must not block signal generation.
    if get_settings().QUANT_OVERLAY_ENABLED:
        try:
            from .research.features import ensure_todays_snapshot
            snap = await ensure_todays_snapshot()
            logger.info("scheduled run: feature snapshot ready (%s)", snap.get("reason", "built"))
        except Exception as e:
            logger.warning("scheduled run: feature snapshot pre-build failed: %s", e)

    success = 0
    skipped = 0
    failed = 0

    for theme_id, name, thesis, chokepoint, symbols in themes_snapshot:
        if not symbols:
            logger.info("scheduled run: theme %s has no symbols, skipping", theme_id)
            skipped += 1
            continue

        idempotency_key = f"scheduled-{theme_id}-{today}"
        try:
            async with db_session() as s:
                repo = RunRepo(s)
                existing = await repo.find_by_idempotency_key(user_id=None, key=idempotency_key)
                if existing is not None and existing.status in ("running", "done"):
                    logger.info(
                        "scheduled run: theme %s already has run %s today (status=%s) — skipping",
                        theme_id, existing.id, existing.status,
                    )
                    skipped += 1
                    continue
                run = await repo.create(
                    theme_id=theme_id,
                    idempotency_key=idempotency_key,
                    config={"trigger": "scheduled", "scheduled_for": today},
                )
                run_id = run.id

            theme_dto = {
                "id": theme_id, "name": name, "thesis": thesis,
                "chokepoint": chokepoint, "symbols": symbols,
            }

            # Run *to completion* sequentially. This is the slow path —
            # 7-10 min per ticker for real DeepSeek. Could be parallelised
            # across themes once we trust rate limits.
            if use_mock:
                from .main import _simulate_run
                await _simulate_run(run_id, theme_dto)
            else:
                from .real_run import real_run
                await real_run(run_id, theme_dto)

            success += 1
            logger.info("scheduled run: theme %s completed (run %s)", theme_id, run_id)

        except Exception as e:
            failed += 1
            logger.exception("scheduled run: theme %s failed: %s", theme_id, e)

    finished = datetime.now(timezone.utc)
    status = "ok" if failed == 0 else ("partial" if success > 0 else "error")
    elapsed_min = (finished - started).total_seconds() / 60

    async with db_session() as s:
        state = await s.get(SystemState, 1)
        if state is not None:
            state.scheduler_last_run_at = finished
            state.scheduler_last_status = status

    logger.info(
        "scheduled run finished in %.1f min: %d ok, %d skipped, %d failed (status=%s)",
        elapsed_min, success, skipped, failed, status,
    )
    await _refresh_next_run_at()


async def _ensure_todays_run(reason: str) -> bool:
    """Fire the daily theme run if it's a weekday, at/after the 09:00 ET run
    hour, the scheduler is enabled, and no completed run exists today.

    The single recovery primitive shared by the boot catch-up, the missed-run
    watchdog, and the enable path. Idempotent — the daily job is date-keyed
    (``scheduled-{theme}-{today}``), so concurrent/duplicate calls no-op. Returns
    True iff it fired the run. Checks ``scheduler_enabled`` itself so it stays
    safe when called after the scheduler was enabled post-boot.
    """
    try:
        async with db_session() as s:
            state = await s.get(SystemState, 1)
        if not (state and state.scheduler_enabled):
            return False
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.weekday() >= 5:
            return False
        if now_et.hour < 9:
            # Before the scheduled hour — let the normal 09:00 cron handle it.
            return False
        start_today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        async with db_session() as s:
            done = (
                await s.execute(
                    select(Run.id).where(Run.status == "done")
                    .where(Run.finished_at >= start_today).limit(1)
                )
            ).first()
        if done:
            return False
        logger.warning(
            "run recovery (%s): no completed run today (missed 09:00 cron) — "
            "firing the daily theme run now so signals are fresh", reason)
        await _run_all_themes_job()
        return True
    except Exception as e:
        logger.exception("run recovery (%s) failed: %s", reason, e)
        return False


async def _startup_catchup_job() -> None:
    """One-shot on boot: recover today's run if missing (see _ensure_todays_run)."""
    await _ensure_todays_run("startup")


async def _run_missed_run_watchdog() -> None:
    """Recurring safety net (every 30 min, 09:00-15:30 ET): recover the daily
    run if it was missed — the gap that left 2026-06-29 with zero signals."""
    await _ensure_todays_run("watchdog")
