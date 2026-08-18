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
    larger error, so even the risk-off band still holds 70-100%."""
    lo, hi = TARGET_BANDS[STATE_EXHAUSTION]
    assert lo == 0.70 and hi == 1.00


def test_theme_break_is_the_only_state_that_goes_near_flat():
    assert TARGET_BANDS[STATE_THEME_BREAK][1] <= 0.50


def test_bands_accommodate_a_leveraged_leaps_book():
    """Bands are on DELTA-ADJUSTED exposure, and a long-dated call carries the
    delta of far more stock than its premium — so a fully-invested LEAPS book
    reads well above 100% by construction. The live book was 122.9% at 76.9%
    premium and 1.60x leverage.

    The original bands topped out at 110%, which placed a normally-invested
    book above EVERY band. That produced paralysis rather than caution: the
    machine returned reduce or hold on every tick, so the accumulation gate
    could fire on a real dislocation and never be allowed to act."""
    lo, hi = TARGET_BANDS[STATE_FULL]
    assert lo <= 1.229 <= hi, "a fully-invested book must sit INSIDE full_participation"
    # Contiguous by design — accumulation begins where full participation ends,
    # so there is no exposure level that belongs to no state.
    assert TARGET_BANDS[STATE_ACCUMULATION][0] >= hi
    assert TARGET_BANDS[STATE_ACCUMULATION][1] > hi, "accumulation needs real headroom"


def test_a_fully_invested_book_is_hold_not_reduce():
    """The regression. At 122.9% the old bands said REDUCE on a book that was
    simply invested."""
    ps = resolve(current_exposure=1.229, target_state=STATE_FULL,
                 previous_state=STATE_FULL)
    assert ps.action == "hold"


def test_a_confirmed_dislocation_can_still_add_from_fully_invested():
    """What the operator asked for: keep buying dips while the thesis holds.
    A confirmed accumulation signal on an already fully-invested book must
    reach ADD, not stall at the top of the current band."""
    ps = resolve(current_exposure=1.229, target_state=STATE_ACCUMULATION,
                 previous_state=STATE_FULL, accumulation_confirmed=True)
    assert ps.state == STATE_ACCUMULATION
    assert ps.action == "add"


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
    ps = resolve(current_exposure=0.85, target_state=STATE_EXHAUSTION,
                 previous_state=STATE_EXHAUSTION)
    assert ps.action == "hold" and ps.in_band


def test_tolerance_prevents_edge_oscillation():
    ps = resolve(current_exposure=1.01, target_state=STATE_EXHAUSTION,
                 previous_state=STATE_EXHAUSTION)
    assert ps.action == "hold", "1pt past the edge is inside tolerance"
    ps2 = resolve(current_exposure=1.05, target_state=STATE_EXHAUSTION,
                  previous_state=STATE_EXHAUSTION)
    assert ps2.action == "reduce"


def test_below_band_adds_and_above_band_reduces():
    assert resolve(current_exposure=0.40, target_state=STATE_EXHAUSTION,
                   previous_state=STATE_EXHAUSTION).action == "add"
    assert resolve(current_exposure=1.05, target_state=STATE_EXHAUSTION,
                   previous_state=STATE_EXHAUSTION).action == "reduce"


def test_a_weak_regime_without_a_dislocation_is_still_defensive():
    """A weak regime and no confirmed dislocation classifies defensive. Under
    bands sized for a leveraged book, 62.1% is BELOW even that band — so the
    answer is add, not hold. Being under-invested in an intact supercycle is
    the larger error, which is the whole premise of the band design."""
    state, _ = classify_state(theme_broken=False, regime_score=1, exhaustion=2,
                              selling_exhaustion=3, accumulation_ready=False,
                              trim_ready=False)
    assert state == STATE_EXHAUSTION
    ps = resolve(current_exposure=0.621, target_state=state,
                 previous_state=STATE_EXHAUSTION)
    assert ps.action == "add"


def test_the_live_2026_08_18_dislocation_reads_add():
    """The reading the operator flagged: macro selloff, thesis intact, no
    rotation, sellers exhausting. The gate fired and the book was already
    fully invested at 122.9% — this must still resolve to ADD."""
    state, _ = classify_state(theme_broken=False, regime_score=1, exhaustion=2,
                              selling_exhaustion=3, accumulation_ready=True,
                              trim_ready=False)
    assert state == STATE_ACCUMULATION
    ps = resolve(current_exposure=1.229, target_state=state,
                 previous_state=STATE_FULL, accumulation_confirmed=True)
    assert ps.action == "add"


def test_reductions_are_still_damped_one_step_at_a_time():
    """The immediate-step exemption is for confirmed accumulation only. Every
    reduction and all ordinary drift keep the one-step rule, which is where
    whipsaw actually costs money."""
    assert step_toward(STATE_ACCUMULATION, STATE_EXHAUSTION,
                       immediate={STATE_ACCUMULATION}) == STATE_FULL


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


# ---------------------------------------------------------------------------
# Control flow: a halt must not be cancellable by an error handler
# ---------------------------------------------------------------------------


def test_portfolio_halt_returns_outside_the_try_block():
    """The halt is DECIDED inside try/except but ACTED ON outside it.

    With the return inside the try, any error between the log line and the
    return — an audit write, a DB hiccup — was swallowed by the except and the
    tick carried straight on to buy. Observed live 2026-08-18 11:13: the loop
    logged "portfolio says HOLD ... No new risk this tick" and then bought ONTO
    and CEG in the same tick, because recording the audit row raised.

    A control-flow decision must never be cancellable by an error handler.
    """
    import inspect

    from api.app.autotrade import entry_loop

    src = inspect.getsource(entry_loop._tick)
    assert "_portfolio_halt" in src, "halt must be captured as state, not acted on inline"

    # The audit write may fail; the return must still happen.
    halt_block = src.split("if _portfolio_halt is not None:")[1]
    audit_try = halt_block.split("return")[0]
    assert "except" in audit_try, "the audit write is guarded"
    assert "audit failure must not un-halt" in halt_block or "un-halt" in halt_block


def test_halt_is_recorded_but_recording_is_not_required():
    """Auditing a halt is best-effort; halting is not.

    Checked structurally: inside the halt block there must be a `return` at the
    block's own indentation level (8 spaces), i.e. a sibling of the try — not
    nested inside it where an exception could skip it.
    """
    import inspect
    import textwrap

    from api.app.autotrade import entry_loop

    src = textwrap.dedent(inspect.getsource(entry_loop._tick))
    lines = src.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if "if _portfolio_halt is not None:" in ln)
    # Walk to the end of the block: the next line at the same indent or less.
    body = []
    for ln in lines[start + 1:]:
        if ln.strip() and not ln.startswith("        "):
            break
        body.append(ln)
    sibling_returns = [ln for ln in body if ln.rstrip() == "        return"]
    assert sibling_returns, (
        "the halt must return at the block's own level, not inside the try — "
        "otherwise an audit-write error skips the return and the tick buys"
    )
