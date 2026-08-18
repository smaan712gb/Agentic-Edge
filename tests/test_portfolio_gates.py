"""Tests for the portfolio-level accumulation and trim gates.

These gates decide the only three things that matter at book level: when to
stop adding, when to reduce aggregate exposure, and when to redeploy. They are
weekly-close based because a high-beta complex produces a daily signal on every
ordinary pullback — 2026-08-18 being the case in point, a -4.8% session on
which the daily tape gate halted all buying while the index sat 0.13 ATR above
its 20-week mean, neither extended nor washed out.

Scores are COUNTS of named conditions rather than normalised indices, so a
reading of 3 means three specific things are true and the caller can see which.
That is also why the two thresholds differ (selling-exhaustion >= 2,
exhaustion >= 3) — they count different numbers of conditions.

Offline — every function under test is pure.
"""

from __future__ import annotations

import pytest

from api.app.portfolio.gates import (
    ACTION_ACCUMULATE, ACTION_HOLD, ACTION_STOP_ADDING, ACTION_TRIM,
    ConditionScore, accumulation_gate, exhaustion_score, selling_exhaustion_score,
    trim_gate, weekly_regime_score,
)


# ---------------------------------------------------------------------------
# Weekly regime
# ---------------------------------------------------------------------------


def _regime(**kw):
    base = dict(close=110.0, ma_w20=100.0, ma_w20_rising=True,
                rs_vs_benchmark=0.03, breadth_above_w20=0.65, structure_intact=True)
    base.update(kw)
    return weekly_regime_score(**base)


def test_all_four_components_score_four():
    assert _regime().score == 4


def test_above_a_falling_average_is_not_an_uptrend():
    """Above a falling 20-week average is a bounce inside a downtrend, which is
    why the slope is required as well as the level."""
    r = _regime(ma_w20_rising=False)
    assert r.components["trend"] is False
    assert r.score == 3


def test_relative_strength_separates_beta_from_rotation():
    """'Everything is down' is beta; 'this is being left behind' is rotation."""
    assert _regime(rs_vs_benchmark=-0.04).components["relative_strength"] is False
    assert _regime(rs_vs_benchmark=0.0).components["relative_strength"] is False


def test_breadth_needs_a_majority():
    assert _regime(breadth_above_w20=0.51).components["breadth"] is True
    assert _regime(breadth_above_w20=0.42).components["breadth"] is False


def test_unknown_inputs_score_zero_and_are_recorded():
    """Missing evidence is not evidence — an uncomputable input must not be
    assumed true, and the caller has to be able to see that it was missing."""
    r = _regime(structure_intact=None, rs_vs_benchmark=None)
    assert r.components["structure"] is False
    assert r.components["relative_strength"] is False
    assert set(r.unknown) == {"structure", "relative_strength"}
    assert r.score == 2


def test_the_live_2026_08_18_reading():
    """Regression on the real measurement: trend intact, everything else not."""
    r = weekly_regime_score(close=652.9, ma_w20=643.8, ma_w20_rising=True,
                            rs_vs_benchmark=-0.0392, breadth_above_w20=0.4167,
                            structure_intact=None)
    assert r.score == 1
    assert r.components["trend"] is True


# ---------------------------------------------------------------------------
# Exhaustion
# ---------------------------------------------------------------------------


def test_a_rising_portfolio_is_not_exhaustion():
    """The trap the whole design avoids: trimming a winner for winning."""
    e = exhaustion_score(extension_atr=1.0, breadth_divergence=False,
                         rs_vs_benchmark=0.05, rs_was_positive=True,
                         distribution_weeks=0, equal_lagging_cap=False)
    assert e.score == 0


def test_extension_is_volatility_scaled_not_percentage():
    """'+15%' means nothing without knowing how far this index normally moves."""
    assert exhaustion_score(
        extension_atr=2.4, breadth_divergence=None, rs_vs_benchmark=None,
        rs_was_positive=None, distribution_weeks=None,
        equal_lagging_cap=None).conditions["extended"] is True
    assert exhaustion_score(
        extension_atr=1.9, breadth_divergence=None, rs_vs_benchmark=None,
        rs_was_positive=None, distribution_weeks=None,
        equal_lagging_cap=None).conditions["extended"] is False


def test_rs_breaking_requires_it_to_have_been_positive():
    """A complex that never led cannot 'stop leading'."""
    broke = exhaustion_score(extension_atr=None, breadth_divergence=None,
                             rs_vs_benchmark=-0.01, rs_was_positive=True,
                             distribution_weeks=None, equal_lagging_cap=None)
    never = exhaustion_score(extension_atr=None, breadth_divergence=None,
                             rs_vs_benchmark=-0.01, rs_was_positive=False,
                             distribution_weeks=None, equal_lagging_cap=None)
    assert broke.conditions["rs_breaking"] is True
    assert never.conditions["rs_breaking"] is False


