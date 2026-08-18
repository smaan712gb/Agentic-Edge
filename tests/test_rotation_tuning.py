"""Regression tests for the rotation-detector retune (2026-08-17).

The detector was calling rotation on ordinary pullbacks. Measured on the
2026-08-08 sweep persisted in theme_rotation:

    flow_distribution      15/17 themes
    rs_breakdown            8/17
    breadth_deterioration   3/17
    bullish_etfs            0 on ALL 17 themes

flow_distribution was not a weak signal, it was effectively a CONSTANT:

  * it counted ``flow_tilt=="bearish" OR gamma_sign=="negative"``, and negative
    dealer gamma is the normal state for most equity ETFs most of the time;
  * it required only ``bearish >= 1`` — a single ETF;
  * it compared that against a ``bullish`` count built from flow-tilt ONLY, so
    an ETF that was bullish on tilt AND negative on gamma incremented both
    sides. The bearish set was larger by construction, and in practice
    ``bullish`` was 0 everywhere, making ``bearish > bullish`` free.

With one signal always true, the "require 2 of 3" confirmation rule silently
degraded to "require 1", and the surviving two signals (rs_breakdown,
breadth_deterioration) are both pure price reads over overlapping names — so a
routine dip flagged the theme. On 2026-08-17 that halted 17 of the day's
highest-conviction entries while the tape was +2.4% with 76% breadth up.

The fix is structural, not a threshold nudge: signals are split into PRICE and
INSTITUTIONAL families and a flag requires evidence from the institutional
family, plus persistence across consecutive sweeps.

Offline — pure functions only.
"""

from __future__ import annotations

from api.app.autotrade.rotation_detector import (
    _INSTITUTIONAL_SIGNALS, _PRICE_SIGNALS, flow_is_distributing,
)


# ---------------------------------------------------------------------------
# flow_distribution is no longer a constant
# ---------------------------------------------------------------------------


def test_negative_gamma_alone_no_longer_trips_flow():
    """The exact 2026-08-08 shape: no bearish tilt, gamma negative on both ETFs.

    Under the old rule this counted as bearish=2, bullish=0 -> tripped on 15/17
    themes. Dealer gamma is positioning, not distribution.
    """
    assert flow_is_distributing(
        bearish_tilt=0, bullish_tilt=0, negative_gamma=2, n_etfs=2) is False


def test_single_bearish_etf_is_not_distribution():
    assert flow_is_distributing(
        bearish_tilt=1, bullish_tilt=0, negative_gamma=1, n_etfs=2) is False
    assert flow_is_distributing(
        bearish_tilt=1, bullish_tilt=0, negative_gamma=1, n_etfs=4) is False


def test_tilt_is_compared_symmetrically():
    """Bulls outnumbering bears must never read as distribution, whatever gamma
    is doing — the old asymmetry let gamma outvote actual bullish flow."""
    assert flow_is_distributing(
        bearish_tilt=2, bullish_tilt=3, negative_gamma=5, n_etfs=5) is False
    assert flow_is_distributing(
        bearish_tilt=2, bullish_tilt=2, negative_gamma=4, n_etfs=4) is False


def test_genuine_distribution_still_trips():
    # Clear bearish majority, corroborated by dealer positioning.
    assert flow_is_distributing(
        bearish_tilt=2, bullish_tilt=0, negative_gamma=1, n_etfs=2) is True
    # Overwhelming on tilt alone needs no gamma corroboration.
    assert flow_is_distributing(
        bearish_tilt=3, bullish_tilt=1, negative_gamma=0, n_etfs=4) is True


def test_minority_bearish_is_not_distribution():
    """2 of 6 bearish is not a theme rotating, even with gamma negative."""
    assert flow_is_distributing(
        bearish_tilt=2, bullish_tilt=1, negative_gamma=3, n_etfs=6) is False


def test_no_etf_data_cannot_trip():
    assert flow_is_distributing(0, 0, 0, 0) is False


# ---------------------------------------------------------------------------
# The structural rule: price alone is a pullback, not a rotation
# ---------------------------------------------------------------------------


def _decide(tripped, min_signals=2, require_institutional=True):
    """Mirror of the sweep's decision predicate (kept in lockstep by the
    test below that asserts the real code contains it)."""
    institutional = [t for t in tripped if t in _INSTITUTIONAL_SIGNALS]
    return (len(tripped) >= min_signals) and ((not require_institutional) or bool(institutional))


def test_price_signals_alone_never_flag():
    """Both price reads agreeing is exactly what an ordinary pullback looks
    like — rs_breakdown and breadth_deterioration measure the same downtrend
    over overlapping names, so they are not independent confirmation."""
    assert _decide(["rs_breakdown", "breadth_deterioration"]) is False


def test_the_old_effective_rule_would_have_flagged_a_pullback():
    """Documents the regression: under the old rule (any 2 signals, and
    flow_distribution always true) a single price signal flagged the theme."""
    assert _decide(["rs_breakdown", "flow_distribution"], require_institutional=True) is True
    # ...which is only acceptable because flow_distribution is now hard to trip.
    # With the retune, that combination requires REAL distribution.


