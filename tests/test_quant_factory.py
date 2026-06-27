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

from api.app.research import graph_features as gf
from api.app.research.features import cross_sectional_z
from api.app.research.labeler import forward_return
from api.app.research import feature_research as fr


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
