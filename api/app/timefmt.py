"""One place that decides how a timestamp leaves this API.

Every datetime this system persists is UTC, but SQLite hands them back
*naive* — no tzinfo. ``datetime.isoformat()`` on a naive value emits no
offset, and ECMAScript parses an offset-less date-time string as LOCAL
time. So ``new Date("2026-08-19T13:01:15")`` in a browser at UTC-4 became
9:01 AM's run displayed as "1:01 PM": the daily 09:00 ET theme batch read
four hours late on the Runs tab, and every rotation ``computed_at`` was
off by the same amount.

Fixing it at the serialisation boundary is the right altitude — the
alternative is every client knowing that this API's naive timestamps
happen to mean UTC, which is precisely the convention nothing should
have to know.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """ISO-8601 with an explicit UTC offset, or None.

    A naive value is *assumed* UTC, which is this system's storage
    convention; an aware value is converted rather than relabelled.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
