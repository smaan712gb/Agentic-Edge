"""The AI-infrastructure portfolio index — one instrument for the whole complex.

Every portfolio-level decision in this design is expressed against "the AI
basket", not against a ticker: the basket enters a support zone, the basket is
extended 2.5 weekly ATR, the basket closes above the reversal week's high. None
of that is computable while the system only knows about 72 separate symbols.

This module builds that instrument.

WHY EQUAL-WEIGHTED IS THE PRIMARY SERIES
A cap-weighted AI index is largely NVDA. The question being asked is whether
the COMPLEX is extended or washed out, so every constituent gets one vote and
the index moves when participation moves. The cap-weighted series is built
alongside it precisely so the two can be compared — a cap-weighted index making
new highs while the equal-weighted one does not is the breadth divergence that
matters most, and it is invisible in either series alone.

WHY WEEKLY
Daily bars on a high-beta complex produce a signal on every ordinary pullback.
2026-08-18 is the case in point: a -4.8% session that was breathing after a
five-day run, on which the daily-driven tape gate halted all buying. The weekly
close is what filters positioning noise from actual trend change, so zones,
ATR distance and reversal confirmation are all computed on weekly bars.

WHY CONFLUENCE RATHER THAN A MOVING AVERAGE
No single level is meaningful. A zone matters when several INDEPENDENT kinds of
level land in the same place — a weekly moving average, a prior swing pivot, an
anchored VWAP, a prior range edge, a retracement of the last leg. Confluence is
the count of distinct level TYPES clustering within a volatility-scaled band,
which is why the band is measured in ATR rather than percent: the same 3% is a
tight cluster on a quiet index and noise on a violent one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("agentic_edge.portfolio")

# A level "counts" toward confluence when it sits within this many weekly ATR
# of the price being tested. Volatility-scaled on purpose — a fixed percentage
# is a tight cluster on a calm index and meaningless on a violent one.
_CONFLUENCE_BAND_ATR = 0.5

# Minimum daily bars for a symbol to enter the index. Low on purpose: chained
# returns admit a new listing from its second bar, and every long-lookback
# measure (20/50-week averages, breadth) excludes it on its own until it has
# the history. Setting this high instead silently drops real holdings.
_MIN_BARS_FOR_CONSTITUENT = 15


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------


@dataclass
class Bar:
    d: date
    o: float
    h: float
    l: float
    c: float
    v: float


def to_weekly(daily: list[Bar]) -> list[Bar]:
    """Resample daily bars to weekly (ISO week, Monday-anchored). Pure.

    Open is the week's first open, close its last close, high/low the extremes,
    volume the sum. The final week is included even when incomplete — the
    running week is exactly what a live decision has to be made against — so
    callers that need a CONFIRMED weekly close must drop the last element.
    """
    if not daily:
        return []
    out: list[Bar] = []
    cur: list[Bar] = []
    cur_key: Optional[tuple[int, int]] = None
    for b in sorted(daily, key=lambda x: x.d):
        key = b.d.isocalendar()[:2]      # (iso_year, iso_week)
        if cur_key is None or key == cur_key:
            cur.append(b)
            cur_key = key
            continue
        out.append(_collapse(cur))
        cur, cur_key = [b], key
    if cur:
        out.append(_collapse(cur))
    return out


def _collapse(week: list[Bar]) -> Bar:
    return Bar(
        d=week[-1].d, o=week[0].o,
        h=max(x.h for x in week), l=min(x.l for x in week),
        c=week[-1].c, v=sum(x.v for x in week),
    )


def atr(bars: list[Bar], period: int = 14) -> Optional[float]:
    """Wilder ATR. Pure. None when there is too little history."""
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].h, bars[i].l, bars[i - 1].c
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Wilder smoothing seeded with the simple mean of the first `period` TRs.
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def sma(bars: list[Bar], period: int) -> Optional[float]:
    if len(bars) < period:
        return None
    return sum(b.c for b in bars[-period:]) / period


def ema(bars: list[Bar], period: int) -> Optional[float]:
    if len(bars) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(b.c for b in bars[:period]) / period
    for b in bars[period:]:
        e = b.c * k + e * (1 - k)
    return e


def anchored_vwap(bars: list[Bar], anchor_idx: int) -> Optional[float]:
    """Volume-weighted average price from ``anchor_idx`` forward.

    Anchored at a structural point (a major low or high), this approximates the
    average price paid by everyone who has transacted since that event — which
    is why it behaves as support or resistance rather than as a statistic.
    """
    seg = bars[anchor_idx:]
    if not seg:
        return None
    num = sum(((b.h + b.l + b.c) / 3.0) * b.v for b in seg)
    den = sum(b.v for b in seg)
    return (num / den) if den > 0 else None


def swing_pivots(bars: list[Bar], left: int = 2, right: int = 2) -> tuple[list[float], list[float]]:
    """(swing_highs, swing_lows) — fractal pivots. Pure.

    A pivot high is a bar whose high exceeds ``left`` bars before and ``right``
    after. These are where price actually turned, which is what makes them
    reference levels rather than derived ones.
    """
    highs: list[float] = []
    lows: list[float] = []
    for i in range(left, len(bars) - right):
        w = bars[i - left: i + right + 1]
        if bars[i].h >= max(b.h for b in w):
            highs.append(bars[i].h)
        if bars[i].l <= min(b.l for b in w):
            lows.append(bars[i].l)
    return highs, lows


def fib_levels(swing_low: float, swing_high: float) -> dict[str, float]:
    """Retracements of the last major leg."""
    rng = swing_high - swing_low
    if rng <= 0:
        return {}
    return {
        "fib_382": swing_high - 0.382 * rng,
        "fib_500": swing_high - 0.500 * rng,
        "fib_618": swing_high - 0.618 * rng,
    }


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------


def confluence_at(
    price: float, levels: dict[str, float], band: float,
) -> tuple[int, list[str]]:
    """How many DISTINCT level types cluster within ``band`` of ``price``. Pure.

    Counting types rather than levels is deliberate: three moving averages
    stacked together are one kind of evidence, not three. ``band`` is supplied
    in price units by the caller, normally a fraction of weekly ATR, so the
    test scales with the index's own volatility.
    """
    if band <= 0:
        return 0, []
    hits = [name for name, lvl in levels.items()
            if lvl is not None and abs(price - float(lvl)) <= band]
    # Collapse to the level FAMILY: ma_*, pivot_*, vwap_*, fib_*, range_*.
    families = {h.split("_")[0] for h in hits}
    return len(families), sorted(hits)


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------


@dataclass
class IndexState:
    """Everything a portfolio-level gate needs, in one object."""
    as_of: date
    n_constituents: int
    equal_weighted: list[Bar] = field(default_factory=list)
    cap_weighted: list[Bar] = field(default_factory=list)
    weekly: list[Bar] = field(default_factory=list)
    weekly_atr: Optional[float] = None
    close: Optional[float] = None
    levels: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Participation: what fraction of constituents are above their own weekly
    # averages. The index can rise on a handful of names; this is what says
    # whether the COMPLEX is advancing.
    breadth_above_w10: Optional[float] = None
    breadth_above_w20: Optional[float] = None
    breadth_above_w50: Optional[float] = None
    breadth_history: list[float] = field(default_factory=list)   # weekly % >w20
    # Cap-weighted twin. A cap-weighted index at new highs while the
    # equal-weighted one is not IS the breadth divergence, and it is invisible
    # in either series alone.
    cap_weighted_weekly: list[Bar] = field(default_factory=list)
    # Benchmark for relative strength (QQQ), same weekly grid.
    benchmark_weekly: list[Bar] = field(default_factory=list)

    def extension_atr(self, anchor: Optional[float] = None) -> Optional[float]:
        """Distance from ``anchor`` (default weekly 20-SMA) in weekly ATR.

        This is the volatility-adjusted extension the trim gate keys off. "The
        basket rose 15%" is not a signal; "the basket is 2.5 ATR above its own
        mean" is, because it is scaled by how far this index normally travels.
        """
        if self.close is None or not self.weekly_atr:
            return None
        ref = anchor if anchor is not None else self.levels.get("ma_w20")
        if ref is None:
            return None
        return (self.close - float(ref)) / self.weekly_atr

    def correction_atr(self) -> Optional[float]:
        """How far the index has fallen from its recent weekly high, in ATR.

        The accumulation gate requires >= 1.5 — a real dislocation rather than
        an ordinary down week.
        """
        if self.close is None or not self.weekly_atr or len(self.weekly) < 8:
            return None
        recent_high = max(b.h for b in self.weekly[-13:])
        return (recent_high - self.close) / self.weekly_atr

    def rs_vs_benchmark(self, weeks: int = 13) -> Optional[float]:
        """Index return minus benchmark return over ``weeks``. Pure read.

        Positive means the complex is being bought relative to the tape, which
        is the distinction between "everything is down" and "this is being left
        behind" — the second is rotation, the first is beta.
        """
        if len(self.weekly) <= weeks or len(self.benchmark_weekly) <= weeks:
            return None
        def _ret(bars: list[Bar]) -> Optional[float]:
            a, b = bars[-weeks - 1].c, bars[-1].c
            return (b / a - 1.0) if a else None
        mine, theirs = _ret(self.weekly), _ret(self.benchmark_weekly)
        if mine is None or theirs is None:
            return None
        return mine - theirs

    def structure_intact(self) -> Optional[bool]:
        """Higher highs AND higher lows on the recent weekly pivots."""
        if len(self.weekly) < 20:
            return None
        highs, lows = swing_pivots(self.weekly[-26:])
        if len(highs) < 2 or len(lows) < 2:
            return None
        return bool(highs[-1] > highs[-2] and lows[-1] > lows[-2])

    def breadth_divergence(self, weeks: int = 8) -> Optional[bool]:
        """Index near its recent high while participation is NOT. Pure read.

        The most reliable warning in the whole framework: the basket rises but
        fewer constituents come with it.
        """
        if len(self.weekly) < weeks + 1 or len(self.breadth_history) < weeks + 1:
            return None
        px_now, px_max = self.weekly[-1].c, max(b.c for b in self.weekly[-weeks:])
        br_now, br_max = self.breadth_history[-1], max(self.breadth_history[-weeks:])
        near_high = px_max > 0 and px_now >= px_max * 0.99
        breadth_lagging = br_max > 0 and br_now < br_max * 0.90
        return bool(near_high and breadth_lagging)

    def confluence(self, price: Optional[float] = None) -> tuple[int, list[str]]:
        p = price if price is not None else self.close
        if p is None or not self.weekly_atr:
            return 0, []
        return confluence_at(float(p), self.levels, _CONFLUENCE_BAND_ATR * self.weekly_atr)

    def to_dict(self) -> dict[str, Any]:
        conf, hits = self.confluence()
        return {
            "as_of": self.as_of.isoformat(),
            "n_constituents": self.n_constituents,
            "close": round(self.close, 3) if self.close is not None else None,
            "weekly_atr": round(self.weekly_atr, 3) if self.weekly_atr else None,
            "weekly_bars": len(self.weekly),
            "extension_atr": (round(self.extension_atr(), 2)
                              if self.extension_atr() is not None else None),
            "correction_atr": (round(self.correction_atr(), 2)
                               if self.correction_atr() is not None else None),
            "confluence": conf,
            "confluence_levels": hits,
            "levels": {k: round(v, 3) for k, v in self.levels.items() if v is not None},
            "breadth_above_w10": self.breadth_above_w10,
            "breadth_above_w20": self.breadth_above_w20,
            "breadth_above_w50": self.breadth_above_w50,
            "rs_vs_qqq_13w": (round(self.rs_vs_benchmark(), 4)
                              if self.rs_vs_benchmark() is not None else None),
            "structure_intact": self.structure_intact(),
            "breadth_divergence": self.breadth_divergence(),
            "notes": self.notes,
        }


def build_index_series(
    per_symbol: dict[str, list[Bar]], *, weights: Optional[dict[str, float]] = None,
    min_constituents: int = 5,
) -> list[Bar]:
    """Combine per-symbol bars into one chained index series, based at 100. Pure.

    Built from average daily RETURNS rather than a normalised price average.
    That difference matters more than it looks:

      * A price average requires every constituent to have data on every date,
        so the series collapses to the SHORTEST history in the universe. With
        recent listings in the book (CRWV, NBIS, OKLO) that truncated the index
        to 21 weekly bars — not enough for a 50-week average, and barely enough
        for a 14-period ATR, which is the whole basis of the weekly framework.
      * Chaining returns lets a constituent join when it starts trading, which
        is how a real equal-weighted index handles additions. The index keeps
        its full history and simply widens as names appear.

    Each day's return is the mean across whichever constituents traded both
    that day and the prior one. Days with fewer than ``min_constituents`` are
    skipped rather than allowed to swing the index on a thin sample.

    OHLC are reconstructed by applying each day's aggregate open/high/low
    ratio (relative to the prior close) to the chained level, so weekly
    resampling, ATR and pivots all behave as they would on a real instrument.
    """
    if not per_symbol:
        return []

    by_date: dict[date, dict[str, Bar]] = {}
    for sym, bars in per_symbol.items():
        for b in bars:
            by_date.setdefault(b.d, {})[sym] = b
    dates = sorted(by_date)
    if len(dates) < 2:
        return []

    def w(sym: str) -> float:
        return (weights or {}).get(sym, 1.0)

    out: list[Bar] = []
    level = 100.0
    prev_close: dict[str, float] = {s: b.c for s, b in by_date[dates[0]].items()}
    out.append(Bar(d=dates[0], o=level, h=level, l=level, c=level,
                   v=sum(b.v for b in by_date[dates[0]].values())))

    for d in dates[1:]:
        day = by_date[d]
        num_c = num_o = num_h = num_l = wsum = 0.0
        n = 0
        for sym, b in day.items():
            pc = prev_close.get(sym)
            if not pc or pc <= 0:
                continue
            ww = w(sym)
            num_c += (b.c / pc - 1.0) * ww
            num_o += (b.o / pc - 1.0) * ww
            num_h += (b.h / pc - 1.0) * ww
            num_l += (b.l / pc - 1.0) * ww
            wsum += ww
            n += 1
        # Refresh closes for EVERY symbol that traded, including ones skipped
        # above, so a newly-listed name is priced in from its second bar.
        for sym, b in day.items():
            prev_close[sym] = b.c
        if n < min_constituents or wsum <= 0:
            continue

        prev_level = level
        level = prev_level * (1.0 + num_c / wsum)
        out.append(Bar(
            d=d,
            o=prev_level * (1.0 + num_o / wsum),
            h=prev_level * (1.0 + max(num_h, num_c, num_o) / wsum),
            l=prev_level * (1.0 + min(num_l, num_c, num_o) / wsum),
            c=level,
            v=sum(b.v for b in day.values()),
        ))
    return out


def compute_levels(weekly: list[Bar]) -> dict[str, float]:
    """Candidate support/resistance levels, namespaced by FAMILY.

    The prefix matters — ``confluence_at`` counts distinct families, so three
    moving averages in the same place register as one kind of evidence.
    """
    levels: dict[str, float] = {}
    if len(weekly) < 10:
        return levels

    for name, val in (("ma_w10", ema(weekly, 10)), ("ma_w20", sma(weekly, 20)),
                      ("ma_w50", sma(weekly, 50))):
        if val is not None:
            levels[name] = val

    highs, lows = swing_pivots(weekly)
    if highs:
        levels["pivot_high"] = highs[-1]
    if lows:
        levels["pivot_low"] = lows[-1]

    # Anchored VWAP from the lowest and highest weekly close in the last ~year.
    window = weekly[-52:] if len(weekly) >= 52 else weekly
    off = len(weekly) - len(window)
    lo_i = off + min(range(len(window)), key=lambda i: window[i].c)
    hi_i = off + max(range(len(window)), key=lambda i: window[i].c)
    for name, i in (("vwap_from_low", lo_i), ("vwap_from_high", hi_i)):
        v = anchored_vwap(weekly, i)
        if v is not None:
            levels[name] = v

    # Prior consolidation edges over the last quarter.
    q = weekly[-13:]
    if q:
        levels["range_high"] = max(b.h for b in q)
        levels["range_low"] = min(b.l for b in q)

    # Retracements of the last major leg.
    if lows and highs:
        levels.update(fib_levels(min(b.l for b in window), max(b.h for b in window)))
    return levels


async def _fetch_daily(symbols: list[str], lookback_days: int) -> dict[str, list[Bar]]:
    """Daily OHLCV per symbol.

    FMP is the primary source. Polygon's free tier enforces a 15-second gap
    between calls, so a 72-symbol universe cannot complete a cold build without
    tripping HTTP 429 — which is exactly what happened on the first attempt.
    FMP has no such limit here and is already the vendor proven to work through
    this environment's TLS proxy.

    Symbols returning fewer than ~50 bars are dropped rather than partially
    included, so a late listing cannot distort the index's rebasing.
    """
    from tradingagents.dataflows.providers.fmp import FmpProvider

    end_d = date.today()
    start_d = end_d - timedelta(days=lookback_days)
    out: dict[str, list[Bar]] = {}

    fmp = FmpProvider()
    try:
        async def _one(sym: str) -> None:
            try:
                body = await fmp._http.get_json(
                    "/stable/historical-price-eod/full",
                    params={"symbol": sym, "from": start_d.isoformat(),
                            "to": end_d.isoformat(), "apikey": fmp._api_key},
                )
            except Exception as e:  # noqa: BLE001 — one bad symbol must not kill the index
                logger.debug("basket: bars failed for %s: %s", sym, e)
                return
            rows = body if isinstance(body, list) else (body or {}).get("historical") or []
            bars: list[Bar] = []
            for r in rows:
                try:
                    bars.append(Bar(
                        d=date.fromisoformat(str(r["date"])[:10]),
                        o=float(r["open"]), h=float(r["high"]), l=float(r["low"]),
                        c=float(r["close"]), v=float(r.get("volume") or 0),
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
            # A recent listing is a legitimate constituent, not an error. SKHY
            # (Nasdaq ADR, listed ~2026-07) carries ~21 bars; chained returns
            # let it join from its first full week instead of being dropped.
            # It is still excluded automatically from any measure needing more
            # history — a 20-week SMA simply returns None for it, so it drops
            # out of breadth rather than distorting it.
            if len(bars) >= _MIN_BARS_FOR_CONSTITUENT:
                out[sym] = sorted(bars, key=lambda b: b.d)

        # Bounded concurrency: fast, without opening 72 sockets at once.
        sem = asyncio.Semaphore(8)

        async def _guarded(sym: str) -> None:
            async with sem:
                await _one(sym)

        await asyncio.gather(*(_guarded(s) for s in symbols))
    finally:
        try:
            await fmp.aclose()
        except Exception:  # noqa: BLE001
            pass
    return out


async def build_basket_index(
    symbols: Optional[list[str]] = None, *, lookback_days: int = 900,
) -> IndexState:
    """The AI-infrastructure index, weekly, with levels and volatility.

    ``lookback_days`` defaults to ~2.5 years so the weekly series carries
    enough bars for a 50-week average and a meaningful ATR.
    """
    from sqlalchemy import select

    from ..db import ThemeSymbol, get_session as db_session

    if symbols is None:
        async with db_session() as s:
            symbols = sorted({
                (r[0] or "").upper()
                for r in (await s.execute(select(ThemeSymbol.symbol))).all() if r[0]
            })

    per_symbol = await _fetch_daily(symbols, lookback_days)
    state = IndexState(as_of=date.today(), n_constituents=len(per_symbol))
    if not per_symbol:
        state.notes.append("no constituent bars available")
        return state
    if len(per_symbol) < len(symbols):
        missing = sorted(set(symbols) - set(per_symbol))
        state.notes.append(f"{len(missing)} symbol(s) without bars: {missing[:8]}")

    state.equal_weighted = build_index_series(per_symbol)
    state.weekly = to_weekly(state.equal_weighted)
    state.weekly_atr = atr(state.weekly, period=14)
    state.close = state.weekly[-1].c if state.weekly else None
    state.levels = compute_levels(state.weekly)

    # --- participation -----------------------------------------------------
    # Computed per constituent on its OWN weekly series, not against the index,
    # so this measures how many names are individually in uptrends rather than
    # how they aggregate.
    weekly_by_symbol = {sym: to_weekly(bars) for sym, bars in per_symbol.items()}
    for label, period, attr in (("w10", 10, "breadth_above_w10"),
                                ("w20", 20, "breadth_above_w20"),
                                ("w50", 50, "breadth_above_w50")):
        above = tot = 0
        for wk in weekly_by_symbol.values():
            m = sma(wk, period)
            if m is None or not wk:
                continue
            tot += 1
            if wk[-1].c > m:
                above += 1
        if tot:
            setattr(state, attr, round(above / tot, 4))

    # Breadth history (% above own 20w SMA), so divergence can be detected.
    if state.weekly:
        n_hist = min(26, len(state.weekly))
        hist: list[float] = []
        for back in range(n_hist - 1, -1, -1):
            above = tot = 0
            for wk in weekly_by_symbol.values():
                seg = wk[: len(wk) - back] if back else wk
                m = sma(seg, 20)
                if m is None or not seg:
                    continue
                tot += 1
                if seg[-1].c > m:
                    above += 1
            hist.append(round(above / tot, 4) if tot else 0.0)
        state.breadth_history = hist

    # --- cap-weighted twin, for the divergence read ------------------------
    try:
        caps = await _market_caps(sorted(per_symbol))
        if caps:
            state.cap_weighted_weekly = to_weekly(
                build_index_series(per_symbol, weights=caps))
    except Exception as e:  # noqa: BLE001
        logger.debug("basket: cap-weighted series unavailable: %s", e)

    # --- benchmark ---------------------------------------------------------
    try:
        bench = await _fetch_daily(["QQQ"], lookback_days)
        if "QQQ" in bench:
            state.benchmark_weekly = to_weekly(bench["QQQ"])
        else:
            state.notes.append("QQQ unavailable — relative strength is blind")
    except Exception as e:  # noqa: BLE001
        state.notes.append(f"benchmark fetch failed: {e}")
    return state


async def _market_caps(symbols: list[str]) -> dict[str, float]:
    """Market caps for the cap-weighted twin. Missing names fall back to 1.0,
    which degrades that constituent to equal weight rather than dropping it."""
    from tradingagents.dataflows.providers.fmp import FmpProvider

    out: dict[str, float] = {}
    fmp = FmpProvider()
    try:
        for i in range(0, len(symbols), 40):
            chunk = symbols[i:i + 40]
            body = await fmp._http.get_json(
                "/stable/batch-quote",
                params={"symbols": ",".join(chunk), "apikey": fmp._api_key})
            for r in (body or []):
                try:
                    sym = str(r.get("symbol", "")).upper()
                    cap = r.get("marketCap")
                    if sym and cap:
                        out[sym] = float(cap)
                except (TypeError, ValueError):
                    continue
    finally:
        try:
            await fmp.aclose()
        except Exception:  # noqa: BLE001
            pass
    return out
