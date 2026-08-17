"""Regression tests for the silent-fail-open class of defect.

Every bug below shares one shape: a guardrail that stops working produces
output IDENTICAL to the guardrail saying "all clear". That is the most
dangerous failure mode in an autonomous book, because nothing looks wrong.

  * MACRO GATE — IBKR's index feed returns {last: None} WITHOUT raising when
    the account lacks a CBOE subscription. _classify_regime(None, None) ->
    'calm' -> sizing_factor 1.0. Observed 2026-08-17: the volatility guardrail
    was permanently inert and full-size entries proceeded; elevated/defensive/
    panic could never fire. No exception, no warning, no log line.

  * CIRCUIT BREAKER — the entry loop resolved the broker inside the same
    try/except as the breaker call, so a connect failure skipped the breaker
    entirely. During the 2026-08-08..10 outage (9,007 failed reconnects,
    ~78h) every health line read breaker=False.

  * KILL SWITCH — the maintenance loop returned before doing anything, which
    disabled the whole exit layer, not just order placement.

Offline — pure/fake objects only, no DB, no broker, no network.
"""

from __future__ import annotations

import asyncio

import pytest

from tradingagents.strategies.macro_regime import (
    MacroRegime, _classify_regime, get_macro_regime,
)


# ---------------------------------------------------------------------------
# Macro volatility guardrail
# ---------------------------------------------------------------------------


class _BlindBroker:
    """Exactly what the paper account returns: empty, and it does NOT raise."""

    async def get_index_quote(self, **_kw):
        return {"last": None, "close": None, "change_pct": None}


class _WorkingBroker:
    def __init__(self, vix, spx_change):
        self._vix, self._spx = vix, spx_change

    async def get_index_quote(self, *, symbol, **_kw):
        if symbol == "VIX":
            return {"last": self._vix, "close": None, "change_pct": None}
        return {"last": 5000.0, "close": None, "change_pct": self._spx}


def test_blind_read_is_reported_as_degraded_not_calm(monkeypatch):
    """The whole point: 'couldn't see' must be distinguishable from 'calm'."""
    async def _no_fallback(_symbols):
        return {}
    monkeypatch.setattr(
        "tradingagents.strategies.macro_regime._fmp_index_quotes", _no_fallback)

    m = asyncio.run(get_macro_regime(_BlindBroker()))
    assert m.degraded is True, "a blind read must set degraded"
    assert m.vix_last is None and m.spx_change_pct is None
    assert "BLIND" in m.rationale


def test_working_read_is_not_flagged_degraded():
    m = asyncio.run(get_macro_regime(_WorkingBroker(15.1, -0.004)))
    assert m.degraded is False
    assert m.regime == "calm" and m.sizing_factor == 1.0


def test_fallback_recovers_the_guardrail_when_broker_is_blind(monkeypatch):
    """A different VENDOR, not a retry of the same entitlement-gated path."""
    async def _fmp(symbols):
        return {
            "^VIX": {"last": 38.0, "close": None, "change_pct": 0.25},
            "^GSPC": {"last": 4200.0, "close": None, "change_pct": -0.041},
        }
    monkeypatch.setattr(
        "tradingagents.strategies.macro_regime._fmp_index_quotes", _fmp)

    m = asyncio.run(get_macro_regime(_BlindBroker()))
    assert m.degraded is False
    assert m.vix_last == 38.0
    # VIX 38 (> 35) and SPX -4.1% (< -3.5%) are both panic.
    assert m.regime == "panic"
    assert m.sizing_factor == 0.0, "panic must block new entries"
    assert "fmp" in m.rationale


def test_panic_would_have_been_missed_while_blind(monkeypatch):
    """The concrete cost of the bug: a real panic tape read as calm, full size."""
    async def _no_fallback(_symbols):
        return {}
    monkeypatch.setattr(
        "tradingagents.strategies.macro_regime._fmp_index_quotes", _no_fallback)

    blind = asyncio.run(get_macro_regime(_BlindBroker()))
    seeing = asyncio.run(get_macro_regime(_WorkingBroker(42.0, -0.05)))

    assert blind.sizing_factor == 1.0 and blind.degraded is True
    assert seeing.sizing_factor == 0.0 and seeing.regime == "panic"
    # Same tape, opposite behaviour — which is why `degraded` must be surfaced.


