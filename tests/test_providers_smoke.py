"""Smoke tests — no network. Verifies imports, Protocols, and key parsing.

Run with `pytest -q tests/test_providers_smoke.py`. Real-vendor tests that
hit the network live in `tests/test_providers_integration.py` and are
gated by the env vars whose absence makes them skip.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest


def test_package_imports_cleanly():
    mod = importlib.import_module("tradingagents.dataflows.providers")
    assert hasattr(mod, "register_into")
    assert hasattr(mod, "get_provider")


def test_protocol_typing():
    from tradingagents.dataflows.providers import (
        FundamentalsProvider,
        OptionsFlowProvider,
        OptionsChainProvider,
        MacroSignalProvider,
        ExecutionProvider,
    )
    # Protocols must be runtime-checkable so we can detect bad providers
    # at startup rather than mid-graph. `runtime_checkable` is set in base.py.
    assert hasattr(FundamentalsProvider, "__instancecheck__")
    assert hasattr(OptionsFlowProvider, "__instancecheck__")
    assert hasattr(OptionsChainProvider, "__instancecheck__")
    assert hasattr(MacroSignalProvider, "__instancecheck__")
    assert hasattr(ExecutionProvider, "__instancecheck__")


def test_value_objects_round_trip_to_json():
    from tradingagents.dataflows.providers import FlowAlert, GammaLevel, OptionContract, OptionRight

    alert = FlowAlert(
        ticker="NVDA",
        triggered_at=datetime(2026, 5, 7, 15, 30, tzinfo=timezone.utc),
        contract="NVDA 2026-06-19 C 1200",
        side="ABOVE_ASK",
        premium=Decimal("250000"),
        size=100,
        open_interest=4321,
        iv=0.42,
        sweep=True,
        repeat_count=3,
        raw={"echoed": True},
    )
    # Frozen dataclasses are hashable — needed for set-based dedup downstream.
    assert hash(alert)


def test_unknown_macro_series_raises():
    import os
    os.environ.setdefault("ALPHA_VANTAGE_API_KEY", "test-key")
    from tradingagents.dataflows.providers.alphavantage_macro import AlphaVantageMacroProvider
    from tradingagents.dataflows.providers.base import ProviderError

    p = AlphaVantageMacroProvider(api_key="test-key")
    import asyncio

    with pytest.raises(ProviderError):
        asyncio.run(p.get_macro_signal("NOT_A_REAL_SERIES"))


def test_ibkr_refuses_non_paper_mode():
    from tradingagents.dataflows.providers.base import LiveTradingDisabledError
    from tradingagents.dataflows.providers.ibkr import IbkrProvider

    with pytest.raises(LiveTradingDisabledError):
        IbkrProvider(account_mode="live")


def test_registry_extends_interface():
    """Spot-check that `register_into` adds the new vendors and categories."""
    import types

    fake_interface = types.SimpleNamespace(
        VENDOR_METHODS={
            "get_stock_data":      {"yfinance": lambda *_a, **_k: ""},
            "get_fundamentals":    {"yfinance": lambda *_a, **_k: ""},
        },
        TOOLS_CATEGORIES={
            "core_stock_apis":   {"description": "...", "tools": ["get_stock_data"]},
            "fundamental_data":  {"description": "...", "tools": ["get_fundamentals"]},
        },
        VENDOR_LIST=["yfinance", "alpha_vantage"],
    )
    from tradingagents.dataflows.providers import registry

    registry.register_into(fake_interface)
    assert "polygon" in fake_interface.VENDOR_METHODS["get_stock_data"]
    assert "fmp" in fake_interface.VENDOR_METHODS["get_fundamentals"]
    assert "options_flow" in fake_interface.TOOLS_CATEGORIES
    assert "macro_signals" in fake_interface.TOOLS_CATEGORIES
    assert "polygon" in fake_interface.VENDOR_LIST
    assert "unusual_whales" in fake_interface.VENDOR_LIST
