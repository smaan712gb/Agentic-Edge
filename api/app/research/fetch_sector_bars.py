"""Populate the local Polygon cache for the sector-dispersion IC study.

Separate entry point from the analysis so the slow part (network, ~30 min on
the free tier) runs once and every subsequent analysis re-run is instant and
offline. Safe to interrupt and re-run: completed (ticker, range) chunks are
cached and skipped.

    cd api && python -m app.research.fetch_sector_bars --years 2
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from .polygon_bars import (
    PolygonClient,
    RateLimiter,
    chunk_ranges,
    load_api_key,
)

logger = logging.getLogger(__name__)

# 11 GICS sector SPDRs + SMH. SPY is the market leg (hedge instrument and beta
# denominator) — it is fetched but never ranked as a "sector".
SECTORS: tuple[str, ...] = (
    "XLK", "XLC", "XLY", "XLF", "XLI",
    "XLE", "XLV", "XLB", "XLP", "XLU", "XLRE", "SMH",
)
MARKET = "SPY"
UNIVERSE: tuple[str, ...] = SECTORS + (MARKET,)

DEFAULT_CACHE = Path(__file__).resolve().parents[3] / ".cache" / "polygon"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (default: yesterday)")
    args = ap.parse_args(argv)

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = end - timedelta(days=int(365 * args.years))

    client = PolygonClient(
        api_key=load_api_key(),
        cache_dir=Path(args.cache),
        limiter=RateLimiter(),
    )

    chunks = chunk_ranges(start, end, days=90)
    total = len(UNIVERSE) * (1 + len(chunks) + 1)  # daily + minute chunks + divs
    logger.info(
        "universe=%d tickers  window=%s..%s  chunks/ticker=%d  ~%d units",
        len(UNIVERSE), start, end, len(chunks), total,
    )

    t0 = time.time()
    done = 0
    for ticker in UNIVERSE:
        client.daily_bars(ticker, start, end)
        done += 1
        client.dividends(ticker)
        done += 1
        for c_start, c_end in chunks:
            bars = client.minute_bars(ticker, c_start, c_end)
            done += 1
            logger.info(
                "%-5s %s..%s  bars=%6d  [%3d/%3d]  calls=%d hits=%d  %.1f min elapsed",
                ticker, c_start, c_end, len(bars), done, total,
                client.calls_made, client.cache_hits, (time.time() - t0) / 60,
            )

    logger.info(
        "DONE in %.1f min — %d API calls, %d cache hits, cache=%s",
        (time.time() - t0) / 60, client.calls_made, client.cache_hits, args.cache,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
