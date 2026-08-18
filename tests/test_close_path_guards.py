"""Guards added after the FN incident of 2026-08-18.

One intent (qty=3, a genuine earnings-break exit) drove 28 close attempts over
3h15m. Every attempt offered $244 into a $240 bid and never repriced —
``walk_steps: 0`` on all 28. Every attempt was recorded ``abandoned`` with
``fill_price: null, order_still_live: false``. Two of them filled at the broker
after the cancel. Because the close quantity came from ``intent.qty`` rather
than the account, each retry asked to sell 3 regardless: +3 → 0 → −3, a naked
short call in a long-call-only book, which the reconciler then skipped (it only
looked at ``> 0``) and the maintenance loop finally wrote off as a phantom.

Each test below fails on the pre-fix code.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.app.autotrade.position_guard import (
    describe_short_options,
    live_position_qty,
    resolve_close_qty,
    short_option_positions,
)
from tradingagents.strategies.execution.walking_limit import (
    ExecutionConfig,
    _confirm_fill_after_cancel,
    submit_single_leg_option,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeIbkr:
    """Minimal IbkrProvider stand-in: positions only."""

    def __init__(self, positions, *, raises=False):
        self._positions = positions
        self._raises = raises

    async def get_positions(self):
        if self._raises:
            raise RuntimeError("competing live session")
        return self._positions


def _opt(symbol, conid, qty, **kw):
    return {"symbol": symbol, "conid": conid, "qty": qty, "secType": "OPT",
            "right": "C", "expiry": kw.get("expiry", "20271217"),
            "strike": kw.get("strike", 350.0)}


# ---------------------------------------------------------------------------
# resolve_close_qty — the close is sized by the broker, not by the intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_qty_clamps_to_live_position():
    # Intent believes 3; broker holds 1 (two contracts already sold by a
    # late fill the walker reported as abandoned).
    ib = FakeIbkr([_opt("FN", 909924757, 1)])
    d = await resolve_close_qty(ib=ib, conid=909924757, symbol="FN", requested=3)
    assert d.qty == 1
    assert d.disposition == "clamped"
    assert d.ok


@pytest.mark.asyncio
async def test_close_qty_refuses_when_flat():
    # The exact retry that took FN from 0 to −3.
    ib = FakeIbkr([])
    d = await resolve_close_qty(ib=ib, conid=909924757, symbol="FN", requested=3)
    assert d.qty == 0
    assert d.disposition == "refuse"
    assert not d.ok


@pytest.mark.asyncio
async def test_close_qty_refuses_and_never_sells_into_a_short():
    ib = FakeIbkr([_opt("FN", 909924757, -3)])
    d = await resolve_close_qty(ib=ib, conid=909924757, symbol="FN", requested=3)
    assert d.qty == 0
    assert d.disposition == "refuse"
    assert d.broker_qty == -3


@pytest.mark.asyncio
async def test_close_qty_refuses_when_broker_unreadable():
    # Blind is not flat. Sizing a close off stale bookkeeping is the bug.
    ib = FakeIbkr([], raises=True)
    d = await resolve_close_qty(ib=ib, conid=909924757, symbol="FN", requested=3)
    assert d.disposition == "refuse"
    assert d.broker_qty is None


@pytest.mark.asyncio
async def test_close_qty_proceeds_when_broker_agrees():
    ib = FakeIbkr([_opt("FN", 909924757, 3)])
    d = await resolve_close_qty(ib=ib, conid=909924757, symbol="FN", requested=3)
    assert (d.qty, d.disposition) == (3, "proceed")


@pytest.mark.asyncio
async def test_live_position_distinguishes_flat_from_unknown():
    assert await live_position_qty(FakeIbkr([]), conid=1) == 0.0
    assert await live_position_qty(FakeIbkr([], raises=True), conid=1) is None


@pytest.mark.asyncio
async def test_live_position_sums_duplicate_conid_rows():
    ib = FakeIbkr([_opt("FN", 1, 2), _opt("FN", 1, -5)])
    assert await live_position_qty(ib, conid=1) == -3.0


# ---------------------------------------------------------------------------
# The long-only invariant
# ---------------------------------------------------------------------------


def test_short_option_detector_finds_the_breach():
    positions = [_opt("MU", 11, 4), _opt("FN", 909924757, -3), {"symbol": "X", "secType": "STK", "qty": -100}]
    shorts = short_option_positions(positions)
    assert len(shorts) == 1
    assert shorts[0]["symbol"] == "FN"
    # A short STOCK is a different (and legal-elsewhere) thing; this invariant
    # is specifically about undefined-risk short option legs.
    assert "FN" in describe_short_options(shorts)


def test_short_option_detector_clean_book():
    assert short_option_positions([_opt("MU", 11, 4), _opt("STX", 12, 1)]) == []


# ---------------------------------------------------------------------------
# Post-cancel fill confirmation — "no fill" vs "don't know"
# ---------------------------------------------------------------------------


class FakeExecIbkr:
    def __init__(self, ib):
        self._ib = ib

    async def _ensure_connected(self):
        if self._ib is None:
            raise RuntimeError("not connected")
        return self._ib


class FakeIB:
    """ib_insync IB stand-in for the executor."""

    def __init__(self, *, executions=None, exec_raises=False):
        self._executions = executions or []
        self._exec_raises = exec_raises
        self.placed: list[float] = []
        self.cancelled = False

    async def qualifyContractsAsync(self, contract):
        return [contract]

    def placeOrder(self, contract, order):
        # IB assigns the order id on placement; the executor reads it back to
        # match executions, so the fake has to do the same.
        if not getattr(order, "orderId", 0):
            order.orderId = 156757
        self.placed.append(order.lmtPrice)
        trade = getattr(self, "_trade", None)
        if trade is None:
            trade = SimpleNamespace(
                order=order,
                orderStatus=SimpleNamespace(status="Submitted", filled=0,
                                            remaining=order.totalQuantity,
                                            avgFillPrice=0),
                fills=[],
            )
            self._trade = trade
        trade.order = order
        return trade

    def cancelOrder(self, order):
        self.cancelled = True
        self._trade.orderStatus.status = "Cancelled"

    async def reqExecutionsAsync(self, _filter):
        if self._exec_raises:
            raise RuntimeError("execution query failed")
        return self._executions


def _exec_detail(order_id, conid, shares, price):
    return SimpleNamespace(
        execution=SimpleNamespace(orderId=order_id, shares=shares, price=price),
        contract=SimpleNamespace(conId=conid),
    )


@pytest.mark.asyncio
async def test_confirm_fill_returns_none_when_executions_unreadable():
    # None means "unknown" and MUST NOT be collapsed into "nothing filled" —
    # that collapse is what wrote order_still_live: false over a live order.
    ib = FakeIB(exec_raises=True)
    trade = SimpleNamespace(order=SimpleNamespace(orderId=1),
                            orderStatus=SimpleNamespace(status="Cancelled", filled=0,
                                                        remaining=3, avgFillPrice=0),
                            fills=[])
    got = await _confirm_fill_after_cancel(
        ibkr=FakeExecIbkr(ib), trade=trade, conid=99, contracts=3, settle_sec=0.1)
    assert got is None


@pytest.mark.asyncio
async def test_confirm_fill_finds_the_execution_that_landed_during_cancel():
    ib = FakeIB(executions=[_exec_detail(156757, 909924757, 3, 216.0)])
    trade = SimpleNamespace(order=SimpleNamespace(orderId=156757),
                            orderStatus=SimpleNamespace(status="Cancelled", filled=0,
                                                        remaining=3, avgFillPrice=0),
                            fills=[])
    got = await _confirm_fill_after_cancel(
        ibkr=FakeExecIbkr(ib), trade=trade, conid=909924757, contracts=3, settle_sec=0.1)
    assert got is not None
    assert got["filled_qty"] == 3
    assert got["avg_fill_price"] == pytest.approx(216.0)


@pytest.mark.asyncio
async def test_confirm_fill_reports_a_confirmed_no_fill():
    ib = FakeIB(executions=[])
    trade = SimpleNamespace(order=SimpleNamespace(orderId=1),
                            orderStatus=SimpleNamespace(status="Cancelled", filled=0,
                                                        remaining=3, avgFillPrice=0),
                            fills=[])
    got = await _confirm_fill_after_cancel(
        ibkr=FakeExecIbkr(ib), trade=trade, conid=99, contracts=3, settle_sec=0.1)
    assert got == {"filled_qty": 0.0, "avg_fill_price": None, "source": "confirmed_no_fill"}


# ---------------------------------------------------------------------------
# The walk actually walks
# ---------------------------------------------------------------------------


class WalkIbkr(FakeExecIbkr):
    def __init__(self, ib, *, bid, ask):
        super().__init__(ib)
        self._bid, self._ask = bid, ask

    async def get_option_quote_by_conid(self, *, conid):
        return {"bid": self._bid, "ask": self._ask, "mid": (self._bid + self._ask) / 2}


def _fast_cfg(**kw):
    base = dict(initial_offset_cents=1, walk_increment_cents=400,
                walk_interval_sec=0.3, timeout_sec=1.8,
                abandon_on_mid_drift_pct=None, post_cancel_settle_sec=0.05)
    base.update(kw)
    return ExecutionConfig(**base)


@pytest.mark.asyncio
async def test_sell_walk_steps_the_price_down_and_reaches_the_bid():
    # FN's quote on the first close attempt: 240 / 256, mid 248.
    ib = FakeIB(executions=[])
    r = await submit_single_leg_option(
        ibkr=WalkIbkr(ib, bid=240.0, ask=256.0), conid=909924757, contracts=3,
        action="SELL", config=_fast_cfg(), adaptive_priority="Urgent",
        allow_touch=True)

    # It walked (the old executor placed one order and reported walk_steps: 0).
    assert r.walk_steps > 1
    assert len(ib.placed) > 1
    # Each successive price is lower — a SELL walks DOWN toward the bid.
    assert ib.placed == sorted(ib.placed, reverse=True)
    # And it is allowed to reach the bid. The old cap of mid − 50% of the
    # half-spread was $244: structurally unable to trade against a $240 bid.
    assert r.cap_price == pytest.approx(240.0)
    assert min(ib.placed) == pytest.approx(240.0)


@pytest.mark.asyncio
async def test_walk_step_widens_to_cover_the_spread_within_the_timeout():
    # The exit path's configured step is 1¢. Against FN's 8-point half-spread
    # that is ~44 hours of walking inside a 180s budget, so the step must scale
    # to the distance the walk actually has to cover.
    ib = FakeIB(executions=[])
    r = await submit_single_leg_option(
        ibkr=WalkIbkr(ib, bid=240.0, ask=256.0), conid=909924757, contracts=3,
        action="SELL", config=_fast_cfg(walk_increment_cents=1),
        allow_touch=True)
    assert min(ib.placed) == pytest.approx(r.cap_price)


@pytest.mark.asyncio
async def test_sell_walk_without_touch_stays_inside_the_spread():
    # A discretionary trim keeps the disciplined cap rather than hitting the bid.
    ib = FakeIB(executions=[])
    r = await submit_single_leg_option(
        ibkr=WalkIbkr(ib, bid=240.0, ask=256.0), conid=909924757, contracts=1,
        action="SELL", config=_fast_cfg(max_offset_pct_of_spread=0.50),
        allow_touch=False)
    assert r.cap_price > 240.0
    assert min(ib.placed) >= r.cap_price


@pytest.mark.asyncio
async def test_abandoned_close_that_actually_filled_is_reported_as_filled():
    # The FN failure mode: walker times out, cancels, order fills anyway.
    ib = FakeIB(executions=[_exec_detail(156757, 909924757, 3, 238.0)])
    r = await submit_single_leg_option(
        ibkr=WalkIbkr(ib, bid=240.0, ask=256.0), conid=909924757, contracts=3,
        action="SELL", config=_fast_cfg(), allow_touch=True)
    assert r.status == "filled"
    assert r.filled_qty == 3
    assert r.fill_price == pytest.approx(238.0)
    assert r.order_still_live is False


@pytest.mark.asyncio
async def test_partial_fill_during_cancel_is_not_reported_as_a_clean_close():
    ib = FakeIB(executions=[_exec_detail(156757, 909924757, 1, 238.0)])
    r = await submit_single_leg_option(
        ibkr=WalkIbkr(ib, bid=240.0, ask=256.0), conid=909924757, contracts=3,
        action="SELL", config=_fast_cfg(), allow_touch=True)
    assert r.status == "filled"
    assert r.filled_qty == 1          # caller must not book a 3-lot close
    assert "PARTIAL" in (r.error or "")


@pytest.mark.asyncio
async def test_unreadable_executions_flag_the_order_as_possibly_live():
    ib = FakeIB(exec_raises=True)
    r = await submit_single_leg_option(
        ibkr=WalkIbkr(ib, bid=240.0, ask=256.0), conid=909924757, contracts=3,
        action="SELL", config=_fast_cfg(), allow_touch=True)
    assert r.status == "abandoned"
    assert r.order_still_live is True
    assert "unconfirmed" in (r.error or "").lower()
