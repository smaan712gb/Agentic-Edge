"""Morning Report — pure-logic tests (no DB, no network).

Covers the band→suggested-action mapping (must stay in lockstep with the
exit-pressure bands the maintenance loop emits) and the alert digest
summariser the 08:45 ET cron pushes to the alert channel.
"""

import pytest

from api.app.report.morning import _BAND_ACTION, summarise_for_alert
from tradingagents.strategies.maintenance.exit_pressure import (
    LEAPS_WEIGHTS, compute_exit_pressure,
)


# ---------------------------------------------------------------------------
# Band → action mapping stays in sync with the exit-pressure engine
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("theme_composite,streak", [
    (90.0, 0),    # healthy → hold
    (55.0, 3),    # deteriorating
    (30.0, 6),    # broken
])
def test_every_emitted_band_has_an_action(theme_composite, streak):
    p = compute_exit_pressure(
        theme_composite=theme_composite, theme_streak_days=streak,
        trim_band="none", exhaustion_score=0.8 if theme_composite < 60 else 0.0,
        rotation_score_delta=15.0 if theme_composite < 40 else None,
        weights=LEAPS_WEIGHTS,
    )
    assert p.band in _BAND_ACTION, f"band {p.band!r} has no suggested action"


def test_action_severity_ordering():
    # The mapping must escalate: hold < trim_light < trim_heavy < aggressive.
    assert _BAND_ACTION["hold"] == "Hold"
    assert _BAND_ACTION["trim_light"].startswith("Trim")
    assert _BAND_ACTION["trim_heavy"] == "Trim"
    assert _BAND_ACTION["aggressive"] == "Exit"


# ---------------------------------------------------------------------------
# Alert digest summariser
# ---------------------------------------------------------------------------

def _report(**overrides):
    base = {
        "as_of": "2026-07-09",
        "top_ideas": [
            {"symbol": "CRDO", "composite": 8.2},
            {"symbol": "ANET", "composite": 7.6},
        ],
        "holdings": [
            {"symbol": "MU", "suggested_action": "Hold"},
            {"symbol": "WDC", "suggested_action": "Trim"},
        ],
        "alerts": {
            "entry_breaker": {"tripped": False, "reason": None},
            "rotation_flagged": [],
            "holding_stake_reductions": [],
        },
    }
    base.update(overrides)
    return base


def test_summary_includes_ideas_and_actions():
    text = summarise_for_alert(_report())
    assert "CRDO 8.2" in text
    assert "WDC → Trim" in text
    assert "MU" not in text  # Hold rows are not actions


def test_summary_flags_breaker_rotation_and_reductions():
    text = summarise_for_alert(_report(alerts={
        "entry_breaker": {"tripped": True, "reason": "nav drop"},
        "rotation_flagged": [{"theme_id": "optical-networking"}],
        "holding_stake_reductions": [{"ticker": "WDC", "change_type": "reduce"}],
    }))
    assert "breaker TRIPPED" in text
    assert "optical-networking" in text
    assert "WDC (reduce)" in text


def test_summary_quiet_day():
    text = summarise_for_alert(_report(
        top_ideas=[],
        holdings=[{"symbol": "MU", "suggested_action": "Hold"}],
    ))
    assert text == "No actionable items this morning."


# ---------------------------------------------------------------------------
# Risk-appetite posture (fear/greed dial)
# ---------------------------------------------------------------------------

from api.app.report.morning import compute_posture
from api.app.report.brief import _fallback_brief


def _health(composite, n_scored=10, n_buys=3):
    return {"composite": composite, "n_scored": n_scored, "n_buys": n_buys}


def test_posture_healthy_book_is_risk_on():
    p = compute_posture(
        theme_health=[_health(8.0, 10, 6), _health(7.5, 10, 5)],
        n_themes_flagged=0, n_themes=2, breaker_tripped=False,
    )
    assert p["score"] > 65
    assert p["label"] in ("constructive", "risk-on")
    assert p["components"]["rotation_calm"] == 100.0


