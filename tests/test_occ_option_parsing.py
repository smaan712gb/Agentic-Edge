"""Regression test for the OCC option-symbol parse (2026-08-18).

Options flow was read as ZERO across the entire system for every symbol,
silently, because the call/put split used:

    call_prem = sum(... if a.contract.endswith("C"))
    put_prem  = sum(... if a.contract.endswith("P"))

OCC symbols are root + YYMMDD + C|P + 8-digit strike — "NVDA260904C00227500"
ends in the STRIKE, not the right. Both filters matched nothing, so both sums
were exactly 0.0. Nothing errored: the fetch succeeded, the alert list was
populated, and the totals were zero.

Measured on NVDA the day it was found (165 live alerts): the correct parse
gives $52,866,161 of call premium against $9,710,158 of put — a decisively
bullish tape the system had been treating as no data at all.

Three consumers were degraded, each failing quietly toward "no signal":
  * flow_tilt was never once "bullish" anywhere in the universe, so the
    rotation detector's `bearish > bullish` comparison was free — that is why
    flow_distribution tripped on 15 of 17 themes and behaved as a constant.
  * flow_imbalance was always None, so z_flow_imbalance had zero variance and
    the quant overlay dropped it from the active weight set entirely.
  * the scorecard's options sub-score sat at its 5.0 neutral default on every
    idea in the morning report.

Offline — pure string parsing.
"""

from __future__ import annotations

import pytest

from tradingagents.signals.sector_regime import _occ_right


@pytest.mark.parametrize("symbol,expected", [
    ("NVDA260904C00227500", "C"),   # the real alert that exposed this
    ("NVDA260821P00230000", "P"),
    ("MU260904C00650000", "C"),
    ("SPXW260918C05000000", "C"),   # 4-char root with a W suffix
    ("A260904P00050000", "P"),      # single-char root
])
def test_right_is_read_from_the_occ_position(symbol, expected):
    assert _occ_right(symbol) == expected


def test_the_old_endswith_test_matched_nothing():
    """Pin the actual defect so it cannot be reintroduced."""
    real = "NVDA260904C00227500"
    assert not real.endswith("C"), "OCC symbols end in the strike, not the right"
    assert not real.endswith("P")
    assert _occ_right(real) == "C"


def test_both_sums_would_have_been_zero():
    """Reproduce the exact failure: real premiums, zero totals."""
    alerts = [("NVDA260904C00227500", 126750.0), ("NVDA260821P00230000", 98000.0)]
    old_call = sum(p for c, p in alerts if c.endswith("C"))
    old_put = sum(p for c, p in alerts if c.endswith("P"))
    assert old_call == 0.0 and old_put == 0.0, "the bug: real premium, zero totals"

    new_call = sum(p for c, p in alerts if _occ_right(c) == "C")
    new_put = sum(p for c, p in alerts if _occ_right(c) == "P")
    assert new_call == 126750.0 and new_put == 98000.0


@pytest.mark.parametrize("bad", [
    "", None, "GARBAGE", "NVDA",
    "NVDA260904X00227500",   # not a C or P in the right slot
    "NVDA260904C0022750A",   # strike is not all digits
    "C00227500",             # right slot present but no root/date
])
def test_unparseable_symbols_are_excluded_not_guessed(bad):
    """A malformed symbol must fall out of BOTH sums rather than be counted as
    a call — a wrong bullish reading is worse than a missing one."""
    assert _occ_right(bad) == ""


def test_case_and_whitespace_tolerant():
    assert _occ_right(" nvda260904c00227500 ") == "C"


def test_right_is_never_taken_from_the_root():
    """A root containing C or P must not be misread — e.g. CRDO, PWR."""
    assert _occ_right("CRDO260904P00200000") == "P"
    assert _occ_right("PWR260904C00150000") == "C"
