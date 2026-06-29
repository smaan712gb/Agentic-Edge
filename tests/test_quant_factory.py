"""Offline unit tests for the Quant Research Factory — no network, no DB.

Covers the deterministic cores: universe-graph features, cross-sectional
standardisation, the pure forward-return labeler math, and the rank-IC /
alpha-decay harness. Run with `pytest -q tests/test_quant_factory.py`.
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

import numpy as np

from api.app.research import graph_features as gf
from api.app.research.features import cross_sectional_z
from api.app.research.labeler import forward_return
from api.app.research import feature_research as fr
from api.app.research import personas as pz
from api.app.research import ml_ranker as mlr
from api.app.research import montecarlo as mc
from api.app.research import event_study as es
from api.app.research import impact_graph as ig
from api.app.research import ner
from api.app.research import overlay as ov


# ---------------------------------------------------------------------------
# Graph features (the 'one graph, not 17 themes' core)
# ---------------------------------------------------------------------------

# A miniature of the real overlap: X sits in 3 themes (a central node like ETN),
# Y and Z in 1 each. Mixed case + a blank verify cleaning.
UNIVERSE = {"A": ["x", "Y"], "B": ["X", "Z"], "C": ["X", ""]}


def test_membership_dedupes_and_uppercases():
    m = gf.build_membership(UNIVERSE)
    assert m["X"] == {"A", "B", "C"}
    assert m["Y"] == {"A"}
    assert "" not in m


def test_theme_counts_and_centrality():
    m = gf.build_membership(UNIVERSE)
    counts = gf.theme_counts(m)
    assert counts == {"X": 3, "Y": 1, "Z": 1}
    cent = gf.centrality(m)
    assert cent["X"] == 1.0                       # busiest node normalises to 1
    assert math.isclose(cent["Y"], 1 / 3, rel_tol=1e-6)


def test_co_membership_degree():
    m = gf.build_membership(UNIVERSE)
    deg = gf.co_membership_degree(m)
    assert deg["X"] == 2                          # neighbours Y (via A) + Z (via B)
    assert deg["Y"] == 1                          # only X
    assert deg["Z"] == 1


def test_compute_graph_features_shape():
    out = gf.compute_graph_features(UNIVERSE)
    assert set(out["X"]) == {
        "theme_count", "theme_centrality", "co_membership_degree",
        "avg_theme_size", "themes",
    }
    assert out["X"]["themes"] == ["A", "B", "C"]
    # X is in 3 themes of avg size 2 (A:{X,Y}=2, B:{X,Z}=2, C:{X}=1 -> 5/3).
    assert math.isclose(out["X"]["avg_theme_size"], 5 / 3, rel_tol=1e-3)


def test_empty_universe_is_safe():
    assert gf.compute_graph_features({}) == {}
    assert gf.centrality({}) == {}


# ---------------------------------------------------------------------------
# Cross-sectional standardisation
# ---------------------------------------------------------------------------


def test_cross_sectional_z_standardises():
    rows = {"A": {"f": 1.0}, "B": {"f": 2.0}, "C": {"f": 3.0}}
    cross_sectional_z(rows, keys=("f",))
    # mean 2, population std sqrt(2/3) ≈ 0.8165
    assert math.isclose(rows["B"]["z_f"], 0.0, abs_tol=1e-9)
    assert rows["A"]["z_f"] < 0 < rows["C"]["z_f"]
    assert math.isclose(rows["A"]["z_f"], -rows["C"]["z_f"], rel_tol=1e-6)


def test_cross_sectional_z_missing_and_degenerate():
    # Missing raw value -> None; all-equal -> 0.0 (no phantom rank).
    rows = {"A": {"f": 5.0}, "B": {"f": 5.0}, "C": {}}
    cross_sectional_z(rows, keys=("f",))
    assert rows["A"]["z_f"] == 0.0
    assert rows["B"]["z_f"] == 0.0
    assert rows["C"]["z_f"] is None


# ---------------------------------------------------------------------------
# Forward-return labeler (pure math)
# ---------------------------------------------------------------------------


def _series(start: date, prices: list[float]) -> list[tuple[date, float]]:
    from datetime import timedelta
    return [(start + timedelta(days=i), p) for i, p in enumerate(prices)]


def test_forward_return_complete_window():
    # 11 daily prices 100..110; horizon 5 from day0 -> last in (d0, d0+5] is 105.
    s = _series(date(2026, 1, 1), [100 + i for i in range(11)])
    ret, complete = forward_return(s, date(2026, 1, 1), horizon_days=5)
    assert complete is True
    assert math.isclose(ret, 105 / 100 - 1.0, rel_tol=1e-9)


def test_forward_return_incomplete_window():
    # Series ends before t0 + horizon -> window not complete.
    s = _series(date(2026, 1, 1), [100, 101, 102])   # only 3 days
    ret, complete = forward_return(s, date(2026, 1, 1), horizon_days=20)
    assert complete is False


def test_forward_return_anchor_is_last_at_or_before_t0():
    s = _series(date(2026, 1, 1), [100 + i for i in range(11)])
    # t0 = day index 2 (price 102); +3 -> price at day5 = 105.
    ret, complete = forward_return(s, date(2026, 1, 3), horizon_days=3)
    assert complete is True
    assert math.isclose(ret, 105 / 102 - 1.0, abs_tol=1e-5)   # labeler rounds to 5dp


def test_forward_return_empty_series():
    assert forward_return([], date(2026, 1, 1), 5) == (None, False)


# ---------------------------------------------------------------------------
# Rank IC + alpha decay
# ---------------------------------------------------------------------------


def test_spearman_ic_monotonic():
    assert math.isclose(fr.spearman_ic([1, 2, 3, 4], [10, 20, 30, 40]), 1.0, rel_tol=1e-9)
    assert math.isclose(fr.spearman_ic([1, 2, 3, 4], [40, 30, 20, 10]), -1.0, rel_tol=1e-9)


def test_spearman_ic_degenerate_is_nan():
    assert math.isnan(fr.spearman_ic([1, 1, 1], [1, 2, 3]))
    assert math.isnan(fr.spearman_ic([1, 2], []))


def test_ic_table_and_alpha_decay_fast_signal():
    # feat perfectly predicts the 5d return, poorly the 60d -> 'fast' decay.
    df = pd.DataFrame({
        "symbol": list("ABCDE"),
        "as_of": pd.to_datetime(["2026-01-01"] * 5, utc=True),
        "label_status": ["final"] * 5,
        "feat": [1.0, 2.0, 3.0, 4.0, 5.0],
        "fwd_ret_5d": [1.0, 2.0, 3.0, 4.0, 5.0],
        "fwd_ret_20d": [1.0, 2.0, 3.0, 4.0, 5.0],
        "fwd_ret_60d": [5.0, 1.0, 4.0, 2.0, 3.0],
    })
    table = fr.compute_ic_table(df)
    assert "feat" in table
    assert math.isclose(table["feat"][5]["ic"], 1.0, rel_tol=1e-9)
    assert abs(table["feat"][60]["ic"]) < abs(table["feat"][5]["ic"])

    decay = fr.alpha_decay(table)
    assert decay["feat"]["verdict"] == "fast"
    assert decay["feat"]["peak_horizon"] == 5


def test_feature_columns_excludes_labels_and_ids():
    df = pd.DataFrame({
        "symbol": ["A"], "as_of": pd.to_datetime(["2026-01-01"], utc=True),
        "label_status": ["final"], "feat": [1.0],
        "fwd_ret_5d": [0.1], "fwd_ret_20d": [0.2], "fwd_ret_60d": [0.3],
    })
    assert fr.feature_columns(df) == ["feat"]


# ---------------------------------------------------------------------------
# Layer 8a — manager personas
# ---------------------------------------------------------------------------


def test_persona_neutral_when_features_absent():
    # No weighted features present -> neutral 50.
    assert pz.score_symbol({}, "aschenbrenner") == 50.0


def test_persona_rewards_aligned_features():
    strong = {"z_theme_centrality": 2.0, "z_co_membership_degree": 2.0,
              "z_smartmoney_theme_confirm": 2.0, "z_momentum_60d": 2.0}
    weak = {"z_theme_centrality": -2.0, "z_co_membership_degree": -2.0,
            "z_smartmoney_theme_confirm": -2.0, "z_momentum_60d": -2.0}
    assert pz.score_symbol(strong, "aschenbrenner") > 80
    assert pz.score_symbol(weak, "aschenbrenner") < 20


def test_persona_activist_likes_below_trend():
    # Negative z_dist_50dma (below 50dma) should HELP the activist score.
    below = pz.score_symbol({"z_dist_50dma": -2.0, "z_smartmoney_theme_confirm": 1.0}, "activist_value")
    above = pz.score_symbol({"z_dist_50dma": 2.0, "z_smartmoney_theme_confirm": 1.0}, "activist_value")
    assert below > above


def test_persona_score_all_and_meta():
    scores = pz.score_all({"z_theme_centrality": 1.0})
    assert set(scores) == set(pz.PERSONAS)
    assert all(0.0 <= v <= 100.0 for v in scores.values())
    assert {m["key"] for m in pz.persona_meta()} == set(pz.PERSONAS)


def test_persona_unknown_raises():
    with pytest.raises(KeyError):
        pz.score_symbol({}, "not_a_persona")


# ---------------------------------------------------------------------------
# Layer 5 — ML ranker (pure numeric core)
# ---------------------------------------------------------------------------


def test_ridge_recovers_linear_ranking():
    rng = np.random.default_rng(0)
    X = rng.standard_normal((200, 3))
    beta = np.array([2.0, -1.0, 0.5])
    y = X @ beta + 0.01 * rng.standard_normal(200)
    _, mean, std = mlr.standardize(X)
    coef, intercept = mlr.ridge_fit(X, y, alpha=1.0)
    pred = mlr.ridge_predict(X, coef, intercept, mean, std)
    # Predictions rank-correlate near-perfectly with the true signal.
    truth = X @ beta
    assert fr.spearman_ic(pred, truth) > 0.99


def test_standardize_handles_zero_variance():
    X = np.array([[1.0, 5.0], [1.0, 7.0], [1.0, 9.0]])   # col 0 constant
    Xs, mean, std = mlr.standardize(X)
    assert np.isfinite(Xs).all()        # no div-by-zero blowup
    assert std[0] == 1.0


def test_rank_percentiles_bounds():
    pct = mlr.rank_percentiles(np.array([10.0, 20.0, 30.0]))
    assert pct.min() == 0.0 and pct.max() == 1.0


# ---------------------------------------------------------------------------
# Layer 6 — event study (pure CAR core)
# ---------------------------------------------------------------------------


def test_market_adjusted_car_subtracts_benchmark():
    sym = _series(date(2026, 1, 1), [100 + 2 * i for i in range(11)])   # +20% over 10d
    bench = _series(date(2026, 1, 1), [100 + i for i in range(11)])     # +10% over 10d
    car = es.market_adjusted_car(sym, bench, date(2026, 1, 1), 5)
    # sym 5d: 110/100-1=0.10 ; bench 5d: 105/100-1=0.05 ; CAR≈0.05
    assert math.isclose(car, 0.05, abs_tol=1e-4)


def test_aggregate_cars_stats():
    agg = es.aggregate_cars([0.1, -0.1, 0.2, 0.0])
    assert agg["n"] == 4
    assert math.isclose(agg["mean_car"], 0.05, abs_tol=1e-9)
    assert agg["win_rate"] == 0.5    # 2 of 4 strictly > 0


def test_equal_weight_index_rebases():
    series = {"A": _series(date(2026, 1, 1), [10, 11, 12]),
              "B": _series(date(2026, 1, 1), [100, 110, 120])}
    idx = es.build_equal_weight_index(series)
    # Both +10%/+20% -> index starts 100 and both move together.
    assert math.isclose(idx[0][1], 100.0, abs_tol=1e-6)
    assert idx[-1][1] > idx[0][1]


# ---------------------------------------------------------------------------
# Layer 7 — Monte Carlo (pure, seeded)
# ---------------------------------------------------------------------------


def test_gbm_is_deterministic_with_seed():
    a = mc.simulate_gbm(100.0, 0.0005, 0.02, 20, 500, seed=7)
    b = mc.simulate_gbm(100.0, 0.0005, 0.02, 20, 500, seed=7)
    assert np.array_equal(a, b)
    assert a.shape == (500, 21)
    assert np.allclose(a[:, 0], 100.0)


def test_gbm_positive_drift_lifts_mean_terminal():
    up = mc.terminal_stats(mc.simulate_gbm(100.0, 0.003, 0.01, 40, 5000, seed=1))
    flat = mc.terminal_stats(mc.simulate_gbm(100.0, 0.0, 0.01, 40, 5000, seed=1))
    assert up["mean_return"] > flat["mean_return"]
    assert -1.0 <= flat["mean_return"] <= 1.0


def test_sizing_is_bounded_and_conservative():
    s = mc.suggested_weight(0.002, 0.02, 20)
    assert 0.0 <= s["suggested"] <= mc.MAX_WEIGHT
    # Zero edge -> zero Kelly contribution.
    assert mc.kelly_fraction(0.0, 0.02, 20) == 0.0


def test_prob_breach_drawdown_monotonic():
    paths = mc.simulate_gbm(100.0, 0.0, 0.03, 30, 4000, seed=3)
    shallow = mc.prob_breach_drawdown(paths, -0.05)
    deep = mc.prob_breach_drawdown(paths, -0.20)
    assert shallow >= deep        # easier to breach a shallow drawdown


# ---------------------------------------------------------------------------
# Layer 8b — impact graph + NER
# ---------------------------------------------------------------------------

IMPACT_UNIVERSE = {"t1": ["X", "Y"], "t2": ["X", "Z"], "t3": ["Z", "W"]}


def test_build_graph_edges_from_shared_themes():
    nodes, edges = ig.build_graph(IMPACT_UNIVERSE)
    assert set(nodes) == {"X", "Y", "Z", "W"}
    assert edges[("X", "Y")] == 1.0     # share t1
    assert edges[("X", "Z")] == 1.0     # share t2
    assert ("Y", "W") not in edges       # not connected


def test_propagate_impact_decays_with_distance():
    nodes, edges = ig.build_graph(IMPACT_UNIVERSE)
    # Shock X only. Y/Z are 1 hop, W is 2 hops -> strictly decaying impact.
    impact = ig.propagate_impact(nodes, edges, {"X": 1.0}, damping=0.5, steps=10)
    assert impact["X"] > impact["Z"] > impact["W"]
    assert impact["W"] > 0.0             # still reaches two hops out


def test_build_graph_folds_extra_edges():
    nodes, edges = ig.build_graph(IMPACT_UNIVERSE, extra_edges=[{"source": "Y", "target": "W", "weight": 3.0}])
    assert edges[("W", "Y")] == 3.0


def test_ner_extracts_chokepoints_and_tickers():
    text = "TSMC said CoWoS advanced packaging is sold out; MU and NVDA benefit."
    ents = ner.extract_entities(text, universe_symbols={"MU", "NVDA", "AAPL"})
    assert "cowos" in ents["chokepoints"]
    assert "advanced packaging" in ents["chokepoints"]
    assert ents["tickers"] == ["MU", "NVDA"]   # AAPL not in text


def test_ner_word_boundary_no_false_ticker():
    # 'MUSEUM' must not surface ticker 'MU'.
    ents = ner.extract_entities("the MUSEUM opened", universe_symbols={"MU"})
    assert ents["tickers"] == []


# ---------------------------------------------------------------------------
# Quant overlay — autonomous wiring (pure cores)
# ---------------------------------------------------------------------------


def test_blended_weights_cold_start_is_prior():
    # No labels (n=0) -> weight stays at the theory prior, for every signal.
    prior = {"z_theme_centrality": 1.0, "z_momentum_60d": 0.4}
    ic = {"z_theme_centrality": 0.2, "z_momentum_60d": 0.1}
    n0 = {"z_theme_centrality": 0, "z_momentum_60d": 0}
    assert ov.blended_weights(prior, ic, n0) == prior


def test_blended_weights_shifts_toward_measured_ic():
    # With lots of labelled data, a strong positive IC pulls the weight UP off a
    # small prior; NaN IC stays on prior regardless of n.
    prior = {"a": 0.2, "b": 0.5}
    ic = {"a": 0.3, "b": float("nan")}
    n = {"a": 100000, "b": 100000}
    w = ov.blended_weights(prior, ic, n, k=ov.SHRINKAGE_K)
    assert w["a"] > prior["a"]            # strong IC lifts it
    assert w["b"] == 0.5                  # NaN IC -> untouched prior


def test_blended_weights_negative_ic_floors_at_zero():
    # A signal that anti-predicts gets driven toward 0, never negative.
    w = ov.blended_weights({"a": 0.5}, {"a": -0.3}, {"a": 100000})
    assert 0.0 <= w["a"] < 0.5


def test_quant_edge_strong_weak_neutral():
    weights = {"z_theme_centrality": 1.0, "z_smartmoney_theme_confirm": 0.8}
    strong = ov.quant_edge({"z_theme_centrality": 2.5, "z_smartmoney_theme_confirm": 2.0}, weights)
    weak = ov.quant_edge({"z_theme_centrality": -2.5, "z_smartmoney_theme_confirm": -2.0}, weights)
    empty = ov.quant_edge({}, weights)
    assert strong["label"] == "STRONG" and strong["score"] > 64
    assert weak["label"] == "WEAK" and weak["score"] < 36
    assert empty["label"] == "NEUTRAL" and empty["score"] == 50.0


def test_quant_edge_normalizes_persona_scale():
    # persona_* is 0..100; 75 should read as a clear positive (~+1 z).
    e_hi = ov.quant_edge({"persona_aschenbrenner": 90.0}, {"persona_aschenbrenner": 1.0})
    e_lo = ov.quant_edge({"persona_aschenbrenner": 10.0}, {"persona_aschenbrenner": 1.0})
    assert e_hi["score"] > 50.0 > e_lo["score"]


def test_format_block_is_human_readable():
    block = ov.format_block("ETN", {"z_theme_centrality": 2.0}, {"z_theme_centrality": 1.0}, "prior")
    assert "QUANT EDGE" in block
    assert "chokepoint centrality" in block
    assert "theory-prior" in block       # cold-start confidence note


def test_edge_to_exit_delta_direction_and_bounds():
    # Strong edge -> negative (hold longer); weak -> positive (trim sooner);
    # neutral -> 0; always within [-max, +max].
    assert ov.edge_to_exit_delta(100.0, 10.0) == -10.0
    assert ov.edge_to_exit_delta(0.0, 10.0) == 10.0
    assert ov.edge_to_exit_delta(50.0, 10.0) == 0.0
    for e in (0, 25, 50, 75, 100):
        assert -10.0 <= ov.edge_to_exit_delta(float(e), 10.0) <= 10.0


# ---------------------------------------------------------------------------
# Entry loop — latest-run-per-theme selection (pure)
# ---------------------------------------------------------------------------


def test_latest_run_per_theme_keeps_freshest():
    from api.app.autotrade.entry_loop import _latest_run_per_theme
    # Pre-sorted newest-first; first run seen per theme wins, others dropped.
    runs = [
        ("r3", "themeA"),   # newest A
        ("r2", "themeB"),   # newest B
        ("r1", "themeA"),   # older A -> ignored
    ]
    out = _latest_run_per_theme(runs)
    assert out == {"themeA": "r3", "themeB": "r2"}


def test_exit_pressure_accepts_quant_delta():
    # The exit-pressure score must move by the quant delta and re-band.
    from tradingagents.strategies.maintenance.exit_pressure import compute_exit_pressure
    base = compute_exit_pressure(theme_composite=5.0, trim_band="none", exhaustion_score=50.0)
    lifted = compute_exit_pressure(theme_composite=5.0, trim_band="none", exhaustion_score=50.0,
                                   quant_edge_delta=+15.0)
    held = compute_exit_pressure(theme_composite=5.0, trim_band="none", exhaustion_score=50.0,
                                 quant_edge_delta=-15.0)
    assert lifted.score > base.score >= held.score
    assert 0.0 <= held.score <= 100.0 and 0.0 <= lifted.score <= 100.0