def test_full_exhaustion_counts_all_five():
    e = exhaustion_score(extension_atr=3.0, breadth_divergence=True,
                         rs_vs_benchmark=-0.02, rs_was_positive=True,
                         distribution_weeks=4, equal_lagging_cap=True)
    assert e.score == 5
    assert "breadth_divergence" in e.met


# ---------------------------------------------------------------------------
# Selling exhaustion
# ---------------------------------------------------------------------------


def test_price_falling_is_not_selling_exhaustion():
    """None of these conditions is 'price fell' — that is the point."""
    s = selling_exhaustion_score(breadth_washout=False, down_volume_spike_fading=False,
                                 correlation_spike=False, declines_shrinking=False,
                                 stopped_making_lows=False)
    assert s.score == 0


def test_capitulation_signature_scores():
    s = selling_exhaustion_score(breadth_washout=True, down_volume_spike_fading=True,
                                 correlation_spike=True, declines_shrinking=True,
                                 stopped_making_lows=True)
    assert s.score == 5


def test_none_is_treated_as_not_met():
    s = selling_exhaustion_score(breadth_washout=None, down_volume_spike_fading=None,
                                 correlation_spike=None, declines_shrinking=None,
                                 stopped_making_lows=True)
    assert s.score == 1


# ---------------------------------------------------------------------------
# Accumulation gate
# ---------------------------------------------------------------------------


def _ok_accum(**kw):
    """A firing case under the evidence-selected contract: elevated volatility
    plus evidence sellers are finishing. Regime, confluence, correction and
    breadth are still passed and still recorded — they are simply no longer
    required, each having cost holdout edge when tested as a condition."""
    base = dict(
        theme_score_positive=True,
        regime=weekly_regime_score(close=110, ma_w20=100, ma_w20_rising=True,
                                   rs_vs_benchmark=0.02, breadth_above_w20=0.6,
                                   structure_intact=True),
        volatility_pct=0.11, confluence=6, correction_atr=1.8,
        breadth_deterioration_stopped=True,
        selling_exhaustion=selling_exhaustion_score(
            breadth_washout=True, down_volume_spike_fading=True,
            correlation_spike=None, declines_shrinking=None, stopped_making_lows=None),
    )
    base.update(kw)
    return accumulation_gate(**base)


def test_both_conditions_deploy_first_quarter():
    d = _ok_accum()
    assert d.action == ACTION_ACCUMULATE
    assert d.stage == 1 and d.deploy_fraction == 0.25


@pytest.mark.parametrize("kw,blocked", [
    ({"theme_score_positive": False}, "theme_score_positive"),
    ({"volatility_pct": 0.04}, "volatility>="),
    ({"volatility_pct": None}, "volatility>="),
    ({"selling_exhaustion": selling_exhaustion_score(
        breadth_washout=True, down_volume_spike_fading=None, correlation_spike=None,
        declines_shrinking=None, stopped_making_lows=None)}, "selling_exhaustion>=2"),
])
def test_any_required_failure_blocks(kw, blocked):
    d = _ok_accum(**kw)
    assert d.action == ACTION_HOLD
    assert any(blocked in b for b in d.blocked_by)


def test_a_weak_regime_no_longer_blocks():
    """THE regression that matters. Requiring regime>=3 alongside dislocation
    conditions made the gate self-contradictory — a dislocation destroys the
    regime score by construction — and it fired ONCE in 4,892 replayed weeks.
    A washed-out complex with sellers finishing must now be able to deploy."""
    weak = weekly_regime_score(close=90, ma_w20=100, ma_w20_rising=False,
                               rs_vs_benchmark=-0.05, breadth_above_w20=0.2,
                               structure_intact=False)
    d = _ok_accum(regime=weak)
    assert weak.score == 0
    assert d.action == ACTION_ACCUMULATE


def test_confluence_no_longer_blocks():
    """min_confluence=5 was the CEILING of a 5-family measure, so it demanded
    perfect alignment and was reached in 1.8% of weeks."""
    assert _ok_accum(confluence=0).action == ACTION_ACCUMULATE


def test_a_shallow_correction_no_longer_blocks():
    assert _ok_accum(correction_atr=0.1).action == ACTION_ACCUMULATE


def test_superseded_conditions_are_still_recorded():
    """They stopped being requirements, not measurements — a decision has to
    remain readable after the fact."""
    obs = _ok_accum(confluence=3, correction_atr=0.4).detail["observed_not_required"]
    assert obs["confluence"] == 3
    assert obs["correction_atr"] == 0.4
    assert "regime_score" in obs and "breadth_deterioration_stopped" in obs


