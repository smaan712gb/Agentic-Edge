"""Tests for the feed-integrity detectors.

Each detector is regression-tested against the shape of a real production
defect, because the value of this module is not that it is clever but that it
would have caught the specific things that got through. Six defects ran live
for weeks while every process-level health check stayed green:

    options flow   full coverage, every value zero      -> flatline (all_zero)
    dark pool      404 on every call, zero coverage     -> empty
    macro gate     both inputs null, defaulted to calm  -> empty
    rotation       job stopped, flags read as current   -> silent

The fifth detector covers the inverse: a gate so strict it never opens, which
from outside is indistinguishable from a market that never qualifies.

Offline — every function under test is pure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.app.autotrade.feed_integrity import (
    Observation, analyze_feed, detect_degraded, detect_empty, detect_flatline,
    detect_never_fires, detect_silence,
)

T0 = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)


def _obs(feed="f", *, n=6, hours=24.0, numeric=None, coverage=None, subjects=50):
    """n readings evenly spread over `hours`. numeric/coverage may be a list."""
    step = timedelta(hours=hours / max(n - 1, 1))
    out = []
    for i in range(n):
        num = numeric[i] if isinstance(numeric, list) else numeric
        cov = coverage[i] if isinstance(coverage, list) else coverage
        out.append(Observation(feed=feed, ts=T0 + step * i, numeric=num,
                               coverage=cov, subjects=subjects))
    return out


# ---------------------------------------------------------------------------
# empty — the dark-pool 404 and the null macro gate
# ---------------------------------------------------------------------------


def test_zero_coverage_throughout_is_empty():
    a = detect_empty(_obs(coverage=0.0))
    assert a is not None and a.kind == "empty"
    assert a.evidence["observations"] == 6


def test_one_good_reading_clears_empty():
    """A feed that answered even once is not dead — it is degraded at worst,
    and calling it dead would send an operator hunting a broken provider."""
    assert detect_empty(_obs(coverage=[0.0, 0.0, 0.0, 0.0, 0.0, 0.8])) is None


def test_empty_needs_enough_evidence():
    assert detect_empty(_obs(n=2, coverage=0.0)) is None


def test_unknown_coverage_is_not_zero_coverage():
    """None means the caller did not measure coverage, which is not the same
    claim as 'the provider returned nothing'."""
    assert detect_empty(_obs(coverage=None)) is None


# ---------------------------------------------------------------------------
# flatline — the OCC parse defect
# ---------------------------------------------------------------------------


def test_all_zero_across_sessions_is_flagged_as_a_parse_defect():
    a = detect_flatline(_obs(numeric=0.0, coverage=1.0, hours=72))
    assert a is not None and a.kind == "flatline"
    assert a.evidence["all_zero"] is True
    assert "parse or aggregation defect" in a.detail


def test_constant_nonzero_reads_as_a_cached_response():
    a = detect_flatline(_obs(numeric=41.5, hours=72))
    assert a is not None and a.evidence["all_zero"] is False
    assert "cached" in a.detail


def test_any_variation_clears_flatline():
    assert detect_flatline(_obs(numeric=[1.0, 1.0, 1.0, 1.0, 1.0, 1.01],
                                hours=72)) is None


def test_a_flatline_inside_one_session_is_not_yet_evidence():
    """Requiring the window to cross a session boundary is what separates a
    quiet afternoon from a dead feed."""
    assert detect_flatline(_obs(numeric=0.0, hours=4)) is None


def test_categorical_constancy_is_never_a_flatline():
    """A macro regime really can read 'calm' for a month. Alerting on that is
    the noise that teaches an operator to ignore the monitor — coverage, not
    variance, is what catches a blind macro gate."""
    obs = [Observation(feed="macro", ts=T0 + timedelta(hours=12 * i),
                       categorical="calm", coverage=1.0) for i in range(8)]
    assert detect_flatline(obs) is None
    assert analyze_feed(obs, T0 + timedelta(hours=90)) == []


# ---------------------------------------------------------------------------
# degraded — partial provider breakage, judged against the feed's own normal
# ---------------------------------------------------------------------------


def test_coverage_collapse_against_own_median():
    a = detect_degraded(_obs(n=9, coverage=[0.9] * 8 + [0.2]))
    assert a is not None and a.kind == "degraded"
    assert a.evidence["median_coverage"] == 0.9


def test_a_feed_whose_normal_is_low_is_not_degraded_at_that_level():
    """70% is healthy for a CUSIP lookup and broken for a quote feed. Only the
    feed's own history knows which it is, which is why there is no constant."""
    assert detect_degraded(_obs(n=9, coverage=[0.7] * 9)) is None


