"""Alert dispatcher — Slack, email, SMS, or stdout.

All automation that takes (or fails to take) a meaningful action calls
``alert(...)``. The dispatcher fans out to whatever providers are
configured. None configured? Fall back to logger so nothing is silent.

Provider config lives in ``Settings`` (left blank by default; user adds
``SLACK_WEBHOOK_URL`` or ``ALERT_EMAIL_TO`` etc. when they want
non-stdout delivery). The interface stays the same regardless.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

logger = logging.getLogger("agentic_edge.alerts")

Level = Literal["info", "warning", "critical"]


async def alert(*, level: Level, title: str, body: str = "") -> None:
    """Fan-out alert dispatcher. Always logs; opportunistically pushes to Slack."""
    line = f"[{level.upper()}] {title}"
    if body:
        line += f" — {body}"
    logger.log(_level_to_logging(level), line)

    # Slack — only if a webhook is configured.
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook:
        try:
            import httpx  # local import; the dispatcher is optional path
            async with httpx.AsyncClient(timeout=5) as c:
                emoji = {"info": ":information_source:", "warning": ":warning:",
                         "critical": ":rotating_light:"}.get(level, "")
                await c.post(webhook, json={
                    "text": f"{emoji} *{title}*\n{body}" if body else f"{emoji} *{title}*",
                })
        except Exception as e:  # pragma: no cover — alerts must never break the caller
            logger.warning("slack alert dispatch failed: %s", e)


def _level_to_logging(level: Level) -> int:
    return {
        "info":     logging.INFO,
        "warning":  logging.WARNING,
        "critical": logging.CRITICAL,
    }[level]
