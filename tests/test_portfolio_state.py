"""Tests for the exposure state machine.

The gates say what happened; this says where aggregate exposure should sit.
Two rules protect the fund from its own signals, and both are tested here:
one step per day, and the band is a target rather than a trigger.
"""

from __future__ import annotations

import pytest

from api.app.portfolio.state import (
    STATE_ACCUMULATION, STATE_EXHAUSTION, STATE_FULL, STATE_MATURE,
    STATE_THEME_BREAK, TARGET_BANDS, classify_state, resolve, step_toward,
    tactical_trim_quantity,
)


def test_even_the_defensive_state_stays_substantially_invested():
    """The thesis is a multi-year supercycle — being flat during it is the
    larger error, so the risk-off band still holds 60-75%."""
    lo, hi = TARGET_BANDS[STATE_EXHAUSTION]
    assert lo == 0.60 and hi == 0.75


def test_theme_break_is_the_only_state_that_goes_near_flat():
    assert TARGET_BANDS[STATE_THEME_BREAK][1] <= 0.40


def test_one_step_per_day_in_both_directions():
    """100 -> 90 -> 80 -> 70, never 100 -> 60. A signal that is right stays
    right tomorrow; a signal that is wrong does less damage one step at a time."""
    assert step_toward(STATE_ACCUMULATION, STATE_EXHAUSTION) == STATE_FULL
    assert step_toward(STATE_FULL, STATE_EXHAUSTION) == STATE_MATURE
    assert step_toward(STATE_MATURE, STATE_EXHAUSTION) == STATE_EXHAUSTION
    # ...and the same on the way back up.
    assert step_toward(STATE_EXHAUSTION, STATE_ACCUMULATION) == STATE_MATURE


def test_theme_break_overrides_the_one_step_rule():
    """The structural thesis has changed — this is not a trading decision."""
    assert step_toward(STATE_ACCUMULATION, STATE_THEME_BREAK) == STATE_THEME_BREAK


def test_first_run_adopts_the_target_directly():
    assert step_toward(None, STATE_EXHAUSTION) == STATE_EXHAUSTION


def test_inside_the_band_is_hold_not_a_trade():
    """Acting on every drift produces trim-and-rebuy churn, which pays spread
    on each crossing and bleeds a book that is directionally right."""
    ps = resolve(current_exposure=0.66, target_state=STATE_EXHAUSTION,
                 previous_state=STATE_EXHAUSTION)
    assert ps.action == "hold" and ps.in_band


def test_tolerance_prevents_edge_oscillation():
    ps = resolve(current_exposure=0.76, target_state=STATE_EXHAUSTION,
                 previous_state=STATE_EXHAUSTION)
    assert ps.action == "hold", "1pt past the edge is inside tolerance"
    ps2 = resolve(current_exposure=0.80, target_state=STATE_EXHAUSTION,
                  previous_state=STATE_EXHAUSTION)
    assert ps2.action == "reduce"


def test_below_band_adds_and_above_band_reduces():
    assert resolve(current_exposure=0.40, target_state=STATE_EXHAUSTION,
                   previous_state=STATE_EXHAUSTION).action == "add"
    assert resolve(current_exposure=1.05, target_state=STATE_EXHAUSTION,
                   previous_state=STATE_EXHAUSTION).action == "reduce"


def test_the_live_2026_08_18_reading():
    """62.1% exposure in a 60-75% band with a weak-but-intact regime is HOLD —
    the book is already positioned correctly, which is a different answer from
    'the tape gate blocked you'."""
    state, _ = classify_state(theme_broken=False, regime_score=1, exhaustion=2,
                              selling_exhaustion=3, accumulation_ready=False,
                              trim_ready=False)
    assert state == STATE_EXHAUSTION
    ps = resolve(current_exposure=0.621, target_state=state,
                 previous_state=STATE_EXHAUSTION)
    assert ps.action == "hold"


def test_weak_regime_with_intact_thesis_is_defensive_not_out():
    """A weak regime inside a live supercycle is a pullback, not an exit."""
    state, reasons = classify_state(theme_broken=False, regime_score=0, exhaustion=1,
                                    selling_exhaustion=0, accumulation_ready=False,
                                    trim_ready=False)
    assert state == STATE_EXHAUSTION
    assert state != STATE_THEME_BREAK
    assert any("thesis intact" in r for r in reasons)


def test_strong_regime_is_full_participation():
    state, _ = classify_state(theme_broken=False, regime_score=4, exhaustion=0,
                              selling_exhaustion=0, accumulation_ready=False,
                              trim_ready=False)
    assert state == STATE_FULL


def test_exhaustion_outranks_a_healthy_regime_score():
    state, _ = classify_state(theme_broken=False, regime_score=4, exhaustion=3,
                              selling_exhaustion=0, accumulation_ready=False,
                              trim_ready=True)
    assert state == STATE_EXHAUSTION


def test_theme_break_outranks_everything():
    state, _ = classify_state(theme_broken=True, regime_score=4, exhaustion=0,
                              selling_exhaustion=0, accumulation_ready=True,
                              trim_ready=False)
    assert state == STATE_THEME_BREAK


# ---------------------------------------------------------------------------
# Tactical trimming
# ---------------------------------------------------------------------------


def test_a_portfolio_trim_never_closes_a_position():
    """Fully exiting a name is a thesis decision taken per name by the exit
    logic — never a consequence of an exposure band."""
    assert tactical_trim_quantity(held_qty=10, reduce_fraction=1.0) <= 9
    assert tactical_trim_quantity(held_qty=1, reduce_fraction=0.5) == 0


def test_trim_scales_with_the_reduction():
    small = tactical_trim_quantity(held_qty=20, reduce_fraction=0.10)
    large = tactical_trim_quantity(held_qty=20, reduce_fraction=0.30)
    assert 0 < small <= large


def test_trim_rounds_down():
    """Never sell more than the instruction implies."""
    q = tactical_trim_quantity(held_qty=7, reduce_fraction=0.10)
    assert q == int(q) and q >= 0


def test_no_trim_without_a_reduction():
    assert tactical_trim_quantity(held_qty=10, reduce_fraction=0.0) == 0
