"""Tests for wiring the Morning Report into the deciding agent (2026-08-17).

The brief was built for a human: read it at 08:45 ET, place the trades. Once
entries were automated it became a newsletter the system ignored — nothing in
the trading path imported it, and `compute_entry_score`'s own docstring said
"decision-support only: the entry LOOP keeps its own gate stack".

The cost showed up the same day. The loop ranks candidates by research
composite alone, so it bought SNDK at "Entry 46/100 - not ready, extended 20%
above the 8 EMA" while ETN sat unbought at "Entry 82/100 - pullback into the
8/21 EMA zone, buyable dip". Composite says WHAT is worth owning; the entry
score says whether NOW is a sane moment to buy it. For a multi-year builder,
overpaying into an extended name is worse than waiting a session.

Only the outputs with no other source in the trading path are wired — posture,
the street read, and the entry score. `top_ideas`, `holdings`, rotation and
smart-money overlap are all assembled from rows the loops already consume, so
feeding those back would double-count the same evidence.

Offline — pure functions only.
"""

from __future__ import annotations

import pytest

from api.app.report.brief_wiring import (
    blended_rank_key, brief_decision_slice, entry_score_sizing_factor,
    idea_sizing_factor, posture_sizing_factor,
)


# ---------------------------------------------------------------------------
# Posture -> portfolio-level sizing
# ---------------------------------------------------------------------------


def test_posture_is_symmetric_around_neutral():
    assert posture_sizing_factor(50, 0.25) == 1.0
    assert posture_sizing_factor(100, 0.25) == 1.25
    assert posture_sizing_factor(0, 0.25) == 0.75


def test_posture_is_clamped_to_the_dial_range():
    assert posture_sizing_factor(999, 0.25) == 1.25
    assert posture_sizing_factor(-999, 0.25) == 0.75


def test_posture_never_goes_negative():
    """A tilt larger than 1 must not invert the position."""
    assert posture_sizing_factor(0, 5.0) >= 0.0


@pytest.mark.parametrize("bad", [None, "n/a", object()])
def test_posture_degrades_to_neutral_on_bad_input(bad):
    assert posture_sizing_factor(bad, 0.25) == 1.0


# ---------------------------------------------------------------------------
# Entry timing -> ranking and sizing
# ---------------------------------------------------------------------------


def test_entry_score_tilts_size_around_neutral():
    assert entry_score_sizing_factor(50, 0.20) == 1.0
    assert entry_score_sizing_factor(100, 0.20) == 1.20
    assert entry_score_sizing_factor(0, 0.20) == 0.80


def test_a_poor_setup_is_sized_down_not_skipped():
    """Never a gate: a weak setup on a strong thesis still gets bought.

    The cost of skipping a name you intend to hold for years exceeds the cost
    of a mediocre entry, so this must stay strictly positive.
    """
    assert entry_score_sizing_factor(0, 0.20) > 0.0
    assert entry_score_sizing_factor(39, 0.20) > 0.0     # ONTO, "not ready"


def test_the_actual_2026_08_17_misordering_is_corrected():
    """ETN (buyable dip) must outrank SNDK and ONTO (extended / not ready)."""
    w = 0.4
    etn = blended_rank_key(7.4, 82, w)     # "pullback into the 8/21 EMA zone"
    sndk = blended_rank_key(7.6, 46, w)    # "extended 20% above the 8 EMA"
    onto = blended_rank_key(7.6, 39, w)    # "not ready"
    assert etn > sndk, "a buyable dip must outrank an extended name"
    assert etn > onto
    # ...even though ETN's composite is the LOWER of the two.
    assert 7.4 < 7.6


def test_thesis_still_dominates_at_the_default_weight():
    """Timing breaks ties; it must not let a weak thesis leapfrog a strong one."""
    w = 0.4
    strong_thesis_ok_entry = blended_rank_key(9.5, 50, w)
    weak_thesis_perfect_entry = blended_rank_key(4.0, 100, w)
    assert strong_thesis_ok_entry > weak_thesis_perfect_entry


def test_uncovered_symbol_keeps_its_composite_rank():
    """A name the brief never scored must not be pushed down for missing data."""
    assert blended_rank_key(7.6, None, 0.4) == 76.0
    # And it still orders correctly against other uncovered names.
    assert blended_rank_key(7.8, None, 0.4) > blended_rank_key(7.6, None, 0.4)


