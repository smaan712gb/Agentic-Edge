"""Background-loop supervisor.

A trading loop task that dies ENTIRELY (an exception escaping the per-tick
guard, a GC'd task, a bug in the except handler) is the highest-consequence
silent failure: exits stop firing, no entries, and ``/api/health`` still shows
IBKR green because the heartbeat runs on the same-or-different task. The per-tick
``asyncio.wait_for`` timeout only guards a hang INSIDE a tick — it does nothing
if ``_loop_forever`` itself unwinds.

``supervise`` attaches a done-callback that, when a loop ends UNEXPECTEDLY
(i.e. not via a clean cancel on shutdown), logs CRITICAL, fires an operator
alert, and respawns the loop via its idempotent ``start_*`` coroutine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("agentic_edge.supervisor")


def supervise(task: asyncio.Task, restart: Callable[[], Awaitable[None]], name: str) -> None:
    """Attach a restart-on-death callback to a background loop task."""

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return  # clean shutdown — do not restart
        exc = None
        try:
            exc = t.exception()
        except Exception:  # noqa: BLE001
            pass
        logger.critical("loop '%s' exited unexpectedly (exc=%r) — restarting", name, exc)
        try:
            asyncio.ensure_future(
                _alert_and_restart(restart, name, repr(exc))
            )
        except Exception as e:  # noqa: BLE001
            logger.error("supervisor: could not schedule restart of '%s': %s", name, e)

    task.add_done_callback(_on_done)


async def _alert_and_restart(restart: Callable[[], Awaitable[None]], name: str, exc: str) -> None:
    try:
        from .alerts import alert
        await alert(level="critical", title=f"Trading loop died: {name}",
                    body=f"{exc} — auto-restarting. Verify positions are being managed.")
    except Exception:  # noqa: BLE001 — never let alerting block the restart
        pass
    try:
        await restart()
        # Re-arm supervision on the freshly-spawned task.
        _resupervise(restart, name)
        logger.info("supervisor: loop '%s' restarted", name)
    except Exception as e:  # noqa: BLE001
        logger.error("supervisor: restart of '%s' FAILED: %s", name, e)


def _resupervise(restart: Callable[[], Awaitable[None]], name: str) -> None:
    """Re-attach the supervisor to whichever task the module now holds."""
    try:
        import importlib
        mod = {
            "entry_loop": "api.app.autotrade.entry_loop",
            "maint_loop": "api.app.autotrade.maint_loop",
            "ibkr_heartbeat": "api.app.autotrade.heartbeat",
        }.get(name)
        if not mod:
            return
        m = importlib.import_module(mod)
        t = getattr(m, "_TASK", None)
        if isinstance(t, asyncio.Task) and not t.done():
            supervise(t, restart, name)
    except Exception:  # noqa: BLE001
        pass