@pytest.mark.parametrize(
    "vix,spx,expected",
    [
        (12.0, 0.002, "calm"),
        (20.0, 0.0, "elevated"),
        (30.0, 0.0, "defensive"),
        (40.0, 0.0, "panic"),
        (12.0, -0.045, "panic"),      # SPX alone can trip it
        (None, -0.025, "defensive"),  # partial read still classifies
    ],
)
def test_classifier_takes_the_worse_of_the_two_signals(vix, spx, expected):
    assert _classify_regime(vix, spx) == expected


def test_percent_vs_fraction_is_not_confused():
    """FMP reports changePercentage as -0.368 meaning -0.368%, while the
    classifier's thresholds are fractions (-0.005 = -0.5%). Feeding the raw
    percent straight through would read a routine -0.37% day as -37% and
    classify PANIC, blocking all entries."""
    raw_fmp_percent = -0.368
    as_fraction = raw_fmp_percent / 100.0
    assert _classify_regime(15.0, as_fraction) == "calm"
    assert _classify_regime(15.0, raw_fmp_percent) == "panic"   # the bug, if unconverted


# ---------------------------------------------------------------------------
# Circuit breaker must latch when the broker is unreachable
# ---------------------------------------------------------------------------


def test_breaker_treats_no_connection_as_a_halt_reason():
    """ib=None is the 'flying blind' case and must produce a halt reason.

    Verified through the pure precondition rather than the DB-latching path:
    the defect was that check_entry_breaker was never CALLED on a connect
    failure, so the contract that matters is that a None provider is a halt.
    """
    import inspect

    from api.app.autotrade.circuit_breaker import check_entry_breaker

    src = inspect.getsource(check_entry_breaker)
    assert "if ib is None" in src, (
        "check_entry_breaker must handle a missing provider explicitly"
    )
    assert "broker unreachable" in src


def test_entry_loop_resolves_broker_outside_the_breaker_try():
    """The connect failure must not skip the breaker evaluation."""
    import inspect

    from api.app.autotrade import entry_loop

    src = inspect.getsource(entry_loop._tick)
    assert "ib_for_breaker = None" in src, (
        "a failed _ibkr() must still reach check_entry_breaker as None"
    )


# ---------------------------------------------------------------------------
# Kill switch stops ORDERS, not observation
# ---------------------------------------------------------------------------


def test_kill_switch_does_not_short_circuit_the_maintenance_tick():
    """Disarming must not disable orphan adoption / exit scoring / alerting."""
    import inspect

    from api.app.autotrade import maint_loop

    src = inspect.getsource(maint_loop._tick)
    assert "OBSERVE-ONLY" in src
    assert "trading_enabled" in src


def test_every_order_placing_path_still_guards_the_kill_switch():
    """Removing the early return is only safe because each executor re-checks.

    If someone adds a new executor without the guard, this fails.
    """
    import inspect

    from api.app.autotrade import maint_loop

    src = inspect.getsource(maint_loop)
    executors = [
        "_execute_off_theme_close", "_execute_stock_exit", "_execute_stock_trim",
        "_execute_auto_short_call_close", "_execute_auto_short_call_roll",
        "_execute_auto_leap_forward_roll",
    ]
    for name in executors:
        fn = getattr(maint_loop, name)
        body = inspect.getsource(fn)
        assert "_autotrade_active" in body, (
            f"{name} places orders but does not re-check the kill switch"
        )
    # _flag_pmcc_close guards via the auto_execute downgrade instead.
    assert "_autotrade_active" in inspect.getsource(maint_loop._flag_pmcc_close)
    assert src.count("async def _execute_") == len(executors), (
        "a new order-placing executor was added — confirm it guards the kill switch"
    )


