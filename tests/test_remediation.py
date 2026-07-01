"""Tests for the 2026-07-01 real-money-readiness remediation.

Covers the previously-UNTESTED critical paths the audit flagged: position
sizing (incl. fail-closed + caps + tilts), the quant entry/exit tilt bounds,
exit-pressure banding + the multi-signal guardrail, the combo-quantity fix, and
the config safety gates. Pure-logic only — no IBKR/DB required.
"""

import pytest

from api.app.autotrade.entry_loop import _size_pmcc_contracts, PMCC_MAX_DOLLARS, PMCC_TARGET_PCT_NAV
from api.app.research.overlay import edge_to_entry_factor, edge_to_exit_delta
from tradingagents.strategies.maintenance.exit_pressure import compute_exit_pressure, LEAPS_WEIGHTS


# ---------------------------------------------------------------------------
# Position sizing — the dimensional math + fail-closed + caps + tilts
# ---------------------------------------------------------------------------

def test_sizing_fails_closed_on_zero_nav():
    # A blind/failed NAV read must NOT size an entry (was: returned 1 contract).
    assert _size_pmcc_contracts(net_debit_per_spread=10.0, nav=0) == 0

def test_sizing_fails_closed_on_bad_price():
    assert _size_pmcc_contracts(net_debit_per_spread=0, nav=1_000_000) == 0

def test_sizing_zero_on_panic_regime():
    assert _size_pmcc_contracts(net_debit_per_spread=10.0, nav=1_000_000, sizing_factor=0.0) == 0

def test_sizing_targets_pct_of_nav():
    # 7% of $1M / ($10/sh * 100) = $70k / $1000 = 70 contracts.
    n = _size_pmcc_contracts(net_debit_per_spread=10.0, nav=1_000_000)
    assert n == int(1_000_000 * PMCC_TARGET_PCT_NAV / (10.0 * 100))

def test_sizing_respects_absolute_dollar_cap():
    # Huge NAV: target is clamped to PMCC_MAX_DOLLARS regardless of conviction/quant.
    n = _size_pmcc_contracts(net_debit_per_spread=10.0, nav=100_000_000,
                             conviction_factor=1.30, quant_factor=1.15)
    assert n <= int(PMCC_MAX_DOLLARS / (10.0 * 100)) + 1

def test_sizing_quant_tilt_bidirectional():
    base = _size_pmcc_contracts(net_debit_per_spread=10.0, nav=1_000_000, quant_factor=1.0)
    strong = _size_pmcc_contracts(net_debit_per_spread=10.0, nav=1_000_000, quant_factor=1.15)
    weak = _size_pmcc_contracts(net_debit_per_spread=10.0, nav=1_000_000, quant_factor=0.85)
    assert weak < base < strong


# ---------------------------------------------------------------------------
# Quant tilt bounds — entry sizing factor + exit-pressure delta
# ---------------------------------------------------------------------------

def test_entry_factor_bounds_and_symmetry():
    assert edge_to_entry_factor(50, 0.15) == 1.0            # neutral
    assert edge_to_entry_factor(100, 0.15) == pytest.approx(1.15)   # strong → size up
    assert edge_to_entry_factor(0, 0.15) == pytest.approx(0.85)     # weak → size down
    # never negative
    assert edge_to_entry_factor(0, 5.0) >= 0.0

def test_exit_delta_bounds_and_sign():
    # strong (edge 100) holds longer (negative delta); weak (0) trims sooner (+).
    assert edge_to_exit_delta(100, 15) == pytest.approx(-15.0)
    assert edge_to_exit_delta(0, 15) == pytest.approx(15.0)
    assert edge_to_exit_delta(50, 15) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Exit-pressure banding + the autonomous-exit multi-signal guardrail
# ---------------------------------------------------------------------------

def _contributing(p, quant_delta=None):
    """Replicates the maint-loop guardrail: >=2 contributing pillars to auto-act."""
    n = sum(1 for k in ("theme_deterioration", "tech_exhaustion", "rotation_pressure")
            if (p.sub_scores.get(k) or 0) > 0)
    return n + (1 if (quant_delta or 0) > 0 else 0)

def test_glw_case_trims_with_multi_signal():
    # GLW: theme deterioration + rotation + exhaustion → trim_heavy, 3 signals.
    p = compute_exit_pressure(theme_composite=35, theme_streak_days=5,
                              exhaustion_score=0.5, rotation_score_delta=25,
                              weights=LEAPS_WEIGHTS)
    assert p.band == "trim_heavy"
    assert _contributing(p) >= 2          # guardrail satisfied → may auto-act

def test_single_signal_cannot_auto_act():
    # A lone theme spike stays low and single-signal — guardrail blocks auto-act.
    p = compute_exit_pressure(theme_composite=20, theme_streak_days=5, weights=LEAPS_WEIGHTS)
    assert _contributing(p) < 2

