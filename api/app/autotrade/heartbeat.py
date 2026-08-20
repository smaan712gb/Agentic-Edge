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

# Forced reconnects that completed without restoring market data. Reset the
# moment data recovers, so each incident gets a fresh budget. Three is enough
# to distinguish a transient farm hiccup (recovers on the first or second)
# from a competing session (never recovers, because the cause is a login
# somewhere else entirely).
_FAILED_RECOVERIES: int = 0
_MAX_FAILED_RECOVERIES = 3


def is_connected_snapshot() -> bool:
    """Cached connection state (refreshed by the heartbeat task)."""
    return _CONNECTED


async def start_heartbeat() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop_forever(), name="ibkr_heartbeat")
    from .supervisor import supervise
    supervise(_TASK, start_heartbeat, "ibkr_heartbeat")
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
            # Active market-data recovery: the socket can be UP while IBKR
            # refuses market data (error 10197, competing live session). The
            # disconnect path never fires for that, so trigger a (cooldown-
            # guarded) reconnect to re-acquire the data farm. force_reconnect()
            # self-limits to once per ~5 min, so this can't thrash.
            if connected and getattr(prov, "market_data_unhealthy", None) and prov.market_data_unhealthy():
                # Log at debug, not warning. force_reconnect() is cooldown-guarded
                # and also declines while orders are working, so most ticks here
                # do nothing — but this line used to announce "forcing reconnect"
                # every 30s regardless. On 2026-08-20 that produced 130 warnings
                # for 15 actual reconnects, which reads as a thrashing loop and
                # sent the diagnosis in the wrong direction. The provider logs
                # its own line when a reconnect really happens.
                # Give up rather than reconnect forever. 10197 means the IBKR
                # *username* has another live session (mobile app, Client Portal,
                # TradingView — paper and live share data entitlements), which no
                # amount of reconnecting from this process can clear. Each attempt
                # still tears down and re-subscribes every position contract, so
                # unbounded retry is a recurring self-inflicted outage dressed up
                # as a fix. Escalate once with the actual remedy, then stop.
                global _FAILED_RECOVERIES
                if _FAILED_RECOVERIES < _MAX_FAILED_RECOVERIES:
                    logger.debug("ibkr heartbeat: market data unhealthy — asking provider "
                                 "to reconnect (may be declined by cooldown/open orders)")
                    try:
                        did = await prov.force_reconnect("heartbeat: market-data unhealthy")
                    except Exception as e:
                        logger.warning("ibkr heartbeat: force_reconnect errored: %s", e)
                        did = False
                    if did:
                        _FAILED_RECOVERIES += 1
                        if _FAILED_RECOVERIES >= _MAX_FAILED_RECOVERIES:
                            logger.error(
                                "ibkr: market data still refused after %d forced reconnects — "
                                "this is NOT a connection fault. Error 10197 means another live "
                                "session holds the market-data entitlement for this IBKR user. "
                                "Look for a logged-in mobile app, Client Portal (web) session, "
                                "or TradingView broker link on the SAME account and close it. "
                                "Suppressing further reconnects; quotes fall back to the "
                                "secondary chain.", _FAILED_RECOVERIES)
            elif connected and _FAILED_RECOVERIES:
                # Data recovered — re-arm so the next genuine incident is treated
                # as new rather than inheriting a spent budget.
                logger.info("ibkr: market data healthy again after %d failed recoveries",
                            _FAILED_RECOVERIES)
                _FAILED_RECOVERIES = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if _CONNECTED:
                logger.warning("ibkr heartbeat: ping failed (%s); will retry", e)
            _CONNECTED = False
        await asyncio.sleep(_PING_INTERVAL_SEC)