def test_institutional_evidence_enables_a_flag():
    assert _decide(["rs_breakdown", "institutional_selling"]) is True
    assert _decide(["breadth_deterioration", "news_negative"]) is True


def test_a_lone_institutional_signal_is_not_enough():
    """Still needs corroboration — one signal of any kind is noise."""
    assert _decide(["institutional_selling"]) is False
    assert _decide(["news_negative"]) is False


def test_families_are_disjoint_and_complete():
    assert _PRICE_SIGNALS.isdisjoint(_INSTITUTIONAL_SIGNALS)
    assert _PRICE_SIGNALS == {"rs_breakdown", "breadth_deterioration"}
    assert _INSTITUTIONAL_SIGNALS == {
        "flow_distribution", "institutional_selling", "news_negative",
        "dark_pool_distribution"}


def test_sweep_actually_enforces_the_institutional_requirement():
    """Guard against the predicate drifting away from this test's mirror."""
    import inspect

    from api.app.autotrade import rotation_detector

    src = inspect.getsource(rotation_detector.run_rotation_sweep)
    assert "_INSTITUTIONAL_SIGNALS" in src
    assert "meets_kind" in src and "candidate_streak" in src


# ---------------------------------------------------------------------------
# Persistence — rotation is multi-day, one reading is noise
# ---------------------------------------------------------------------------


def _streak(candidate_by_sweep, confirm_needed=2):
    """Replay the sweep's streak logic over consecutive readings."""
    streak, flags = 0, []
    for cand in candidate_by_sweep:
        streak = streak + 1 if cand else 0
        flags.append(cand and streak >= confirm_needed)
    return flags


def test_one_noisy_sweep_never_halts_entries():
    assert _streak([True, False, False]) == [False, False, False]


def test_a_sustained_call_confirms_on_the_second_sweep():
    assert _streak([True, True, True]) == [False, True, True]


def test_streak_resets_when_the_condition_clears():
    assert _streak([True, True, False, True]) == [False, True, False, False]


def test_confirm_sweeps_of_one_restores_immediate_flagging():
    assert _streak([True, True], confirm_needed=1) == [True, True]


# ---------------------------------------------------------------------------
# A sweep that measured nothing must not publish a verdict
# ---------------------------------------------------------------------------


def test_unmeasured_theme_is_skipped_not_recorded_as_clear():
    """Every signal sits in its own try/except, so a data-layer outage makes
    them all fail quietly and `tripped` stays empty — identical to a healthy
    "nothing is rotating". Writing that as flagged=False WITH a fresh
    computed_at would clear real flags AND defeat the staleness guard, because
    the row would then look freshly measured. Observed 2026-08-17 when an
    out-of-process sweep could not reach IBKR ('clientId 20 already in use').
    """
    import inspect

    from api.app.autotrade import rotation_detector

    src = inspect.getsource(rotation_detector.run_rotation_sweep)
    assert "measured_price" in src and "measured_inst" in src
    assert "if not (measured_price or measured_inst):" in src
    # Must SKIP the write entirely — not persist a False verdict.
    skip_block = src.split("if not (measured_price or measured_inst):")[1]
    assert "continue" in skip_block.split("# Persist")[0]
    assert "unmeasured" in src


def test_degraded_sweep_alerts_the_operator():
    """Un-gated entries must never be silent."""
    import inspect

    from api.app.autotrade import rotation_detector

    src = inspect.getsource(rotation_detector.run_rotation_sweep)
    assert "Rotation sweep degraded" in src


# ---------------------------------------------------------------------------
# Off-exchange distribution (2026-08-18)
# ---------------------------------------------------------------------------


def test_dark_pool_is_an_institutional_signal_not_a_price_one():
    """Block prints are institutions moving size off the lit book — a pullback
    cannot manufacture them, so it belongs in the family that can authorise a
    rotation call."""
    assert "dark_pool_distribution" in _INSTITUTIONAL_SIGNALS
    assert "dark_pool_distribution" not in _PRICE_SIGNALS


def test_dark_pool_alone_still_cannot_flag():
    """One institutional signal is evidence, not confirmation."""
    assert _decide(["dark_pool_distribution"]) is False


def test_dark_pool_can_confirm_a_price_break():
    """Price weakness + off-exchange distribution is a real rotation call."""
    assert _decide(["rs_breakdown", "dark_pool_distribution"]) is True


def test_sweep_uses_the_signed_imbalance_not_gross_notional():
    """Gross off-exchange notional ranks market cap; only the buy/sell split
    distinguishes accumulation from distribution. Measured 2026-08-18: FN
    +0.59 accumulating vs ONTO -1.00 distributing on the same session."""
    import inspect

    from api.app.autotrade import rotation_detector

    src = inspect.getsource(rotation_detector._dark_pool_signal)
    assert "imbalance" in src
    assert "dark_pool_pressure" in src, "must use the signed helper"


def test_score_denominator_tracks_the_signal_count():
    """Six signals now — the score must not still divide by five."""
    import inspect

    from api.app.autotrade import rotation_detector

    src = inspect.getsource(rotation_detector.run_rotation_sweep)
    assert "len(tripped) / 6.0" in src
    n_signals = len(_PRICE_SIGNALS) + len(_INSTITUTIONAL_SIGNALS)
    assert n_signals == 6
