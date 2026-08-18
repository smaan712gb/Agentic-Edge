"""A data outage must not be mistaken for evidence to reduce exposure.

Every score in the framework treats an uncomputable input as zero, which is
right when one input is missing and catastrophic when they all are. An empty
IndexState scores regime 0/4, exhaustion 0 and selling-exhaustion 0; that
combination classifies as ``exhaustion_rotation`` with a 60-75% band, and any
book above 75% then receives REDUCE.

Found live: FMP began returning 401 ("Invalid API KEY") mid-session on
2026-08-18, hours after a healthy decision. An empty index was verified to
return `reduce` at 95% exposure — a revoked API key instructing the fund to
sell, on no information at all. That directly contradicts the standing rule
that exits must be signal-driven.

These tests pin the distinction shut: absent evidence produces no instruction,
weak evidence still produces a real one.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from api.app.portfolio.basket_index import Bar, IndexState
from api.app.portfolio.daily import (
    INSTRUCTION_NO_DECISION, MIN_WEEKS_FOR_DECISION, evaluate_index, evidence_missing,
)


def _bars(n: int, start: float = 100.0, step: float = 1.0) -> list[Bar]:
    d0 = date(2024, 1, 1)
    out = []
    for i in range(n):
        c = start + i * step
        out.append(Bar(d=d0 + timedelta(weeks=i), o=c - 0.5, h=c + 1.0,
                       l=c - 1.0, c=c, v=1_000_000))
    return out


def _healthy(n: int = 80) -> IndexState:
    wk = _bars(n)
    idx = IndexState(as_of=wk[-1].d, n_constituents=40)
    idx.weekly = wk
    idx.weekly_atr = 2.0
    idx.close = wk[-1].c
    idx.levels = {"ma_w20": wk[-1].c - 5}
    idx.breadth_above_w20 = 0.7
    idx.breadth_history = [0.7] * 26
    idx.benchmark_weekly = _bars(n, start=50.0, step=0.4)
    return idx


# ---------------------------------------------------------------------------
# The failure that was live
# ---------------------------------------------------------------------------


def test_an_empty_index_never_instructs_a_reduction():
    """The exact shape of the 2026-08-18 outage, at the exposure that made it
    dangerous. Before the guard this returned 'reduce'."""
    idx = IndexState(as_of=date.today(), n_constituents=0)
    d = evaluate_index(idx, exposure_pct=0.95, previous_state=None)
    assert d["instruction"] == INSTRUCTION_NO_DECISION
    assert d["instruction"] != "reduce"


@pytest.mark.parametrize("exposure", [0.0, 0.4, 0.615, 0.95, 1.3])
def test_no_data_yields_no_instruction_at_any_exposure(exposure):
    idx = IndexState(as_of=date.today(), n_constituents=0)
    d = evaluate_index(idx, exposure_pct=exposure, previous_state=None)
    assert d["instruction"] == INSTRUCTION_NO_DECISION
    assert d["state"] == "unknown"
    assert d["target_band"] == [None, None]


def test_the_reason_is_reported_not_just_the_refusal():
    """An operator has to be able to act on this, so it names what is missing
    rather than reporting a generic failure."""
    idx = IndexState(as_of=date.today(), n_constituents=0)
    d = evaluate_index(idx, exposure_pct=0.9, previous_state=None)
    assert d["degraded"]
    assert any("weekly" in g for g in d["degraded"])


# ---------------------------------------------------------------------------
# evidence_missing itself
# ---------------------------------------------------------------------------


def test_a_healthy_index_has_no_gaps():
    assert evidence_missing(_healthy()) == []


def test_too_little_history_is_absent_evidence_not_weak_evidence():
    idx = _healthy(n=MIN_WEEKS_FOR_DECISION - 1)
    gaps = evidence_missing(idx)
    assert gaps and any("weekly bars" in g for g in gaps)
    assert evaluate_index(idx, exposure_pct=0.95)["instruction"] == INSTRUCTION_NO_DECISION


def test_a_missing_atr_blocks_a_decision():
    """Every extension and correction measure is ATR-scaled, so without it the
    location half of both gates is silently inert."""
    idx = _healthy()
    idx.weekly_atr = None
    assert any("ATR" in g for g in evidence_missing(idx))


def test_zero_constituents_is_caught_even_with_a_series_present():
    """A stale cached series with no live constituents is still an outage."""
    idx = _healthy()
    idx.n_constituents = 0
    assert any("constituents" in g for g in evidence_missing(idx))


def test_none_index_is_handled():
    assert evidence_missing(None) == ["no index"]


# ---------------------------------------------------------------------------
# The guard must not swallow real decisions
# ---------------------------------------------------------------------------


def test_a_healthy_index_still_produces_a_real_instruction():
    """The guard must be narrow: it exists to catch absence, and a framework
    that refuses to decide whenever conditions are poor is just as useless as
    one that decides on nothing."""
    d = evaluate_index(_healthy(), exposure_pct=0.95, previous_state=None)
    assert d["instruction"] in ("add", "hold", "reduce")
    assert d["state"] != "unknown"
    assert d["regime"]["score"] is not None


def test_a_weak_but_measured_regime_still_decides():
    """Falling index, real data — this SHOULD produce a defensive answer. It is
    the case the no-data guard must not be confused with."""
    idx = _healthy()
    idx.weekly = _bars(80, start=200.0, step=-1.0)
    idx.close = idx.weekly[-1].c
    idx.levels = {"ma_w20": idx.close + 10}
    idx.breadth_above_w20 = 0.1
    d = evaluate_index(idx, exposure_pct=0.95, previous_state=None)
    assert d["instruction"] != INSTRUCTION_NO_DECISION
    assert d["regime"]["score"] is not None
