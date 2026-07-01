"""One-time reconciliation after the 2026-06-29 loop-hang outage.

Two drifts the standard tooling can't fix on its own:

  * ORPHANS — 6 LEAP option positions live at the broker with no managing
    TradeIntent. The maint-loop orphan-adopt path only adopts stocks
    (secType=="STK") and /api/admin/positions/import is stock-only too, so
    option orphans need this explicit adopt. We create one filled `leap_only`
    intent per position (status=filled / position_state=leap_open) so the
    maintenance loop owns it for exits/rotation and the health monitor stops
    flagging it.

  * PHANTOMS — 6 filled `leap_only` intents (5 symbols; AVGO twice) whose
    option is no longer at the broker. We flip them to closed.

Broker source: IBKR portfolio stream in logs/agentic_edge.log @ 2026-06-30
09:23 ET (broker-authoritative). Verified at build time that all 6 orphan
conids are present in the live stream and all 5 phantom symbols are absent.
`averageCost` from ib_insync is total-per-contract (premium x 100); the
provider/intents store the PER-SHARE premium, so premium = averageCost/100
(matches the per-share marketPrice and the existing leap_only fill prices).

Usage:
    python scripts/reconcile_orphans_phantoms_20260630.py            # dry-run
    python scripts/reconcile_orphans_phantoms_20260630.py --commit    # apply
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from api.app.db import TradeAuditLog, TradeIntent, get_session

# --- Orphan LEAP positions (broker truth, 2026-06-30 09:23 ET) --------------
# premium is per-share = averageCost / 100.
ORPHANS = [
    # symbol, strike, expiry(YYYYMMDD), qty, conid, premium
    ("XLK",  142.5, "20271217",  6, 767688819,  60.00),
    ("QQQ",  555.0, "20270917",  2, 822844815, 208.07),
    ("NVDA", 150.0, "20270917",  3, 829544854,  66.25),
    ("IWM",  230.0, "20270917", 11, 831747786,  84.87),
    ("IWM",  235.0, "20270917",  1, 831747847,  82.14),
    ("SPY",  570.0, "20270917",  4, 868238505, 205.23),
]

# --- Phantom symbols (filled leap_only intents with no live broker position) -
PHANTOM_SYMBOLS = ["AMAT", "AMD", "AVGO", "QCOM", "STM"]


async def main(commit: bool) -> None:
    now = datetime.now(timezone.utc)
    mode = "COMMIT" if commit else "DRY-RUN"
    print(f"\n=== Orphan/phantom reconciliation [{mode}] ===\n")

    async with get_session() as s:
        # ---------- ORPHANS: adopt ----------
        print("ORPHANS — adopt as managed leap_only intents:")
        adopt_plan = []
        for sym, strike, expiry, qty, conid, premium in ORPHANS:
            # Idempotency: skip if an active intent already covers this conid.
            existing = (await s.execute(
                select(TradeIntent.id, TradeIntent.walking_config)
                .where(TradeIntent.symbol == sym)
                .where(TradeIntent.status == "filled")
                .where(TradeIntent.position_state.in_(
                    ["leap_open", "leap_open_naked", "pmcc_full", "leap_pending", "closing"]))
            )).all()
            already = any(int((cfg or {}).get("leap_conid") or 0) == conid for _id, cfg in existing)
            if already:
                print(f"  SKIP {sym:5} {strike} {expiry} x{qty}  (conid {conid} already has an active intent)")
                continue
            adopt_plan.append((sym, strike, expiry, qty, conid, premium))
            print(f"  NEW  {sym:5} {strike:>7} C {expiry} x{qty:<3} @ ${premium:>7.2f}/sh  "
                  f"notional ${premium*qty*100:>12,.0f}  conid {conid}")

        # ---------- PHANTOMS: close ----------
        print("\nPHANTOMS — close (filled leap_only intent, no live broker position):")
        phantoms = (await s.execute(
            select(TradeIntent)
            .where(TradeIntent.symbol.in_(PHANTOM_SYMBOLS))
            .where(TradeIntent.status == "filled")
            .where(TradeIntent.structure == "leap_only")
            .where(TradeIntent.position_state == "leap_open")
            .order_by(TradeIntent.symbol)
        )).scalars().all()
        for i in phantoms:
            print(f"  CLOSE {i.symbol:5} strike {i.leap_strike} {i.leap_expiry} x{i.qty:<4} "
                  f"fill ${i.leap_fill_price}  intent {i.id[:8]}")

        print(f"\nSummary: adopt {len(adopt_plan)} orphan(s), close {len(phantoms)} phantom(s).")

        if not commit:
            print("\nDRY-RUN — no changes written. Re-run with --commit to apply.\n")
            return

        # ---------- apply ----------
        for sym, strike, expiry, qty, conid, premium in adopt_plan:
            intent = TradeIntent(
                symbol=sym, side="BUY", qty=float(qty), order_type="LMT",
                limit_px=premium, status="filled",
                structure="leap_only", position_state="leap_open",
                leap_strike=strike, leap_expiry=expiry, leap_qty=int(qty),
                leap_fill_price=premium, net_debit_filled=premium,
                leap_filled_at=now,
                entry_strategy="adopted_orphan_leap",
                rationale=(
                    f"Adopted orphan LEAP from broker: {qty}x {sym} {strike}C {expiry} "
                    f"@ ${premium:.2f}/sh. Had no managing intent after the 2026-06-29 "
                    f"outage; adopting so the maintenance loop owns its exits/rotation."
                ),
                walking_config={
                    "leap_conid": conid,
                    "source": "reconcile_orphans_phantoms_20260630",
                    "adopted_at": now.isoformat(),
                },
            )
            s.add(intent)
            await s.flush()
            s.add(TradeAuditLog(
                intent_id=intent.id, action="orphan_leap_adopted", outcome="filled",
                payload={"symbol": sym, "strike": strike, "expiry": expiry, "qty": qty,
                         "conid": conid, "leap_premium": premium,
                         "note": "Broker holds this LEAP; no intent existed. Adopted into management."},
            ))
            print(f"  + adopted {sym} -> intent {intent.id[:8]}")

        for i in phantoms:
            i.status = "closed"
            i.position_state = "closed"
            s.add(TradeAuditLog(
                intent_id=i.id, action="phantom_reconciled_closed", outcome="closed",
                payload={"symbol": i.symbol, "leap_strike": i.leap_strike,
                         "leap_expiry": i.leap_expiry, "qty": i.qty,
                         "note": "Filled leap_only intent with no live broker position "
                                 "(verified absent from portfolio stream). Reconciled to closed."},
            ))
            print(f"  - closed phantom {i.symbol} intent {i.id[:8]}")

        print(f"\nCOMMIT done: {len(adopt_plan)} adopted, {len(phantoms)} closed.\n")


if __name__ == "__main__":
    asyncio.run(main(commit="--commit" in sys.argv))
