"""Regression tests for the auto-gate audit recorder.

The rotation entry-halt (and other non-gate maintenance actions) call
``record_auto_action`` with ``gate_result=None``. Before the fix this crashed on
``gate_result.passed`` (AttributeError); the crash was swallowed by the caller's
try/except, so the halt FAILED OPEN and the entry went through anyway (ABBNY into
a flagged 'grid-bottleneck' rotation). These tests lock in the defensive
handling so a None gate result can never again silently let an entry pass.

Offline — uses a fake async session (no DB).
"""

from __future__ import annotations

import asyncio

from api.app.autotrade.auto_gate import record_auto_action


class _FakeSession:
    """Minimal stand-in for AsyncSession: add() is sync, flush() is async."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


def test_none_gate_result_without_error_is_rejected_not_crash():
    sess = _FakeSession()
    row = asyncio.run(record_auto_action(
        sess, loop="entry", action_type="entry_blocked_rotation",
        gate_result=None, symbol="ABBNY",
    ))
    # The rotation halt must record a REJECTED row (the entry is blocked),
    # never raise — a raise here is the fail-open bug.
    assert row.gate_status == "rejected"
    assert len(sess.added) == 1


def test_none_gate_result_with_error_is_error():
    sess = _FakeSession()
    row = asyncio.run(record_auto_action(
        sess, loop="entry", action_type="open_leap",
        gate_result=None, symbol="X", error="boom",
    ))
    assert row.gate_status == "error"


def test_none_gate_result_does_not_touch_failures_or_first_reject():
    # Exercises the gate_result-is-None branches of both the failures payload
    # and the rejected-logging path — neither may dereference None.
    sess = _FakeSession()
    row = asyncio.run(record_auto_action(
        sess, loop="maintenance", action_type="position_pressure_hold",
        gate_result=None, symbol="MU",
    ))
    assert row.gate_status == "rejected"
    assert row.gate_failures is None