def test_posture_broad_rotation_drags_score_down():
    calm = compute_posture(theme_health=[_health(6.5)], n_themes_flagged=0,
                           n_themes=16, breaker_tripped=False)
    stressed = compute_posture(theme_health=[_health(6.5)], n_themes_flagged=14,
                               n_themes=16, breaker_tripped=False)
    assert stressed["score"] < calm["score"] - 30


def test_posture_breaker_caps_at_20():
    p = compute_posture(
        theme_health=[_health(9.0, 10, 9)],
        n_themes_flagged=0, n_themes=1, breaker_tripped=True,
    )
    assert p["score"] == 20.0
    assert p["breaker_capped"] is True
    assert p["label"] == "defensive"


def test_posture_empty_inputs_is_defensive():
    p = compute_posture(theme_health=[], n_themes_flagged=0, n_themes=1,
                        breaker_tripped=False)
    assert p["label"] in ("defensive", "cautious")


# ---------------------------------------------------------------------------
# EMA trend stack — the operator's Trend spec (8/21/50/100/200)
# ---------------------------------------------------------------------------

from api.app.research.trend import ema, ema_stack


def test_ema_of_constant_series_is_the_constant():
    assert ema([50.0] * 300, 21) == pytest.approx(50.0)


def test_perfect_uptrend_scores_100():
    closes = [10.0 * (1.004 ** i) for i in range(320)]   # steady riser
    s = ema_stack(closes)
    assert s is not None
    assert s["score"] == 100.0
    assert s["label"] == "perfect alignment"
    assert all(p["ok"] for p in s["pairs"])
    assert s["price_above_8ema"] is True


def test_perfect_downtrend_scores_0():
    closes = [400.0 * (0.996 ** i) for i in range(320)]  # steady faller
    s = ema_stack(closes)
    assert s is not None
    assert s["score"] == 0.0
    assert s["label"] == "downtrend stack"


def test_recent_reversal_is_partial():
    # Long downtrend then a sharp 60-day rally: fast EMAs flip first, slow lag.
    closes = [400.0 * (0.997 ** i) for i in range(260)]
    closes += [closes[-1] * (1.01 ** i) for i in range(1, 61)]
    s = ema_stack(closes)
    assert s is not None
    assert 0.0 < s["score"] < 100.0


def test_insufficient_history_returns_none():
    assert ema_stack([100.0] * 100) is None


# ---------------------------------------------------------------------------
# Pattern recognition + market structure
# ---------------------------------------------------------------------------

from api.app.research.patterns import bull_pattern_count, detect_patterns, market_structure


def _ohlcv(closes, spread=0.01, vol=1_000_000.0):
    highs = [c * (1 + spread) for c in closes]
    lows = [c * (1 - spread) for c in closes]
    vols = [vol] * len(closes)
    return highs, lows, closes, vols


def test_failed_breakout_detected():
    # Range under 100, pop to 106, collapse back to 96.
    closes = [95.0 + (i % 5) for i in range(100)]     # chops 95-99
    closes += [101.0, 104.0, 106.0]                    # breakout
    closes += [99.0, 97.0, 96.0]                       # failure
    h, l, c, v = _ohlcv(closes)
    keys = {p["key"] for p in detect_patterns(highs=h, lows=l, closes=c, volumes=v)}
    assert "failed_breakout" in keys


def test_clean_riser_has_no_failed_breakout():
    closes = [10.0 * (1.004 ** i) for i in range(260)]
    h, l, c, v = _ohlcv(closes)
    keys = {p["key"] for p in detect_patterns(highs=h, lows=l, closes=c, volumes=v)}
    assert "failed_breakout" not in keys
    assert bull_pattern_count(detect_patterns(highs=h, lows=l, closes=c, volumes=v)) >= 0


def test_flat_base_detected_after_advance():
    closes = [50.0 * (1.006 ** i) for i in range(120)]        # strong advance
    top = closes[-1]
    closes += [top * (1 + 0.02 * ((i % 4) - 1.5) / 10) for i in range(35)]  # tight range
    h, l, c, v = _ohlcv(closes, spread=0.005)
    keys = {p["key"] for p in detect_patterns(highs=h, lows=l, closes=c, volumes=v)}
    assert "flat_base" in keys


