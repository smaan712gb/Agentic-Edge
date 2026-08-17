"""Offline unit tests for the sector-dispersion IC harness — no network, no DB.

The point of these tests is recovery-and-null: feed the harness a synthetic
panel with a *known* planted relationship and confirm it finds it at the right
sign and strength, then feed it pure noise and confirm it reports nothing. A
harness that cannot do both is not evidence about the real market either way —
a sign error or a look-ahead bug would otherwise show up as "an edge".

Run with `pytest -q tests/test_sector_dispersion.py`.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from api.app.research import sector_dispersion_ic as sd


SECTORS = list(sd.SECTORS)
TRUE_BETA = {tk: b for tk, b in zip(SECTORS, np.linspace(0.6, 1.5, len(SECTORS)))}
# Per-sector idiosyncratic vol, deliberately unequal — the z-standardiser has to
# make a 1% SMH gap comparable to a 1% XLU gap, and equal vols would hide a bug.
IDIO_VOL = {tk: v for tk, v in zip(SECTORS, np.linspace(0.003, 0.012, len(SECTORS)))}


def synth_panel(n_sessions: int = 300, carry: float = 0.0, seed: int = 7) -> pd.DataFrame:
    """Synthetic returns panel.

    `carry` is the planted continuation: the fraction of a sector's overnight
    *idiosyncratic* move that persists into the session. carry=0 is the null.
    """
    rng = np.random.default_rng(seed)
    sessions = [date(2024, 1, 1) + timedelta(days=i) for i in range(n_sessions)]
    r_on_mkt = rng.normal(0, 0.005, n_sessions)
    r_fwd_mkt = {h: rng.normal(0, 0.008, n_sessions) for h in sd.HORIZONS}

    recs: list[dict] = []
    for i, s in enumerate(sessions):
        for tk in SECTORS:
            eps_on = rng.normal(0, IDIO_VOL[tk])
            rec = {
                "session": s, "ticker": tk,
                "r_on": TRUE_BETA[tk] * r_on_mkt[i] + eps_on,
            }
            for h in sd.HORIZONS:
                eps_fwd = carry * eps_on + rng.normal(0, 0.006)
                rec[f"r_fwd_{h}"] = TRUE_BETA[tk] * r_fwd_mkt[h][i] + eps_fwd
            recs.append(rec)
        mkt = {"session": s, "ticker": sd.MARKET, "r_on": r_on_mkt[i]}
        for h in sd.HORIZONS:
            mkt[f"r_fwd_{h}"] = r_fwd_mkt[h][i]
        recs.append(mkt)
    return pd.DataFrame.from_records(recs)


def pipeline(panel: pd.DataFrame) -> pd.DataFrame:
    return sd.build_signal(sd.residualise(panel))


# ------------------------------------------------------------------ primitives
def test_at_or_before_picks_last_print_and_reports_staleness():
    day = pd.DataFrame({"hm": [480, 540, 555, 570], "close": [10.0, 11.0, 12.0, 13.0]})
    px, stale = sd._at_or_before(day, 560, max_staleness=45)
    assert px == 12.0 and stale == 5


def test_at_or_before_rejects_stale_print():
    """An 08:00 print standing in for an 09:20 price is not a premarket signal."""
    day = pd.DataFrame({"hm": [480], "close": [10.0]})
    px, stale = sd._at_or_before(day, 560, max_staleness=45)
    assert px != px and stale == 80  # NaN


def test_at_or_before_handles_no_prints():
    px, stale = sd._at_or_before(pd.DataFrame({"hm": [], "close": []}), 560,
                                 max_staleness=45)
    assert px != px and stale == 10**6


def test_build_returns_are_logs_of_the_right_pair():
    panel = pd.DataFrame([{
        "session": date(2024, 1, 2), "ticker": "XLK", "prev_close": 100.0,
        "px_0920": 101.0, "px_0933": 102.0, "px_1030": 103.0, "px_1550": 104.0,
    }])
    out = sd.build_returns(panel)
    assert out["r_on"].iloc[0] == pytest.approx(np.log(101 / 100))
    assert out["r_fwd_1550"].iloc[0] == pytest.approx(np.log(104 / 102))


def test_rolling_beta_is_causal():
    """Day t's beta must not be computed from day t's own return."""
    rng = np.random.default_rng(3)
    mkt = pd.Series(rng.normal(0, 0.01, 200))
    sec = 1.3 * mkt + rng.normal(0, 0.001, 200)
    beta = sd._rolling_beta(sec, mkt, sd.BETA_WINDOW)
    assert beta.iloc[: sd.BETA_WINDOW // 2].isna().all()
    # Shrunk toward 1.0, so it lands between the raw beta and 1.
    tail = beta.dropna().iloc[-1]
    assert 1.0 < tail < 1.3


def test_residualise_strips_market_beta():
    """Residual overnight returns should be near-uncorrelated with the market."""
    df = pipeline(synth_panel(n_sessions=300, carry=0.0))
    wide = df.pivot(index="session", columns="ticker", values="resid_r_on")
    mkt = df.pivot(index="session", columns="ticker", values="r_on")[sd.MARKET]
    for tk in SECTORS:
        pair = pd.concat([wide[tk], mkt], axis=1).dropna()
        assert abs(pair.corr().iloc[0, 1]) < 0.20, f"{tk} residual still tracks market"


def test_signal_z_is_scale_free_across_unequal_vol_sectors():
    """The z-scores of a high-vol and a low-vol sector must be comparable."""
    df = pipeline(synth_panel(n_sessions=300, carry=0.0))
    sds = df.groupby("ticker")["signal_z"].std().dropna()
    assert sds.max() / sds.min() < 1.5, "standardisation failed to equalise scale"


# --------------------------------------------------------------- recovery/null
def test_recovers_a_planted_signal():
    df = pipeline(synth_panel(n_sessions=300, carry=0.6, seed=11))
    res = sd.analyse_horizon(df, "1550")
    assert res.n_days > 200
    assert res.mean_ic > 0.15, f"planted signal not recovered (IC={res.mean_ic:.3f})"
    assert res.ic_tstat > 4
    assert res.spread_bps_gross > 0
    assert res.spread_tstat > 3


def test_null_panel_produces_no_signal():
    df = pipeline(synth_panel(n_sessions=300, carry=0.0, seed=23))
    res = sd.analyse_horizon(df, "1550")
    assert abs(res.mean_ic) < 0.05, f"false positive on noise (IC={res.mean_ic:.3f})"
    assert abs(res.ic_tstat) < 3


def test_sign_convention_reversal_signal_reads_negative():
    """A planted *reversal* must come back with a negative IC, not a positive one.

    This is the test that catches a sign error — the overnight-reversal
    literature predicts exactly this shape, so reading it as +IC would invert
    the strategy's whole thesis.
    """
    df = pipeline(synth_panel(n_sessions=300, carry=-0.6, seed=31))
    res = sd.analyse_horizon(df, "1550")
    assert res.mean_ic < -0.15
    assert res.spread_bps_gross < 0


def test_cost_is_subtracted_from_the_gross_spread():
    df = pipeline(synth_panel(n_sessions=300, carry=0.6, seed=11))
    res = sd.analyse_horizon(df, "1550")
    expected = sd.COST_BPS_ONE_WAY * sd.LEGS_PER_ROUND_TRIP
    assert res.spread_bps_gross - res.spread_bps_net == pytest.approx(expected, abs=1e-6)
    assert res.sharpe_net < res.sharpe_gross


def test_beta_drift_is_reported_for_a_dollar_neutral_book():
    """With betas spread 0.6..1.5 and a momentum-ish sort, the naive book should
    carry a non-zero net beta — the quantity that tells you how much of a raw
    edge was market direction."""
    df = pipeline(synth_panel(n_sessions=300, carry=0.6, seed=11))
    res = sd.analyse_horizon(df, "1550")
    assert res.mean_net_beta == res.mean_net_beta  # not NaN
    assert abs(res.mean_net_beta) < 2.0


def test_thin_cross_section_days_are_skipped():
    panel = synth_panel(n_sessions=120, carry=0.0)
    keep = [sd.MARKET] + SECTORS[:4]  # 4 sectors — below MIN_SECTORS_PER_DAY
    res = sd.analyse_horizon(pipeline(panel[panel["ticker"].isin(keep)]), "1550")
    assert res.n_days == 0
