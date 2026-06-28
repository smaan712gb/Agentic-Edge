"""Event-study engine — cumulative abnormal return (CAR) around catalysts.

Semiconductors are catalyst-driven, so the highest-alpha question is: when event
type E fires on a name, what does the name do next, in excess of the universe?
This harness answers it from events ALREADY in the DB — no new ingestion:

  * stake filings (13D/13G)       — StakeFiling.filed_at  (new / increase / exit)
  * chokepoint news               — NewsMention.published_at (bullish / bearish)
  * 13F manager position changes  — PositionChange.current_period (new/add/exit)

For each event we measure the symbol's forward return over a window and subtract
the equal-weight universe return over the same window — the *abnormal* part.
Aggregated per event type at horizons {5, 20, 60}d, the mean CAR and its decay
tell you which catalysts are tradeable and for how long.

Offline + read-only. The CAR math is pure (``market_adjusted_car``,
``aggregate_cars``) and unit-tested. Research-only — measures, never trades.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .feature_research import resolve_db_path
from .labeler import forward_return

logger = logging.getLogger(__name__)

WINDOWS: tuple[int, ...] = (5, 20, 60)
MIN_EVENTS = 8   # below this, the per-type CAR is a smoke test, not a finding.


# ---------------------------------------------------------------------------
# Pure core (unit-tested)
# ---------------------------------------------------------------------------


def market_adjusted_car(
    sym_series: list[tuple[date, float]],
    bench_series: list[tuple[date, float]],
    event_date: date, window_days: int,
) -> Optional[float]:
    """Symbol forward return minus benchmark forward return over the window.

    Returns None if either leg can't be measured to a complete window — we only
    count events whose abnormal return is fully realised, never a partial one.
    """
    sym_ret, sym_ok = forward_return(sym_series, event_date, window_days)
    bench_ret, bench_ok = forward_return(bench_series, event_date, window_days)
    if sym_ret is None or bench_ret is None or not (sym_ok and bench_ok):
        return None
    return round(sym_ret - bench_ret, 5)


def aggregate_cars(cars: list[float]) -> dict[str, float]:
    """Summary stats for a list of CARs: n, mean, median, win-rate, stdev."""
    if not cars:
        return {"n": 0, "mean_car": float("nan"), "median_car": float("nan"),
                "win_rate": float("nan"), "stdev": float("nan")}
    wins = sum(1 for c in cars if c > 0)
    return {
        "n": len(cars),
        "mean_car": round(statistics.fmean(cars), 5),
        "median_car": round(statistics.median(cars), 5),
        "win_rate": round(wins / len(cars), 4),
        "stdev": round(statistics.pstdev(cars), 5) if len(cars) > 1 else 0.0,
    }


def build_equal_weight_index(series_by_symbol: dict[str, list[tuple[date, float]]]
                             ) -> list[tuple[date, float]]:
    """Equal-weight price index (rebased to 100) from per-symbol close series.

    Each symbol is normalised to its own first observation, then averaged across
    symbols present on each date — a simple, transparent universe benchmark.
    """
    norm: dict[date, list[float]] = {}
    for series in series_by_symbol.values():
        if not series:
            continue
        ordered = sorted(series, key=lambda x: x[0])
        base = ordered[0][1]
        if base <= 0:
            continue
        for d, px in ordered:
            norm.setdefault(d, []).append(px / base)
    return [(d, 100.0 * statistics.fmean(v)) for d, v in sorted(norm.items()) if v]


# ---------------------------------------------------------------------------
# Event loading
# ---------------------------------------------------------------------------


def load_events(conn: sqlite3.Connection) -> list[dict]:
    """Pull catalyst events from the existing tables into a flat list."""
    events: list[dict] = []

    def _q(sql: str) -> list:
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.OperationalError:
            return []   # table may not exist on an older DB

    for tkr, filed, ctype, activist in _q(
        "SELECT subject_ticker, filed_at, change_type, is_activist FROM stake_filings "
        "WHERE subject_ticker IS NOT NULL AND filed_at IS NOT NULL"
    ):
        kind = "stake_13d" if activist else "stake_13g"
        events.append({"event_type": f"{kind}_{ctype or 'initial'}", "symbol": tkr, "date": filed})

    for tkr, pub, sent in _q(
        "SELECT ticker, COALESCE(published_at, captured_at), sentiment FROM news_mentions "
        "WHERE ticker IS NOT NULL"
    ):
        if sent in ("bullish", "bearish"):
            events.append({"event_type": f"news_{sent}", "symbol": tkr, "date": pub})

    for tkr, period, ctype in _q(
        "SELECT ticker, current_period, change_type FROM position_changes "
        "WHERE ticker IS NOT NULL AND current_period IS NOT NULL AND change_type IN ('new','add','exit')"
    ):
        events.append({"event_type": f"mgr_{ctype}", "symbol": tkr, "date": period})

    # Normalise dates to date objects; drop unparseable.
    import pandas as pd
    out: list[dict] = []
    for e in events:
        try:
            d = pd.to_datetime(e["date"], utc=True).date()
        except Exception:
            continue
        out.append({**e, "symbol": (e["symbol"] or "").upper(), "date": d})
    return [e for e in out if e["symbol"]]


async def _price_series_for(symbols: set[str], lookback_days: int
                            ) -> dict[str, list[tuple[date, float]]]:
    from .labeler import _price_series
    out: dict[str, list[tuple[date, float]]] = {}
    sem = asyncio.Semaphore(6)

    async def _one(sym: str) -> None:
        async with sem:
            out[sym] = await _price_series(sym, lookback_days)

    await asyncio.gather(*[_one(s) for s in symbols])
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class EventStudyReport:
    n_events: int
    n_symbols: int
    windows: list[int]
    by_event_type: dict          # {event_type: {window: {n, mean_car, ...}}}
    events: list[dict]           # event_type, count
    warnings: list[str] = field(default_factory=list)


async def run_event_study(db_path: Optional[str] = None, lookback_days: int = 400
                          ) -> EventStudyReport:
    path = resolve_db_path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    conn = sqlite3.connect(str(path))
    try:
        events = load_events(conn)
    finally:
        conn.close()

    warnings: list[str] = []
    if not events:
        return EventStudyReport(0, 0, list(WINDOWS), {}, [],
                                ["no catalyst events in the DB yet (stake filings / "
                                 "news / 13F changes) — populate them via the sweeps"])

    symbols = {e["symbol"] for e in events}
    series = await _price_series_for(symbols, lookback_days)
    bench = build_equal_weight_index(series)

    # event_type -> window -> [CARs]
    cars: dict[str, dict[int, list[float]]] = {}
    for e in events:
        sym_series = series.get(e["symbol"])
        if not sym_series:
            continue
        for w in WINDOWS:
            car = market_adjusted_car(sym_series, bench, e["date"], w)
            if car is not None:
                cars.setdefault(e["event_type"], {}).setdefault(w, []).append(car)

    by_type: dict[str, dict] = {}
    for etype, by_w in cars.items():
        by_type[etype] = {str(w): aggregate_cars(by_w.get(w, [])) for w in WINDOWS}

    counts: dict[str, int] = {}
    for e in events:
        counts[e["event_type"]] = counts.get(e["event_type"], 0) + 1
    events_summary = [{"event_type": k, "count": v}
                      for k, v in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)]

    max_n = max((agg["n"] for t in by_type.values() for agg in t.values()), default=0)
    if max_n < MIN_EVENTS:
        warnings.append(
            f"LOW EVENT COUNT: at most {max_n} measurable events for any type "
            f"(need >={MIN_EVENTS}). CARs here are a SMOKE TEST of the machinery, "
            f"not a tradeable edge. Sharpens as the filing/news sweeps accumulate."
        )

    return EventStudyReport(
        n_events=len(events), n_symbols=len(symbols), windows=list(WINDOWS),
        by_event_type=by_type, events=events_summary, warnings=warnings,
    )


def print_report(rep: EventStudyReport) -> None:
    line = "=" * 78
    print(line)
    print("EVENT STUDY  —  market-adjusted CAR around catalysts")
    print(line)
    print(f"events / symbols   : {rep.n_events} events over {rep.n_symbols} symbols")
    print()
    if rep.warnings:
        print("WARNINGS")
        for w in rep.warnings:
            print(f"  ! {w}")
        print()
    print(f"  {'event_type':24s} {'n@20d':>6s} {'CAR@5d':>8s} {'CAR@20d':>8s} {'CAR@60d':>8s} {'win@20d':>8s}")
    for etype in sorted(rep.by_event_type):
        by_w = rep.by_event_type[etype]
        def g(w, k):
            v = by_w.get(str(w), {}).get(k, float("nan"))
            return v
        n20 = g(20, "n")
        print(f"  {etype:24s} {int(n20) if n20 == n20 else 0:>6d} "
              f"{_f(g(5,'mean_car')):>8s} {_f(g(20,'mean_car')):>8s} {_f(g(60,'mean_car')):>8s} "
              f"{_f(g(20,'win_rate')):>8s}")
    print()
    print("CAR>0 = the catalyst beat the universe over the window. PROMOTION GATE:")
    print("research only — no event signal moves to a gate until n is credible.")
    print(line)


def _f(x: float) -> str:
    return "   n/a" if x != x else f"{x:+.4f}"


def main(argv: Optional[Iterable[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    try:  # pragma: no cover
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    rep = asyncio.run(run_event_study(args.db))
    if args.json:
        from dataclasses import asdict
        print(json.dumps(asdict(rep), default=str, indent=2))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
