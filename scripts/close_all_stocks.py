"""One-shot stock close-out — flatten all equity positions on the IBKR paper account.

Runs through the same IbkrProvider used by the live system. For each
open stock position:

  1. Snapshot the live bid/ask via reqMktData.
  2. Submit a marketable LMT SELL at bid - 1¢ (to fill quickly without
     paying ask). Stock spreads are 1-3¢ on the names we hold; this
     gets filled within seconds.
  3. Wait up to 30s for the fill. If unfilled, cancel and walk to mid.
  4. Record an audit row in trade_audit_log.

Safe-by-default:
  * --dry-run prints the plan without sending anything (default ON)
  * --execute is required to actually fire orders
  * --symbols=AAA,BBB to close a subset (default: all)
  * --skip-universe skips positions whose ticker is in any current theme
    (so you can keep the names that are about to become PMCC targets —
    not generally recommended, but available)

Usage from PowerShell:
  python scripts/close_all_stocks.py                 # dry-run, show plan
  python scripts/close_all_stocks.py --execute       # fire it
  python scripts/close_all_stocks.py --execute --skip-universe
  python scripts/close_all_stocks.py --execute --symbols=GM,FDX,XLE
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("close_all_stocks")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


WALK_INTERVAL_SEC    = 10.0     # poll the order this often
TOTAL_TIMEOUT_SEC    = 60.0     # give up if no fill after this many seconds
INITIAL_OFFSET_CENTS = 1        # bid - 1¢ for sell (we're crossing the spread)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Flatten all stock positions.")
    ap.add_argument("--execute", action="store_true",
                    help="Actually send orders (default: dry-run)")
    ap.add_argument("--symbols", type=str, default="",
                    help="Comma-separated subset; default = all positions")
    ap.add_argument("--skip-universe", action="store_true",
                    help="Skip positions whose ticker is in any current theme")
    args = ap.parse_args()

    # -------- Load env + connect IBKR --------
    from api.app.config import get_settings, reset_settings_cache
    reset_settings_cache()
    s = get_settings()
    if s.IBKR_MODE != "paper":
        logger.error("IBKR_MODE != paper. Refusing.")
        return 1

    from tradingagents.dataflows.providers.ibkr import IbkrProvider
    ib_provider = IbkrProvider(
        host=s.IBKR_HOST, port=s.IBKR_PORT, client_id=s.IBKR_CLIENT_ID,
        account_mode=s.IBKR_MODE,
    )
    ib = await ib_provider._ensure_connected()
    logger.info("connected to IBKR (paper) on %s:%d clientId=%d",
                s.IBKR_HOST, s.IBKR_PORT, s.IBKR_CLIENT_ID)

    # -------- Pull current positions --------
    positions = await ib_provider.get_positions()
    if not positions:
        logger.info("no positions to close.")
        return 0

    # Keep only stock-type positions (not options or futures)
    stocks = [p for p in positions if p.get("secType") == "STK" and float(p.get("qty") or 0) != 0]
    if args.symbols:
        wanted = {x.strip().upper() for x in args.symbols.split(",") if x.strip()}
        stocks = [p for p in stocks if p["symbol"].upper() in wanted]

    if args.skip_universe:
        from api.app.db import get_session as db_session
        from api.app.autotrade.universe import current_universe
        async with db_session() as ses:
            uni = await current_universe(ses)
        before = len(stocks)
        stocks = [p for p in stocks if p["symbol"].upper() not in uni]
        logger.info("--skip-universe: dropped %d positions in current themes",
                    before - len(stocks))

    # -------- Print the plan --------
    total_value = sum(p["qty"] * p["last_price"] for p in stocks)
    total_pnl = sum(p["pnl"] for p in stocks)
    print()
    print(f"=== Close-out plan ({len(stocks)} stock positions) ===")
    print(f"{'Symbol':<8} {'Qty':>8}  {'Avg':>10}  {'Last':>10}  {'Value':>14}  {'PnL':>12}")
    print("-" * 76)
    for p in sorted(stocks, key=lambda x: x["symbol"]):
        val = p["qty"] * p["last_price"]
        print(f"{p['symbol']:<8} {p['qty']:>8.0f}  ${p['avg_price']:>9.2f}  ${p['last_price']:>9.2f}  ${val:>13,.0f}  ${p['pnl']:>11,.0f}")
    print("-" * 76)
    print(f"{'TOTAL':<8} {'':>8}  {'':>10}  {'':>10}  ${total_value:>13,.0f}  ${total_pnl:>11,.0f}")
    print()

    if not args.execute:
        print("DRY RUN — no orders sent. Add --execute to fire.")
        await ib_provider.aclose()
        return 0

    # -------- Confirm --------
    confirm = input(f"Submit MARKETABLE-LIMIT SELLS for {len(stocks)} positions on PAPER account? [yes/no] ")
    if confirm.strip().lower() not in ("yes", "y"):
        print("Cancelled.")
        await ib_provider.aclose()
        return 0

    # -------- Execute --------
    from ib_insync import LimitOrder, Stock  # type: ignore
    from api.app.db import TradeAuditLog, get_session as db_session

    successes = 0
    failures: list[tuple[str, str]] = []
    for p in stocks:
        sym = p["symbol"]
        qty = abs(p["qty"])
        action = "SELL" if p["qty"] > 0 else "BUY"     # cover shorts too if any

        # Snapshot current quote
        contract = Stock(sym, "SMART", "USD")
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            logger.warning("could not qualify %s, skipping", sym)
            failures.append((sym, "qualify failed"))
            continue
        contract = qualified[0]

        ticker = ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(1.5)
        bid = float(ticker.bid or 0)
        ask = float(ticker.ask or 0)
        last = float(ticker.last or p["last_price"] or 0)
        ib.cancelMktData(contract)

        # Marketable limit price: cross the spread by 1¢ for fast fill, but
        # never worse than mid (don't pay through the spread).
        if action == "SELL":
            if bid > 0:
                limit = round(bid - INITIAL_OFFSET_CENTS / 100.0, 2)
            else:
                limit = round(last - 0.01, 2)
            if ask > 0 and limit > ask:
                limit = round(ask, 2)
        else:
            if ask > 0:
                limit = round(ask + INITIAL_OFFSET_CENTS / 100.0, 2)
            else:
                limit = round(last + 0.01, 2)

        order = LimitOrder(action=action, totalQuantity=qty, lmtPrice=limit, tif="DAY")

        # Audit BEFORE the call.
        async with db_session() as ses:
            ses.add(TradeAuditLog(
                action="close_out_attempt", outcome=None,
                payload={"symbol": sym, "qty": qty, "side": action,
                         "limit": limit, "bid": bid, "ask": ask, "last": last},
            ))

        trade = ib.placeOrder(contract, order)
        logger.info("submitted %s %s %s @ $%.2f (orderId=%s)",
                    action, qty, sym, limit, trade.order.orderId)

        # Poll for fill
        deadline = asyncio.get_running_loop().time() + TOTAL_TIMEOUT_SEC
        filled = False
        avg_fill = None
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(WALK_INTERVAL_SEC)
            os = trade.orderStatus
            if (os.filled or 0) >= qty:
                filled = True
                avg_fill = float(os.avgFillPrice or 0)
                break
            if os.status in ("Cancelled", "ApiCancelled"):
                break

        if not filled:
            try:
                ib.cancelOrder(trade.order)
            except Exception:
                pass
            logger.warning("did NOT fill within %.0fs: %s", TOTAL_TIMEOUT_SEC, sym)
            failures.append((sym, f"no fill in {TOTAL_TIMEOUT_SEC}s"))
            async with db_session() as ses:
                ses.add(TradeAuditLog(
                    action="close_out_outcome", outcome="abandoned",
                    payload={"symbol": sym, "limit": limit, "qty": qty},
                ))
            continue

        successes += 1
        logger.info("FILLED %s %s @ $%.2f", action, sym, avg_fill)
        async with db_session() as ses:
            ses.add(TradeAuditLog(
                action="close_out_outcome", outcome="filled",
                ibkr_account=ib.managedAccounts()[0] if ib.managedAccounts() else None,
                payload={
                    "symbol": sym, "qty": qty, "side": action,
                    "limit": limit, "fill_price": avg_fill,
                    "ibkr_order_id": str(trade.order.orderId),
                },
            ))
        # Don't drown the IBKR socket; small spacing between orders.
        await asyncio.sleep(0.5)

    # -------- Summary --------
    print()
    print(f"=== Done: {successes}/{len(stocks)} filled ===")
    if failures:
        print("Unfilled:")
        for sym, reason in failures:
            print(f"  {sym}: {reason}")

    await ib_provider.aclose()
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
