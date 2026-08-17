"""Regression tests for the 2026-08-17 live-validation findings.

Three independent defects surfaced on the first live session after a 5-day
outage, all of which cost real deployment:

  1. STALE ROTATION FLAGS blocked entries on a market that no longer existed.
     ``theme_rotation`` rows computed 2026-08-08 (money leaving the AI complex)
     halted 17 of the day's highest-conviction candidates — MU, TER, SNDK, STX,
     MRVL, CRDO — on 'breadth_deterioration', while the live tape read +2.4%
     with 76% of the universe up and money rotating back INTO semis.
     ``is_theme_rotating`` had no notion of its own freshness.

  2. DUPLICATE ORDERS per ticker. A symbol in N themes produced N candidate
     rows and therefore N orders in a single tick. The dedup set is built once
     BEFORE the loop and never updated as fills land inside it, so nothing
     downstream caught it: the per-symbol gate allows 3 actions/day and the
     per-name NAV cap only bites after ~2x. MU/MRVL/SNDK queued twice; ETN
     (4 themes) up to four times.

  3. LEAP DTE WINDOW blackout. The 18-24mo eligibility band excluded the
     January LEAP cycle — the deepest, most liquid long-dated series — for
     roughly half of every year, reported as "no expirations in window" (i.e.
     an unsuitable universe) rather than a mis-set constant.

Offline — pure functions only, no DB and no broker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.app.autotrade.entry_loop import (
    _one_order_per_symbol,
    rotation_flag_is_fresh,
)


# ---------------------------------------------------------------------------
# 1. One order per ticker, regardless of how many themes claim it
# ---------------------------------------------------------------------------


def _rows_multi_theme():
    """Composite-desc rows as the DB returns them: MU and ETN span themes."""
    return [
        ("run-memory", "MU", 7.8),      # ai-memory-wall  — strongest MU read
        ("run-test", "TER", 7.8),
        ("run-storage", "MU", 7.6),     # ai-storage      — duplicate
        ("run-power", "ETN", 7.4),      # data-center-power-wall
        ("run-grid", "ETN", 7.4),       # grid-bottleneck — duplicate
        ("run-cooling", "ETN", 7.3),    # liquid-cooling  — duplicate
        ("run-dc", "ETN", 7.2),         # ai-dc-construction — duplicate
    ]


_THEMES = {
    "run-memory": "ai-memory-wall", "run-test": "ai-test-metrology",
    "run-storage": "ai-storage", "run-power": "data-center-power-wall",
    "run-grid": "grid-bottleneck", "run-cooling": "liquid-cooling",
    "run-dc": "ai-dc-construction",
}


def test_multi_theme_symbol_yields_exactly_one_order():
    cands, collapsed = _one_order_per_symbol(_rows_multi_theme(), _THEMES, set(), set())
    symbols = [c[2] for c in cands]
    assert symbols.count("MU") == 1, "MU in 2 themes must produce ONE order"
    assert symbols.count("ETN") == 1, "ETN in 4 themes must produce ONE order"
    assert sorted(symbols) == ["ETN", "MU", "TER"]
    # Operator-visible accounting of what was collapsed.
    assert collapsed == {"MU": 2, "ETN": 4}


def test_dedup_keeps_the_strongest_theme_read():
    """Rows arrive composite-desc, so the survivor must be the first/highest."""
    cands, _ = _one_order_per_symbol(_rows_multi_theme(), _THEMES, set(), set())
    mu = next(c for c in cands if c[2] == "MU")
    etn = next(c for c in cands if c[2] == "ETN")
    assert mu[0] == "run-memory" and mu[3] == 7.8      # not the 7.6 ai-storage row
    assert etn[0] == "run-power" and etn[3] == 7.4     # not the 7.3 / 7.2 rows


def test_dedup_is_case_insensitive():
    rows = [("r1", "MU", 7.8), ("r2", "mu", 7.5)]
    cands, collapsed = _one_order_per_symbol(rows, {"r1": "t1", "r2": "t2"}, set(), set())
    assert len(cands) == 1
    assert collapsed == {"MU": 2}


def test_existing_and_attempted_filters_still_apply():
    """Dedup must not weaken the pre-existing de-dup guards."""
    rows = [("run-memory", "MU", 7.8), ("run-test", "TER", 7.8)]
    # Already routed this exact (run, symbol).
    c1, _ = _one_order_per_symbol(rows, _THEMES, {("run-memory", "MU")}, set())
    assert [c[2] for c in c1] == ["TER"]
    # Already attempted this symbol anywhere in the window.
    c2, _ = _one_order_per_symbol(rows, _THEMES, set(), {"TER"})
    assert [c[2] for c in c2] == ["MU"]


def test_no_duplicates_means_nothing_collapsed():
    rows = [("run-memory", "MU", 7.8), ("run-test", "TER", 7.6)]
    cands, collapsed = _one_order_per_symbol(rows, _THEMES, set(), set())
    assert len(cands) == 2
    assert collapsed == {}


# ---------------------------------------------------------------------------
# 2. Rotation flags expire — stale state is wrong evidence, not weak evidence
# ---------------------------------------------------------------------------


def test_fresh_rotation_flag_is_honoured():
    recent = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert rotation_flag_is_fresh(recent, 6.0) is True


def test_the_actual_2026_08_17_incident_would_be_ignored():
    """9-day-old flags blocked 17 candidates on an accumulation day."""
    now = datetime(2026, 8, 17, 13, 32, tzinfo=timezone.utc)
    computed = datetime(2026, 8, 8, 22, 4, tzinfo=timezone.utc)
    assert rotation_flag_is_fresh(computed, 6.0, now=now) is False


def test_naive_timestamp_is_treated_as_utc():
    """SQLite returns naive datetimes for timezone-aware columns; comparing a
    naive to an aware datetime raises TypeError, which would have been swallowed
    by the caller's except and failed open for the wrong reason."""
    now = datetime(2026, 8, 17, 13, 32, tzinfo=timezone.utc)
    stale_naive = datetime(2026, 8, 8, 22, 4)          # no tzinfo
    fresh_naive = datetime(2026, 8, 17, 13, 0)         # no tzinfo
    assert rotation_flag_is_fresh(stale_naive, 6.0, now=now) is False
    assert rotation_flag_is_fresh(fresh_naive, 6.0, now=now) is True


