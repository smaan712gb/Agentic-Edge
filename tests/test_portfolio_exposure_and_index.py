"""Tests for the portfolio-level layer: exposure measurement and the AI index.

Two foundations, both of which the system previously lacked entirely.

EXPOSURE. Every gate measured PREMIUM PAID. For a long-call book that is the
maximum loss — the right number for risk-of-ruin, the wrong one for "how
invested am I". Measured live 2026-08-18: $530,859 premium (39.7% of NAV)
against $836,099 delta-adjusted (62.6%). The system believed it held 40% with
60% in dry powder; it held 62%, and the old "100% of NAV in premium" cap would
have permitted roughly 156% real exposure.

INDEX. Portfolio decisions are expressed against the basket — it enters a
support zone, it is extended 2.5 weekly ATR, it closes above the reversal
week's high. None of that is computable from 72 separate symbols.

Offline — pure functions only, synthetic bars, no network.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from api.app.portfolio.basket_index import (
    Bar, atr, build_index_series, confluence_at, ema, sma, swing_pivots, to_weekly,
)
from api.app.portfolio.exposure import (
    BookExposure, PositionExposure, estimate_delta, position_notional,
)


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


def test_option_notional_is_delta_adjusted_not_premium():
    """The whole point: 10 NVDA calls at 0.86 delta with spot 220 control
    $189k of stock, whatever was paid for them."""
    n = position_notional(qty=10, sec_type="OPT", delta=0.86, spot=220.0, strike=150.0)
    assert n == pytest.approx(10 * 100 * 0.86 * 220.0)
    assert n == pytest.approx(189_200.0)


def test_stock_notional_collapses_to_qty_times_price():
    """Stock has delta 1 by definition."""
    assert position_notional(qty=100, sec_type="STK", delta=None, spot=None,
                             strike=None, price=50.0) == 5_000.0


def test_missing_greeks_estimate_rather_than_count_zero():
    """Counting an unpriceable position as zero exposure would tell the system
    it has room to add when it does not — the exact failure being prevented."""
    n = position_notional(qty=5, sec_type="OPT", delta=None, spot=400.0, strike=320.0)
    assert n > 0


@pytest.mark.parametrize("spot,strike,lo,hi", [
    (300.0, 100.0, 0.90, 0.95),   # deep ITM
    (110.0, 100.0, 0.75, 0.85),   # comfortably ITM
    (100.0, 100.0, 0.50, 0.60),   # at the money
    (50.0, 100.0, 0.20, 0.30),    # far OTM
])
def test_delta_estimate_tracks_moneyness(spot, strike, lo, hi):
    d = estimate_delta(spot=spot, strike=strike)
    assert lo <= d <= hi


def test_delta_estimate_errs_high_when_blind():
    """With no spot at all, assume the deep-ITM structure the book targets.
    Over-stating exposure is the safe direction for an add/no-add gate."""
    assert estimate_delta(spot=None, strike=None) >= 0.80


def _book() -> BookExposure:
    b = BookExposure(nav=1_000_000.0)
    b.positions = [
        PositionExposure("NVDA", 10, "OPT", 150.0, "20280616", 95_000, 0.86,
                         "broker", 220.0, 189_200, sleeve="core"),
        PositionExposure("ETN", 5, "OPT", 320.0, "20280121", 88_000, 0.84,
                         "broker", 436.0, 183_120, sleeve="tactical"),
    ]
    return b


def test_exposure_and_premium_are_different_numbers():
    b = _book()
    assert b.premium == 183_000
    assert b.notional == 372_320
    assert b.premium_pct == pytest.approx(0.183)
    assert b.exposure_pct == pytest.approx(0.37232)
    assert b.exposure_pct > b.premium_pct, "leverage means exposure exceeds premium"


def test_leverage_is_reported():
    assert _book().leverage == pytest.approx(372_320 / 183_000, rel=1e-6)


def test_sleeves_split_the_book():
    """A trim must be able to touch tactical and leave the core alone."""
    pct = _book().sleeve_pct()
    assert pct["core"] == pytest.approx(0.1892)
    assert pct["tactical"] == pytest.approx(0.18312)


def test_zero_nav_does_not_divide_by_zero():
    b = BookExposure(nav=0.0)
    b.positions = _book().positions
    assert b.exposure_pct == 0.0 and b.premium_pct == 0.0


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------


def _series(start_price: float, n: int, step: float, start_day: date) -> list[Bar]:
    out = []
    p = start_price
    for i in range(n):
        out.append(Bar(d=start_day + timedelta(days=i), o=p, h=p * 1.01,
                       l=p * 0.99, c=p, v=1000.0))
        p += step
    return out


def test_index_is_chained_returns_not_a_price_average():
    """A price average requires every constituent on every date, collapsing the
    series to the SHORTEST history. With recent listings in the book that
    truncated the index to 21 weekly bars — too few for a 50-week average.
    Chaining returns lets a name join when it starts trading."""
    long_hist = _series(100.0, 60, 1.0, date(2026, 1, 1))
    late_ipo = _series(50.0, 20, 0.5, date(2026, 2, 10))   # starts 40 days later
    idx = build_index_series({"OLD": long_hist, "NEW": late_ipo}, min_constituents=1)
    assert len(idx) > 40, "late listing must not truncate the whole index"
    assert idx[0].c == pytest.approx(100.0), "series is based at 100"


def test_index_equal_weights_constituents():
    """One vote each — the index must read participation, not price level."""
    cheap = _series(10.0, 30, 0.1, date(2026, 1, 1))     # +1%/day
    pricey = _series(1000.0, 30, 10.0, date(2026, 1, 1))  # +1%/day
    idx = build_index_series({"CHEAP": cheap, "PRICEY": pricey}, min_constituents=1)
    # Identical percentage moves must produce the same index path regardless of
    # nominal price — a $1,600 name cannot dominate a $140 one.
    assert idx[-1].c > idx[0].c
    solo = build_index_series({"CHEAP": cheap}, min_constituents=1)
    assert idx[-1].c == pytest.approx(solo[-1].c, rel=1e-6)


def test_thin_days_are_skipped():
    """A day with too few constituents must not swing the index."""
    a = _series(100.0, 10, 1.0, date(2026, 1, 1))
    idx = build_index_series({"A": a}, min_constituents=5)
    assert len(idx) == 1, "only the seed bar survives when the sample is too thin"


# ---------------------------------------------------------------------------
# Weekly resampling and volatility
# ---------------------------------------------------------------------------


def test_weekly_collapses_to_iso_weeks():
    daily = _series(100.0, 14, 1.0, date(2026, 1, 5))    # Mon 5 Jan, two weeks
    wk = to_weekly(daily)
    assert len(wk) == 2
    assert wk[0].o == 100.0                     # first open of week 1
    assert wk[0].c == pytest.approx(106.0)      # last close of week 1
    assert wk[0].h >= max(b.h for b in daily[:7])


def test_weekly_keeps_the_running_week():
    """A live decision is made against the incomplete current week; callers
    needing a CONFIRMED close drop the last bar themselves."""
    daily = _series(100.0, 9, 1.0, date(2026, 1, 5))     # 1 full week + 2 days
    assert len(to_weekly(daily)) == 2


def test_atr_needs_history_and_is_positive():
    bars = _series(100.0, 40, 1.0, date(2026, 1, 1))
    assert atr(bars[:5], period=14) is None
    a = atr(bars, period=14)
    assert a is not None and a > 0


def test_moving_averages_require_their_period():
    bars = _series(100.0, 30, 1.0, date(2026, 1, 1))
    assert sma(bars, 50) is None
    assert ema(bars, 50) is None
    assert sma(bars, 10) == pytest.approx(sum(b.c for b in bars[-10:]) / 10)


def test_swing_pivots_find_actual_turns():
    up = _series(100.0, 10, 2.0, date(2026, 1, 1))
    down = _series(118.0, 10, -2.0, date(2026, 1, 11))
    highs, lows = swing_pivots(up + down)
    assert highs, "a peak between rising and falling legs must register"


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------


def test_confluence_counts_families_not_levels():
    """Three moving averages stacked together are ONE kind of evidence.
    Counting them as three would let a single MA cluster fake a 5-confluence
    zone, which is precisely the noise the design rejects."""
    levels = {"ma_w10": 100.0, "ma_w20": 100.5, "ma_w50": 101.0}
    n, hits = confluence_at(100.0, levels, band=2.0)
    assert len(hits) == 3
    assert n == 1, "one family"


def test_confluence_rewards_independent_evidence():
    levels = {"ma_w20": 100.0, "pivot_low": 100.4, "vwap_from_low": 99.7,
              "fib_618": 100.2, "range_low": 99.9}
    n, _ = confluence_at(100.0, levels, band=1.0)
    assert n == 5, "five distinct families is a real zone"


def test_confluence_band_is_volatility_scaled():
    """The same distance is a tight cluster on a calm index and noise on a
    violent one — which is why the caller passes a fraction of ATR."""
    levels = {"ma_w20": 100.0, "pivot_low": 96.0}
    assert confluence_at(100.0, levels, band=1.0)[0] == 1     # tight band
    assert confluence_at(100.0, levels, band=5.0)[0] == 2     # wide band


def test_zero_band_matches_nothing():
    assert confluence_at(100.0, {"ma_w20": 100.0}, band=0.0) == (0, [])