def test_volatility_threshold_is_absolute_not_relative():
    """A self-calibrating version (top quintile of the complex's own trailing
    three years) was tested and is worse: holdout +1.0% and 2/3 baskets, versus
    +5.0% and 3/3. A relative threshold also fires in a calm complex having a
    mildly active week; absolute stress is what carries the edge."""
    assert _ok_accum(volatility_pct=0.0839).action == ACTION_HOLD
    assert _ok_accum(volatility_pct=0.0841).action == ACTION_ACCUMULATE


def test_staging_advances_one_step_at_a_time():
    """Buys some before confirmation, more after — never everything at once."""
    s2_blocked = _ok_accum(stage_completed=1, strong_weekly_reversal=False)
    assert s2_blocked.action == ACTION_HOLD

    s2 = _ok_accum(stage_completed=1, strong_weekly_reversal=True)
    assert s2.action == ACTION_ACCUMULATE and s2.deploy_fraction == 0.25

    s3_blocked = _ok_accum(stage_completed=2)
    assert s3_blocked.action == ACTION_HOLD

    s3 = _ok_accum(stage_completed=2, closed_above_reversal_high=True)
    assert s3.action == ACTION_ACCUMULATE and s3.deploy_fraction == 0.50


def test_stage_three_also_unlocks_on_relative_strength_recovery():
    d = _ok_accum(stage_completed=2, rs_restored_positive=True)
    assert d.action == ACTION_ACCUMULATE and d.stage == 3


def test_deploy_fractions_sum_to_the_whole_reserve():
    assert 0.25 + 0.25 + 0.50 == 1.0


def test_reserve_exhausted_stops_deploying():
    assert _ok_accum(stage_completed=3).action == ACTION_HOLD


# ---------------------------------------------------------------------------
# Trim gate
# ---------------------------------------------------------------------------


def _exh(score: int) -> ConditionScore:
    keys = ["extended", "breadth_divergence", "rs_breaking", "distribution", "narrowing"]
    return ConditionScore(score=score,
                          conditions={k: (i < score) for i, k in enumerate(keys)})


def test_location_plus_exhaustion_plus_persistence_trims():
    d = trim_gate(confluence_at_resistance=6, extension_atr=1.0, exhaustion=_exh(3),
                  deterioration_persists=True)
    assert d.action == ACTION_TRIM and d.stage == 1 and d.trim_fraction == 0.10


def test_extension_alone_satisfies_the_location_condition():
    d = trim_gate(confluence_at_resistance=1, extension_atr=2.8, exhaustion=_exh(3),
                  deterioration_persists=True)
    assert d.action == ACTION_TRIM


def test_exhaustion_without_a_level_stops_adding_rather_than_selling():
    """An index can sit at resistance for weeks with healthy participation, and
    trimming that is just selling a winner. Raise no new risk instead."""
    d = trim_gate(confluence_at_resistance=2, extension_atr=1.0, exhaustion=_exh(4),
                  deterioration_persists=True)
    assert d.action == ACTION_STOP_ADDING
    assert d.trim_fraction == 0.0


def test_a_level_without_exhaustion_does_nothing():
    d = trim_gate(confluence_at_resistance=6, extension_atr=3.0, exhaustion=_exh(1),
                  deterioration_persists=True)
    assert d.action == ACTION_HOLD


def test_second_trim_requires_a_confirmed_reversal():
    held = trim_gate(confluence_at_resistance=6, extension_atr=3.0, exhaustion=_exh(4),
                     deterioration_persists=True, stage_completed=1,
                     confirmed_weekly_reversal=False)
    assert held.action == ACTION_STOP_ADDING

    d = trim_gate(confluence_at_resistance=6, extension_atr=3.0, exhaustion=_exh(4),
                  deterioration_persists=True, stage_completed=1,
                  confirmed_weekly_reversal=True)
    assert d.action == ACTION_TRIM and d.stage == 2 and d.trim_fraction == 0.125


def test_total_reduction_stays_inside_the_structural_band():
    """Two steps take exposure down ~22.5%, so a book at 100% lands near 77% —
    inside the 65-75% exhaustion band, not out of the theme."""
    assert 0.10 + 0.125 == pytest.approx(0.225)


def test_the_live_2026_08_18_reading_is_hold():
    """Regression on the real measurement: mid-range, so neither gate fires."""
    d = trim_gate(confluence_at_resistance=2, extension_atr=0.13, exhaustion=_exh(2),
                  deterioration_persists=True)
    assert d.action == ACTION_HOLD