def test_market_structure_uptrend_hh_hl():
    # Rising zig-zag: clear higher highs and higher lows.
    closes = []
    for leg in range(8):
        base = 100 + leg * 10
        closes += [base + j for j in range(8)] + [base + 8 - j for j in range(5)]
    h, l, c, v = _ohlcv(closes, spread=0.002)
    ms = market_structure(highs=h, lows=l, closes=c, volumes=v)
    assert ms is not None
    assert ms["structure"] == "uptrend"
    assert ms["higher_highs"] and ms["higher_lows"]


def test_market_structure_needs_history():
    h, l, c, v = _ohlcv([100.0] * 50)
    assert market_structure(highs=h, lows=l, closes=c, volumes=v) is None


# ---------------------------------------------------------------------------
# Perfect Entry Score
# ---------------------------------------------------------------------------

from api.app.report.entry_score import compute_entry_score


def _idea(**over):
    base = {
        "composite": 7.5,
        "rotation_flagged": False,
        "trend": {"score": 100.0, "label": "perfect alignment",
                  "emas": {"ema8": 100.0, "ema21": 97.0}, "price": 100.5},
        "smart_money": {"manager_count": 3, "confirmation": True},
        "technical": {"flow_imbalance": 0.4, "rvol": 0.6},
        "analyst": {"n_up_30d": 2, "n_down_30d": 0, "upside_pct": 15.0},
        "patterns": [{"key": "vcp", "direction": "bullish"},
                     {"key": "flat_base", "direction": "bullish"}],
        "structure": {"distribution_flag": False, "distribution_days_25": 1},
    }
    base.update(over)
    return base


def test_prime_setup_scores_high_with_reasons():
    e = compute_entry_score(_idea())
    assert e["score"] >= 85
    assert e["label"] == "prime setup"
    texts = " | ".join(r["text"] for r in e["reasons"] if r["ok"])
    assert "EMA" in texts and "institutions buying" in texts.lower()
    assert "volume contraction" in texts.lower()
    assert "breakout pending" in texts.lower()


def test_failed_breakout_and_distribution_penalise():
    good = compute_entry_score(_idea())["score"]
    bad = compute_entry_score(_idea(
        patterns=[{"key": "failed_breakout", "direction": "bearish"}],
        structure={"distribution_flag": True, "distribution_days_25": 6},
        rotation_flagged=True,
    ))["score"]
    assert bad <= good - 35


def test_extended_price_earns_no_entry_point():
    e = compute_entry_score(_idea(trend={
        "score": 100.0, "label": "perfect alignment",
        "emas": {"ema8": 100.0, "ema21": 97.0}, "price": 112.0,
    }))
    assert any("extended" in r["text"] for r in e["reasons"] if not r["ok"])


def test_entry_score_empty_idea_is_not_ready():
    e = compute_entry_score({"composite": 0.0})
    assert e["score"] < 50
    assert e["label"] == "not ready"


# ---------------------------------------------------------------------------
# Portfolio risk engine (pure parts)
# ---------------------------------------------------------------------------

from api.app.report.portfolio_risk import _avg_pairwise_correlation, _pearson


def test_pearson_perfect_and_inverse():
    a = [0.01, -0.02, 0.03, -0.01, 0.02]
    assert _pearson(a, a) == pytest.approx(1.0)
    assert _pearson(a, [-x for x in a]) == pytest.approx(-1.0)


def test_corr_haircut_thresholds():
    from api.app.report.portfolio_risk import corr_haircut_factor
    assert corr_haircut_factor(None, high=0.80, med=0.65) == 1.0     # fail-open
    assert corr_haircut_factor(0.30, high=0.80, med=0.65) == 1.0     # diversifying
    assert corr_haircut_factor(0.70, high=0.80, med=0.65) == 0.75    # crowded
    assert corr_haircut_factor(0.90, high=0.80, med=0.65) == 0.5     # same bet
    # a haircut, never a block
    assert corr_haircut_factor(1.0, high=0.80, med=0.65) > 0


