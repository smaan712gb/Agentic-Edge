"""Automation safety layer.

Every automated decision flows through this package:

    universe.validate(symbol)      → in current theme universe?
    auto_gate.check(intent)        → kill switches + caps + pretrade limits
    market_conditions.check(...)   → VIX, halt, RTH, spread sanity
    reconcile.reconcile(symbol)    → DB state matches IBKR

If all four pass, the action is allowed. Every call writes one
``auto_actions`` row regardless of outcome — silence is not an option.

The IBKR-call layer is *not* in this package; that lives in
``api/app/positions.py`` (reads) and a forthcoming ``trades.py`` (writes).
auto_gate is the policy engine; trades.py is the executor.
"""

from .alerts import alert
from .auto_gate import (
    AutoGateResult,
    GateFailure,
    check_auto_action,
    record_auto_action,
)
from .universe import current_universe, validate_in_universe

__all__ = [
    "alert",
    "AutoGateResult",
    "GateFailure",
    "check_auto_action",
    "current_universe",
    "record_auto_action",
    "validate_in_universe",
]