def test_boundary_is_inclusive():
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    exactly_6h = now - timedelta(hours=6)
    just_over = now - timedelta(hours=6, seconds=1)
    assert rotation_flag_is_fresh(exactly_6h, 6.0, now=now) is True
    assert rotation_flag_is_fresh(just_over, 6.0, now=now) is False


def test_missing_timestamp_fails_open_to_fresh():
    """A schema gap must not silently disable the detector entirely."""
    assert rotation_flag_is_fresh(None, 6.0) is True


def test_ttl_does_not_survive_a_weekend_gap():
    """Friday's close-of-session sweep must NOT still be gating Monday's open.
    The sweep reruns every 30 min during RTH, so a real rotation is re-flagged
    within one entry tick."""
    friday_last_sweep = datetime(2026, 8, 14, 20, 45, tzinfo=timezone.utc)
    monday_open = datetime(2026, 8, 17, 13, 32, tzinfo=timezone.utc)
    assert rotation_flag_is_fresh(friday_last_sweep, 6.0, now=monday_open) is False


# ---------------------------------------------------------------------------
# 3. LEAP DTE window — January cycle must qualify year-round
# ---------------------------------------------------------------------------


def test_window_is_wide_enough_to_never_blackout_the_january_cycle():
    """The real invariant, independent of today's date.

    US equities list their deepest long-dated LEAPs on the JANUARY cycle, so
    consecutive candidate expiries are ~365 days apart. For at least one to sit
    inside the window on ANY given day, the window must be at least a year wide.

    Old band 540-720d was only 180d wide — narrower than the gap between
    Januaries — so for ~6 months a year NO January series qualified at all
    (on 2026-08-17: Jan-2028 = 522d, under the floor; Jan-2029 = 886d, over the
    ceiling). This asserts the geometry, not a snapshot, so it keeps holding
    next year.
    """
    from tradingagents.strategies.pmcc import LEAP_DTE_MAX_DAYS, LEAP_DTE_MIN_DAYS

    width = LEAP_DTE_MAX_DAYS - LEAP_DTE_MIN_DAYS
    assert width >= 365, (
        f"eligibility window is {width}d wide; anything under 365d lets the "
        f"annual January-cycle blackout reappear"
    )


def test_target_is_decoupled_from_the_window_midpoint():
    """The floor is a sparse-chain fallback. Deriving the target from it would
    drag the book ~5 months shorter (270-720 midpoints to 495d) every time the
    floor widened — a silent strategy change disguised as a config tweak."""
    from tradingagents.strategies.pmcc import (
        LEAP_DTE_MAX_DAYS, LEAP_DTE_MIN_DAYS, LEAP_DTE_TARGET_DAYS,
    )

    midpoint = (LEAP_DTE_MIN_DAYS + LEAP_DTE_MAX_DAYS) // 2
    assert LEAP_DTE_TARGET_DAYS != midpoint
    assert LEAP_DTE_TARGET_DAYS > midpoint, "must still prefer the longer-dated end"
    assert LEAP_DTE_MIN_DAYS < LEAP_DTE_TARGET_DAYS <= LEAP_DTE_MAX_DAYS


def test_selector_prefers_long_dated_over_the_nearer_eligible_series():
    """Widening the floor must not pull selection toward the short end.

    The board is built relative to TODAY so this stays valid as the calendar
    moves. Under the old midpoint target (495d) this would have quoted the 487d
    and 522d contracts; it must instead quote the 669d/522d pair.
    """
    from datetime import date, timedelta

    from tradingagents.strategies.pmcc import (
        LEAP_DTE_MAX_DAYS, LEAP_DTE_MIN_DAYS, LEAP_DTE_TARGET_DAYS,
        _dte, _filter_expirations_by_dte,
    )

    today = date.today()

    def exp(dte: int) -> str:
        return (today + timedelta(days=dte)).strftime("%Y%m%d")

    # 151d (too near) · 305 · 487 · 522 (the January cycle) · 669 · 886 (too far)
    board = [exp(d) for d in (151, 305, 487, 522, 669, 886)]
    eligible = _filter_expirations_by_dte(board, LEAP_DTE_MIN_DAYS, LEAP_DTE_MAX_DAYS)

    assert exp(522) in eligible, "the January-cycle contract must be eligible"
    assert exp(151) not in eligible and exp(886) not in eligible

    quoted = sorted(eligible, key=lambda e: abs(_dte(e) - LEAP_DTE_TARGET_DAYS))[:2]
    assert set(quoted) == {exp(669), exp(522)}, (
        f"expected the two longest-dated eligible contracts, got "
        f"{[(q, _dte(q)) for q in quoted]}"
    )