def test_effective_bets_formula_matches_high_correlation():
    # 5 names, all perfectly correlated → 1 real bet.
    rets = {s: [0.01, -0.02, 0.03, -0.01, 0.02] * 8 for s in "ABCDE"}
    avg = _avg_pairwise_correlation(rets)
    assert avg == pytest.approx(1.0)
    n = len(rets)
    n_eff = n / (1 + (n - 1) * avg)
    assert n_eff == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Filing rows — plain-words meaning + deterministic impact defaults
# ---------------------------------------------------------------------------

from datetime import datetime, timezone

from api.app.db import AutoAction
from api.app.report.morning import _filing_row, _insider_row


def _action(action_type, symbol, payload):
    return AutoAction(
        loop="maintenance", action_type=action_type, symbol=symbol,
        gate_status="passed", payload=payload,
        timestamp=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
    )


def test_filing_row_translates_form_types():
    row = _filing_row(_action("filing_alert_after_hours", "AXTI", {
        "form_type": "8-K", "severity": "after_hours",
        "rationale": "8-K accepted after hours with financial exhibits",
        "link": "https://sec.gov/x", "is_held": True,
    }))
    assert "material corporate event" in row["meaning"]
    assert row["impact"] == "unclear"
    assert row["is_held"] is True
    assert row["link"] == "https://sec.gov/x"


def test_filing_row_dilution_forms_lean_negative():
    assert _filing_row(_action("filing_alert_high", "X", {"form_type": "S-3"}))["impact"] == "negative"
    assert _filing_row(_action("filing_alert_high", "X", {"form_type": "424B5"}))["impact"] == "negative"
    assert _filing_row(_action("filing_alert_high", "X", {"form_type": "NT 10-K"}))["impact"] == "negative"


def test_filing_row_activist_leans_positive():
    assert _filing_row(_action("filing_alert_high", "X", {"form_type": "SC 13D"}))["impact"] == "positive"


def test_insider_buy_reads_positive_with_headline():
    row = _insider_row(_action("insider_buy_alert", "MU", {
        "direction": "buy", "reporting_name": "Jane Doe",
        "value_usd": 1_200_000, "owner_type": "officer: CEO",
    }))
    assert row["impact"] == "positive"
    assert "Jane Doe bought" in row["headline"]
    assert "$1,200,000" in row["headline"]


def test_insider_sale_reads_neutral():
    row = _insider_row(_action("insider_alert", "MU", {
        "direction": "sale", "reporting_name": "John Roe", "value_usd": 500_000,
    }))
    assert row["impact"] == "neutral"
    assert "sold" in row["headline"]


# ---------------------------------------------------------------------------
# Plain-English fallback brief
# ---------------------------------------------------------------------------

def test_fallback_brief_has_all_four_sections():
    text = _fallback_brief({
        "date": "2026-07-09",
        "account": {"equity": 1_000_000, "cash_pct": 100.0},
        "n_holdings": 0,
        "top_ideas": [{"symbol": "CRDO", "rotation_blocked": True}],
        "holdings_needing_action": [],
        "entry_breaker_tripped": False,
        "themes_rotation_flagged": ["optical-networking"],
        "stake_reductions_on_holdings": [],
        "strongest_themes": [{"theme": "Optical networking", "score_out_of_10": 7.1}],
    })
    for header in ("## Where we stand", "## Our money",
                   "## The plan for today", "## What we're watching"):
        assert header in text
    assert "fully in cash" in text
    assert "CRDO" in text


def test_fallback_brief_breaker_message():
    text = _fallback_brief({
        "entry_breaker_tripped": True, "themes_rotation_flagged": [],
        "account": {}, "top_ideas": [], "holdings_needing_action": [],
        "stake_reductions_on_holdings": [], "strongest_themes": [],
        "n_holdings": 0,
    })
    assert "safety brake" in text


def test_summary_tolerates_error_sections():
    # A failed section arrives as {"error": ...} — the digest must not crash.
    text = summarise_for_alert(_report(
        top_ideas={"error": "boom"},
        holdings={"error": "boom"},
        alerts={"error": "boom"},
    ))
    assert isinstance(text, str)
