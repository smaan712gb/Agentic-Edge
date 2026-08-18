"""The instruction must track live exposure; the band may stay a daily judgement.

The daily decision fixes two different kinds of thing and only one should stay
fixed. The STATE and its target band come from weekly evidence on completed
bars — settling those once a day is what stops the fund re-deciding strategic
exposure on intraday noise. The INSTRUCTION is not a judgement at all: it is
arithmetic on where exposure sits relative to that band, and exposure moves
continuously because a long-call book gains delta as it rises.

Freezing that number meant a book that gapped overnight from below its band
into the middle of it still read the old exposure and kept buying — comparing
yesterday's exposure against today's band. Options only trade in RTH, so every
stored instruction is necessarily acted on at prices that did not exist when it
was computed.

Offline — ``live_instruction`` is pure.
"""

from __future__ import annotations

import pytest

from api.app.portfolio.daily import INSTRUCTION_NO_DECISION, live_instruction


def _decision(band=(0.80, 0.90), instruction="add", stored_exp=0.615, state="mature_advance"):
    return {"instruction": instruction, "state": state, "target_band": list(band),
            "exposure": {"delta_adjusted_pct": stored_exp}}


# ---------------------------------------------------------------------------
# The failure this exists to prevent
# ---------------------------------------------------------------------------


def test_an_overnight_gap_into_the_band_stops_the_buying():
    """Decision stored ADD at 61.5%; the book gaps to 85%, inside the 80-90%
    band, with no order placed. The stored instruction would keep buying."""
    live = live_instruction(_decision(), current_exposure=0.85)
    assert live["instruction"] == "hold"
    assert live["stored_instruction"] == "add"
    assert live["live"] is True


def test_a_gap_through_the_band_reverses_the_instruction():
    live = live_instruction(_decision(), current_exposure=0.97)
    assert live["instruction"] == "reduce"


def test_the_band_itself_is_never_recomputed():
    """The weekly judgement stands for the day — only the arithmetic is live."""
    live = live_instruction(_decision(band=(0.60, 0.75)), current_exposure=0.95)
    assert live["target_band"] == [0.60, 0.75]
    assert live["state"] == "mature_advance"


def test_still_adds_when_genuinely_below_the_band():
    live = live_instruction(_decision(), current_exposure=0.615)
    assert live["instruction"] == "add"


# ---------------------------------------------------------------------------
# Tolerance — the band is a target, not a trigger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exposure,expected", [
    (0.785, "hold"),    # inside tolerance below the floor
    (0.775, "add"),     # beyond it
    (0.915, "hold"),    # inside tolerance above the ceiling
    (0.925, "reduce"),
])
def test_tolerance_prevents_oscillation_at_the_edges(exposure, expected):
    """Without it, exposure drifting a point past an edge trades, the trade
    overshoots, and the book pays spread on every crossing."""
    assert live_instruction(_decision(), current_exposure=exposure)["instruction"] == expected


# ---------------------------------------------------------------------------
# Degraded inputs
# ---------------------------------------------------------------------------


def test_no_decision_is_never_converted_into_an_instruction():
    """A data outage must not acquire an opinion just because live exposure is
    measurable — the band it would be compared against does not exist."""
    d = {"instruction": INSTRUCTION_NO_DECISION, "state": "unknown",
         "target_band": [None, None]}
    live = live_instruction(d, current_exposure=0.95)
    assert live["instruction"] == INSTRUCTION_NO_DECISION
    assert live["live"] is False


def test_a_missing_band_does_not_fabricate_one():
    live = live_instruction({"instruction": "add", "target_band": None},
                            current_exposure=0.95)
    assert live["live"] is False


def test_the_stored_reading_is_reported_alongside_the_live_one():
    """An operator has to be able to see that the two disagreed, and by how
    much, or the re-derivation is invisible in the logs."""
    live = live_instruction(_decision(stored_exp=0.615), current_exposure=0.85)
    assert live["stored_exposure_pct"] == 0.615
    assert live["exposure_pct"] == 0.85
