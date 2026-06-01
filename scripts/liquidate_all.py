"""One-time comprehensive liquidation — flatten the entire book.

Cancels all working orders, then closes EVERY open position:
  * stock long  -> SELL          stock short -> BUY (to close)
  * option long -> SELL (close)  option short -> BUY (to close)

Marketable limit orders (cross the spread) so a forced flatten actually
fills. Dry-run by default — prints the exact plan and sends nothing. Pass
--execute to fire.

Intended to run at the regular open (9:30 ET): US options don't trade
pre-market, so a complete flatten requires RTH. The script warns if run
outside RTH and (for options) those orders simply won't fill until the open.

Usage (PowerShell):
  python scripts/liquidate_all.py                 # dry-run plan
  python scripts/liquidate_all.py --execute       # flatten the book
  python scripts/liquidate_all.py --execute --pre-market-stocks  # ETH stock fills
"""

from __future__ import annotations

import sys, asyncio, argparse
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.app.config  # noqa: F401 — load .env / aliases


async def _quote(ib, contract):
    """Best-effort bid/ask/last for marketable pricing."""
    try:
        t = ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(1.2)
        bid = float(t.bid or 0); ask = float(t.ask or 0); last = float(t.last or 0)
        try: ib.cancelMktData(contract)
        except Exception: pass
        return bid, ask, last
    except Exception:
        return 0.0, 0.0, 0.0


def _limit_for(side: str, bid: float, ask: float, last: float) -> float | None:
    """Marketable limit: sell at bid, buy at ask; fall back to last; small
    buffer to ensure crossing."""
    if side == "SELL":
        px = bid if bid > 0 else last
        return round(px * 0.995, 2) if px > 0 else None   # 0.5% under to cross
    else:  # BUY to close
        px = ask if ask > 0 else last
        return round(px * 1.005, 2) if px > 0 else None


async def main() -> int:
    ap = argparse.ArgumentParser(description="One-time full liquidation.")
    ap.add_argument("--execute", action="store_true", help="Actually send orders (default: dry-run)")
    ap.add_argument("--pre-market-stocks", action="store_true",
                    help="Flag stock orders outsideRth so they fill in ETH (options always RTH)")
    args = ap.parse_args()

    from datetime import datetime
    import pytz
    now_et = datetime.now(pytz.timezone("America/New_York"))
    is_rth = now_et.weekday() < 5 and (now_et.hour, now_et.minute) >= (9, 30) and now_et.hour < 16
    print(f"=== Liquidation {'EXECUTE' if args.execute else 'DRY-RUN'} | {now_et:%Y-%m-%d %H:%M ET} | RTH={is_rth} ===")
    if not is_rth:
        print("WARNING: outside regular hours — option orders will NOT fill until 9:30 ET.")

    from tradingagents.dataflows.providers.ibkr import IbkrProvider
    from ib_insync import LimitOrder, Contract  # type: ignore
    prov = IbkrProvider(client_id=88)
    ib = await prov._ensure_connected()

    # 1) Cancel all working orders.
    open_trades = [t for t in ib.openTrades() if t.orderStatus.status not in ("Filled", "Cancelled")]
    print(f"\nWorking orders to cancel: {len(open_trades)}")
    if args.execute:
        try:
            ib.reqGlobalCancel(); print("  reqGlobalCancel sent")
        except Exception as e:
            print(f"  global cancel failed: {e}")

    # 2) Flatten positions.
    positions = await prov.get_positions()
    plan = []
    for p in positions:
        qty = float(p.get("qty") or 0)
        if qty == 0:
            continue
        sec = str(p.get("secType") or "STK").upper()
        side = "SELL" if qty > 0 else "BUY"   # BUY closes shorts
        plan.append((p, sec, side, abs(qty)))

    print(f"\nPositions to flatten: {len(plan)}")
    sent = 0
    for p, sec, side, qty in plan:
        sym = p.get("symbol"); cid = int(p.get("conid") or 0)
        label = f"{sym} {sec}" + (f" {p.get('right')}{p.get('strike')} {p.get('expiry')}" if sec == "OPT" else "")
        if not cid:
            print(f"  SKIP {label}: no conid"); continue
        try:
            qc = await ib.qualifyContractsAsync(Contract(conId=cid, exchange="SMART"))
            contract = qc[0] if qc else None
        except Exception as e:
            print(f"  SKIP {label}: qualify failed ({e})"); continue
        if contract is None:
            print(f"  SKIP {label}: could not qualify"); continue

        bid, ask, last = await _quote(ib, contract)
        limit = _limit_for(side, bid, ask, last)
        if limit is None:
            print(f"  {side} {qty:g} {label}: NO QUOTE — skipped (will retry)"); continue

        outside = bool(args.pre_market_stocks and sec == "STK")
        print(f"  {side} {qty:g} {label} @ LMT {limit} (bid={bid} ask={ask} last={last})"
              + (" [ETH]" if outside else ""))
        if args.execute:
            try:
                order = LimitOrder(side, qty, limit, tif="DAY", outsideRth=outside)
                ib.placeOrder(contract, order)
                sent += 1
            except Exception as e:
                print(f"     submit failed: {e}")

    print(f"\n{'SENT ' + str(sent) + ' orders.' if args.execute else 'DRY-RUN — nothing sent.'}")
    try: await prov.aclose()
    except Exception: pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