def test_degraded_needs_a_baseline():
    assert detect_degraded(_obs(n=4, coverage=[0.9, 0.9, 0.9, 0.1])) is None


# ---------------------------------------------------------------------------
# silent — a job that stopped running
# ---------------------------------------------------------------------------


def test_late_against_its_own_learned_cadence():
    obs = _obs(n=6, hours=5)                      # ~1h cadence
    a = detect_silence(obs, T0 + timedelta(hours=10))
    assert a is not None and a.kind == "silent"
    assert a.evidence["median_gap_min"] == 60.0


def test_on_cadence_is_silent_free():
    obs = _obs(n=6, hours=5)
    assert detect_silence(obs, T0 + timedelta(hours=5, minutes=90)) is None


def test_cadence_is_learned_not_declared():
    """A slow feed is not late at an interval that would be late for a fast
    one — so changing a cron cannot silently invalidate the check."""
    slow = _obs(n=6, hours=120)                   # ~24h cadence
    assert detect_silence(slow, T0 + timedelta(hours=140)) is None


# ---------------------------------------------------------------------------
# analyze_feed — one dead provider must produce one alert, not three
# ---------------------------------------------------------------------------


def test_empty_suppresses_the_downstream_symptoms():
    obs = _obs(n=8, hours=72, coverage=0.0, numeric=0.0)
    found = analyze_feed(obs, T0 + timedelta(hours=73))
    assert [a.kind for a in found] == ["empty"]


def test_a_healthy_feed_is_quiet():
    obs = _obs(n=8, hours=72, coverage=0.95,
               numeric=[10.0, 11.5, 9.2, 12.1, 10.8, 11.9, 10.4, 12.6])
    assert analyze_feed(obs, T0 + timedelta(hours=73)) == []


def test_no_observations_is_not_an_anomaly():
    assert analyze_feed([], T0) == []


# ---------------------------------------------------------------------------
# never_fires — the inverse failure
# ---------------------------------------------------------------------------


def _decisions(n, action, blocked, *, days=90):
    step = timedelta(days=days / max(n - 1, 1))
    return [(T0 + step * i, action, list(blocked)) for i in range(n)]


def test_a_gate_that_never_opened_reports_its_binding_condition():
    a = detect_never_fires(_decisions(60, "hold", ["regime>=3", "confluence>=5"]),
                           firing_actions={"accumulate"}, feed="accumulation_gate")
    assert a is not None and a.kind == "never_fires"
    assert a.evidence["blockers"]["regime>=3"] == 60
    assert "regime>=3 (60/60)" in a.detail


def test_a_gate_that_fired_even_once_is_working_as_designed():
    d = _decisions(59, "hold", ["regime>=3"])
    d.append((T0 + timedelta(days=91), "accumulate", []))
    assert detect_never_fires(d, firing_actions={"accumulate"},
                              feed="accumulation_gate") is None


def test_a_short_sample_is_not_enough_to_judge_a_gate():
    """A gate sitting closed for a fortnight is a gate doing its job."""
    assert detect_never_fires(_decisions(10, "hold", ["regime>=3"], days=10),
                              firing_actions={"accumulate"},
                              feed="accumulation_gate") is None


def test_never_fires_is_informational_not_an_alarm():
    """It reports a rule worth reviewing, not a system fault — paging on it
    would be crying wolf about a deliberate design choice."""
    a = detect_never_fires(_decisions(60, "hold", ["regime>=3"]),
                           firing_actions={"accumulate"}, feed="accumulation_gate")
    assert a.level == "info"
