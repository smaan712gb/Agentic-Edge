"""Rate-limited, on-disk-cached Polygon bar fetcher for offline research.

Sits under ``app.research`` with the other harnesses: strictly read-only with
respect to the trading system, and it never touches the broker. The only side
effect is a JSON cache under ``--cache`` so a re-run costs zero API calls.

Two things this module exists to survive:

  * **TLS interception.** Norton (and corporate proxies) MITM outbound TLS, and
    the certifi bundle rejects the proxy CA — the same footgun ``app.main``
    solves with ``truststore``. We do it here too, because research scripts run
    outside the FastAPI process.
  * **The 5-calls/minute free tier.** Every request goes through a token-bucket
    pacer with 429 backoff, and every response is cached by (ticker, range), so
    an interrupted pull resumes instead of restarting.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - environment dependent
    import truststore as _truststore

    _truststore.inject_into_ssl()
except Exception as _e:  # pragma: no cover - falls back to certifi
    logging.getLogger(__name__).warning("truststore inject failed: %s", _e)

logger = logging.getLogger(__name__)

_BASE = "https://api.polygon.io"
# Free tier is nominally 5 requests/minute (12s), but measured against the live
# endpoint 13s spacing still draws 429s — the window is enforced tighter than
# documented, and each rejection costs a 20s+ backoff, so pacing too fast is
# slower end-to-end than pacing conservatively. 15s measured clean.
_MIN_INTERVAL_S = 15.0
_MAX_RETRIES = 5


class RateLimiter:
    """Single-process token bucket: never issue calls closer than `interval`."""

    def __init__(self, interval: float = _MIN_INTERVAL_S) -> None:
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last = time.monotonic()


@dataclass
class PolygonClient:
    api_key: str
    cache_dir: Path
    limiter: RateLimiter
    calls_made: int = 0
    cache_hits: int = 0

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- internals
    def _get(self, url: str) -> dict[str, Any]:
        """One paced GET with exponential backoff on 429/5xx."""
        for attempt in range(_MAX_RETRIES):
            self.limiter.wait()
            try:
                with urllib.request.urlopen(url, timeout=90) as resp:
                    self.calls_made += 1
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    backoff = 20.0 * (attempt + 1)
                    logger.warning("HTTP %s — backing off %.0fs", exc.code, backoff)
                    time.sleep(backoff)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as exc:
                backoff = 10.0 * (attempt + 1)
                logger.warning("network error %s — retry in %.0fs", exc, backoff)
                time.sleep(backoff)
        raise RuntimeError(f"exhausted {_MAX_RETRIES} retries: {url}")

    def _cached(self, key: str, build_url) -> list[dict]:
        """Fetch-with-cache. `build_url` is deferred so cache hits cost nothing.

        Follows Polygon's ``next_url`` pagination — a range wide enough to
        exceed the 50k row cap (SPY trades nearly every extended-hours minute)
        returns partial results with a continuation link, and silently
        truncating there would punch invisible holes in the panel.
        """
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            self.cache_hits += 1
            return json.loads(path.read_text())

        url = build_url()
        results: list[dict] = []
        pages = 0
        while url:
            payload = self._get(url)
            results.extend(payload.get("results") or [])
            pages += 1
            nxt = payload.get("next_url")
            url = f"{nxt}&apiKey={self.api_key}" if nxt else None
            if pages > 20:  # pragma: no cover - defensive
                logger.warning("pagination cap hit for %s", key)
                break

        path.write_text(json.dumps(results))
        return results

    # ------------------------------------------------------------------- public
    def daily_bars(self, ticker: str, start: date, end: date) -> list[dict]:
        key = f"{ticker}_daily_{start}_{end}"
        return self._cached(
            key,
            lambda: f"{_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
            f"?adjusted=true&sort=asc&limit=50000&apiKey={self.api_key}",
        )

    def minute_bars(self, ticker: str, start: date, end: date) -> list[dict]:
        key = f"{ticker}_min_{start}_{end}"
        return self._cached(
            key,
            lambda: f"{_BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}"
            f"?adjusted=true&sort=asc&limit=50000&apiKey={self.api_key}",
        )

    def dividends(self, ticker: str) -> list[dict]:
        """Ex-dividend dates. Needed to drop contaminated overnight returns:
        Polygon adjusts for splits but NOT dividends, so an ex-div morning shows
        a spurious gap down of the distribution size — which is exactly the
        signal we are trying to measure."""
        key = f"{ticker}_divs"
        return self._cached(
            key,
            lambda: f"{_BASE}/v3/reference/dividends?ticker={ticker}"
            f"&limit=1000&apiKey={self.api_key}",
        )


def chunk_ranges(start: date, end: date, days: int = 90) -> list[tuple[date, date]]:
    """Split [start, end] into <=`days` calendar-day windows.

    90 calendar days ~= 62 trading days. A sector ETF prints ~450 extended-hours
    minute bars/day (~28k rows, comfortably under the 50k cap); SPY prints
    closer to 960 and spills to a second page, which `_cached` handles.
    """
    out: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out


def load_api_key(env_path: Optional[Path] = None) -> str:
    """Read POLYGON_API_KEY from the project .env (authoritative per config.py)."""
    path = env_path or Path(__file__).resolve().parents[3] / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("POLYGON_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    return key
    import os

    key = os.environ.get("POLYGON_API_KEY", "")
    if not key:
        raise RuntimeError("POLYGON_API_KEY not found in .env or environment")
    return key