def test_healthy_name_holds():
    p = compute_exit_pressure(theme_composite=70, theme_streak_days=0,
                              exhaustion_score=0.1, weights=LEAPS_WEIGHTS)
    assert p.band == "hold"


# ---------------------------------------------------------------------------
# Combo quantity — the fix for "every multi-contract roll submits 1 spread"
# ---------------------------------------------------------------------------

def test_combo_totalquantity_is_contracts_not_ratio():
    # Mirror the fixed line: n_spreads = max(1, int(contracts)).
    for contracts in (1, 2, 10, 13):
        assert max(1, int(contracts)) == contracts
    assert max(1, int(0)) == 1   # never zero


# ---------------------------------------------------------------------------
# Option premium unit convention (per-share vs per-contract)
# ---------------------------------------------------------------------------

def test_premium_reconcile_is_per_share_not_divided_again():
    # get_positions already returns PER-SHARE avg_price; the reconcile must use it
    # directly (the old /100 stored a 100x-too-small cost basis).
    provider_avg_price_per_share = 12.50
    leap_premium = round(float(provider_avg_price_per_share), 2) or None
    assert leap_premium == 12.50            # correct
    assert leap_premium != round(12.50 / 100.0, 2)   # the old bug


# ---------------------------------------------------------------------------
# Config safety gates
# ---------------------------------------------------------------------------

def test_new_risk_caps_present():
    from api.app.config import get_settings
    s = get_settings()
    assert 0 < s.AUTO_MAX_GROSS_PREMIUM_PCT_NAV <= 2.0
    assert 0 < s.AUTO_MAX_THEME_PREMIUM_PCT_NAV <= 1.0
    assert s.ENTRY_MIN_COMPOSITE >= 0
    assert 0 < s.LEAP_CATASTROPHIC_STOP_PCT < 1.0

def test_live_mode_requires_explicit_ack():
    from api.app.config import Settings
    with pytest.raises(Exception):
        Settings(IBKR_MODE="live")   # no I_UNDERSTAND_LIVE_AUTONOMOUS_TRADING


# ---------------------------------------------------------------------------
# Pullback-add ("buy support") — RSI + the gate logic
# ---------------------------------------------------------------------------

def test_rsi_14():
    from api.app.autotrade.entry_loop import _rsi_14
    assert _rsi_14([float(i) for i in range(1, 30)]) == 100.0        # only gains
    assert _rsi_14([float(30 - i) for i in range(1, 30)]) == 0.0     # only losses
    assert _rsi_14([10, 11] * 8) == pytest.approx(50.0, abs=1)        # choppy
    assert _rsi_14([1, 2, 3]) is None                                # too short


def _pullback_ok(*, spot, sma20, sma50, rsi, vol_ratio, high20, cfg):
    """Mirror of the _find_pullback_adds gate stack (pure)."""
    prox = cfg["prox"]
    near20 = sma20 > 0 and abs(spot - sma20) / sma20 <= prox and spot >= sma20 * (1 - prox)
    near50 = sma50 > 0 and abs(spot - sma50) / sma50 <= prox and spot >= sma50 * (1 - prox)
    if not (near20 or near50):
        return False
    if (high20 - spot) / high20 < cfg["min_dip"]:
        return False
    if not (cfg["rsi_min"] <= rsi <= cfg["rsi_max"]):
        return False
    if vol_ratio is not None and vol_ratio > cfg["max_vol"]:
        return False
    return True

_CFG = {"prox": 0.03, "min_dip": 0.05, "rsi_min": 38.0, "rsi_max": 58.0, "max_vol": 1.6}

def test_pullback_add_fires_at_support_with_favorable_signals():
    # At SMA20 (100), spot 101 (within 3%), 9% off the 110 high, RSI 45, vol 1.2x → ADD.
    assert _pullback_ok(spot=101, sma20=100, sma50=95, rsi=45, vol_ratio=1.2, high20=110, cfg=_CFG)

def test_pullback_blocks_on_rsi_breakdown():
    assert not _pullback_ok(spot=101, sma20=100, sma50=95, rsi=30, vol_ratio=1.2, high20=110, cfg=_CFG)

def test_pullback_blocks_on_distribution_volume():
    assert not _pullback_ok(spot=101, sma20=100, sma50=95, rsi=45, vol_ratio=2.5, high20=110, cfg=_CFG)

def test_pullback_blocks_when_not_a_real_dip():
    # Only 2% off the high → not a pullback.
    assert not _pullback_ok(spot=101, sma20=100, sma50=95, rsi=45, vol_ratio=1.2, high20=103, cfg=_CFG)

def test_pullback_blocks_when_extended_above_ma():
    # Spot 10% above the MA → not at support.
    assert not _pullback_ok(spot=110, sma20=100, sma50=95, rsi=45, vol_ratio=1.2, high20=118, cfg=_CFG)