def test_rank_weight_extremes_behave():
    assert blended_rank_key(8.0, 20, 0.0) == 80.0    # pure composite
    assert blended_rank_key(8.0, 20, 1.0) == 20.0    # pure entry timing


# ---------------------------------------------------------------------------
# Street / institutional read
# ---------------------------------------------------------------------------


def test_street_read_blends_only_the_signals_present():
    """A name with only analyst coverage must not be penalised for lacking an
    institutional verdict — the average is over what exists."""
    upside_only = idea_sizing_factor({"upside_pct": 50}, 0.15)
    assert upside_only == pytest.approx(1.15, abs=0.001)


def test_street_read_directions():
    strong = idea_sizing_factor(
        {"upside_pct": 54, "n_up_30d": 3, "n_down_30d": 0,
         "institutional_label": "constructive"}, 0.15)
    weak = idea_sizing_factor(
        {"upside_pct": -30, "n_up_30d": 0, "n_down_30d": 3,
         "institutional_label": "stepping back"}, 0.15)
    assert strong > 1.0 > weak > 0.0


def test_unknown_symbol_is_exactly_neutral():
    """An uncovered candidate must size precisely as it does without the brief."""
    assert idea_sizing_factor(None, 0.15) == 1.0
    assert idea_sizing_factor({}, 0.15) == 1.0
    assert idea_sizing_factor({"institutional_label": "limited data"}, 0.15) == 1.0


# ---------------------------------------------------------------------------
# The persisted slice
# ---------------------------------------------------------------------------


def _report():
    return {
        "as_of": "2026-08-17",
        "generated_at": "2026-08-17T12:45:00+00:00",
        "posture": {"score": 75.0, "label": "constructive",
                    "components": {"theme_health": 66.0}, "breaker_capped": False},
        "top_ideas": [
            {"symbol": "MU", "composite": 7.8,
             "analyst": {"upside_pct": 54.0, "pt_consensus": 1562.0,
                         "n_up_30d": 0, "n_down_30d": 0},
             "institutional": {"label": "constructive"},
             "entry": {"score": 71.0, "label": "good setup", "reasons": [
                 {"ok": True, "text": "perfect EMA stack (8>21>50>100>200)"},
                 {"ok": False, "text": "extended 8% above the 8 EMA — chase risk"},
             ]}},
            {"symbol": "etn", "composite": 7.4,
             "analyst": {}, "institutional": {},
             "entry": {"score": 82.0, "label": "good setup", "reasons": []}},
        ],
        # Must NOT be persisted — large, and no gate can act on it.
        "brief": {"narrative": "x" * 5000},
        "holdings": [{"symbol": "NBIS"}],
    }


def test_slice_captures_what_the_loops_act_on():
    sl = brief_decision_slice(_report())
    assert sl["posture_score"] == 75.0 and sl["posture_label"] == "constructive"
    assert sl["ideas"]["MU"]["entry_score"] == 71.0
    assert sl["ideas"]["MU"]["upside_pct"] == 54.0
    assert sl["ideas"]["MU"]["institutional_label"] == "constructive"


def test_slice_uppercases_symbols():
    """The loop looks up by upper-cased symbol; a lowercase idea must still hit."""
    assert "ETN" in brief_decision_slice(_report())["ideas"]


def test_slice_keeps_only_failed_entry_checks():
    """The passing checks are noise in an audit row; the warnings are the point."""
    warns = brief_decision_slice(_report())["ideas"]["MU"]["entry_warnings"]
    assert warns == ["extended 8% above the 8 EMA — chase risk"]


def test_slice_excludes_the_narrative_and_holdings():
    """Storing the prose would bloat the audit table with text no gate reads."""
    sl = brief_decision_slice(_report())
    assert "brief" not in sl and "holdings" not in sl
    assert len(str(sl)) < 2000


def test_slice_survives_a_degraded_report():
    """Every report section is best-effort and may come back as {'error': ...}."""
    sl = brief_decision_slice({"top_ideas": {"error": "boom"}, "posture": None})
    assert sl["ideas"] == {} and sl["posture_score"] is None