def test_operator_close_bypasses_the_kill_switch_and_the_daily_cap():
    """An authenticated manual close is the emergency path — it must fire when
    the switch is OFF, which is exactly when the operator needs it."""
    import inspect

    from api.app.autotrade import maint_loop

    src = inspect.getsource(maint_loop._flag_pmcc_close)
    assert "if operator:" in src and "auto_execute = True" in src
    assert "not operator and await _maintenance_cap_hit" in src


def test_manual_exit_endpoint_handles_the_production_structure():
    """leap_only is the live book; it used to fall through to a 400."""
    import inspect

    from api.app import admin

    src = inspect.getsource(admin.manual_exit_position)
    assert 'structure == "leap_only"' in src
    assert "operator=True" in src


# ---------------------------------------------------------------------------
# Entry execution: decided orders execute, and nothing is hardcoded
# ---------------------------------------------------------------------------


def test_execution_params_scale_with_the_spread():
    """A wide illiquid LEAP and a tight megacap must not get the same budget.

    The old path pinned initial_offset=5c, increment=5c, timeout=180s inline for
    every contract. On 2026-08-17 that produced 13 abandons in 14 orders: 5c is
    a sane step on a $2 spread and 32 pointless crawls on a $16 one, and 180s
    ran out before the algo could cross a wide market.
    """
    from api.app.autotrade.entry_loop import build_entry_execution_config

    tight_cfg, tight = build_entry_execution_config(bid=99.8, ask=100.2, mid=100.0)
    wide_cfg, wide = build_entry_execution_config(bid=87.0, ask=92.2, mid=89.6)

    assert wide["spread_pct"] > tight["spread_pct"]
    assert wide_cfg.timeout_sec > tight_cfg.timeout_sec, "wider spread needs more time"
    assert wide_cfg.walk_increment_cents > tight_cfg.walk_increment_cents


def test_step_never_below_the_exchange_minimum_tick():
    """Options over $3 have a 5c minimum tick — a smaller step is a no-op walk."""
    from api.app.autotrade.entry_loop import build_entry_execution_config

    cfg, _ = build_entry_execution_config(bid=99.99, ask=100.01, mid=100.0)
    assert cfg.initial_offset_cents >= 5
    assert cfg.walk_increment_cents >= 5


def test_timeout_is_clamped():
    """An absurd spread must not produce an unbounded working order."""
    from api.app.config import get_settings
    from api.app.autotrade.entry_loop import build_entry_execution_config

    cfg, _ = build_entry_execution_config(bid=1.0, ask=99.0, mid=50.0)
    assert cfg.timeout_sec <= float(get_settings().LEAP_ENTRY_TIMEOUT_MAX_SEC)


def test_a_decided_entry_is_not_abandoned_on_spread_or_drift():
    """Once the decision is made, execution is the algo's job.

    The spread is a cost of the position, not a veto on it — on a multi-year
    hold half a spread paid once amortises away, while abandoning leaves no
    position at all. Two abandon triggers had to go: the fractional-spread cap
    (now the full half-spread, i.e. the ask) and abandon-on-mid-drift, which
    ExecutionConfig defaults to 0.05 and the entry path never set.
    """
    from api.app.config import get_settings
    from api.app.autotrade.entry_loop import build_entry_execution_config

    s = get_settings()
    cfg, audit = build_entry_execution_config(bid=509.0, ask=525.0, mid=517.0)

    assert cfg.max_offset_pct_of_spread >= 1.0, "must be willing to pay the offer"
    assert cfg.abandon_on_mid_drift_pct is None, "a decided entry must not cancel on drift"
    assert s.LEAP_ENTRY_ADAPTIVE_PRIORITY == "Urgent"
    # The cap resolves to exactly the ask — pays the offer, never through it.
    assert audit["mid"] + (audit["ask"] - audit["bid"]) / 2 == 525.0


def test_missing_quote_degrades_to_a_sane_plan():
    """No bid/ask (after hours) must still produce a usable config, not a crash."""
    from api.app.autotrade.entry_loop import build_entry_execution_config

    cfg, audit = build_entry_execution_config(bid=None, ask=None, mid=100.0)
    assert audit["spread_pct"] == 0.0
    assert cfg.timeout_sec > 0 and cfg.walk_increment_cents >= 5
