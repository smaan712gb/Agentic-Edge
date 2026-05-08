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
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .config import get_settings
from .db import SystemState, Theme, get_session as db_session
from .repos import RunRepo, ThemeRepo

logger = logging.getLogger("agentic_edge.scheduler")


_SCHEDULER: Optional[AsyncIOScheduler] = None
_JOB_ID = "theme_daily_run"


# ---------------------------------------------------------------------------
# Public API — start / stop / fire
# ---------------------------------------------------------------------------


async def start_scheduler() -> None:
    """Bring up the in-process scheduler. Idempotent."""
    global _SCHEDULER
    if _SCHEDULER is not None:
        return

    state = await _read_state_or_seed()

    _SCHEDULER = AsyncIOScheduler(timezone="America/New_York")
    if state.scheduler_enabled:
        _add_or_update_job(state.scheduler_cron)
    _SCHEDULER.start()
    logger.info(
        "scheduler started (enabled=%s, cron=%s)",
        state.scheduler_enabled, state.scheduler_cron,
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
