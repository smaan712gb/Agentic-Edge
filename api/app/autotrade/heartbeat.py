"""IBKR connection heartbeat.

Background task that pings the IBKR provider every 30 seconds. Two
purposes:

  1. Surface connection state quickly — `/api/health` reads
     ``is_connected_snapshot()`` so the UI can render IBKR status without
     issuing an IBKR call on every refresh.
  2. Force an early reconnect when the socket dropped while idle.
     Without the heartbeat, a provider would only notice on the next
     trade attempt; with it, reconnection happens within ~30s of the
     drop and the next entry-loop tick sees a healthy connection.

Reconnect logic itself lives in ``IbkrProvider._ensure_connected`` —
this task just keeps tickling it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("agentic_edge.heartbeat")


_TASK: Optional[asyncio.Task] = None
_PING_INTERVAL_SEC = 30.0
_CONNECTED: bool = False


def is_connected_snapshot() -> bool:
    """Cached connection state (refreshed by the heartbeat task)."""
    return _CONNECTED


async def start_heartbeat() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop_forever(), name="ibkr_heartbeat")
    logger.info("ibkr heartbeat started (every %.0fs)", _PING_INTERVAL_SEC)


async def stop_heartbeat() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None


async def _loop_forever() -> None:
    global _CONNECTED
    # Brief startup delay so the FastAPI lifespan finishes other init first.
    await asyncio.sleep(2.0)
    while True:
        try:
            from api.app.positions import _ibkr
            prov = await _ibkr()
            connected = bool(prov and prov.is_connected())
            if connected != _CONNECTED:
                logger.info("ibkr heartbeat: connection state -> %s",
                            "connected" if connected else "disconnected")
            _CONNECTED = connected
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if _CONNECTED:
                logger.warning("ibkr heartbeat: ping failed (%s); will retry", e)
            _CONNECTED = False
        await asyncio.sleep(_PING_INTERVAL_SEC)
