"""EDGAR backfill + acceptance test for the Hedge Fund Signal Tracker.

Pulls every 13F-HR (and 13D/G/Form-4) for the configured managers from a
start date through today, stores them, and prints the top-20 holdings per
manager plus the cross-fund overlap table — the Phase-1 acceptance criterion.

This both *validates* the pipeline end-to-end against live SEC data and
*seeds* the dashboard, so after running it the /managers page is populated.

Requires EDGAR_USER_AGENT_EMAIL (SEC 403s anonymous requests). If you're
behind a TLS-intercepting corporate proxy, set SSL_CERT_FILE to your CA
bundle so cert verification against data.sec.gov succeeds.

Usage (PowerShell):
  python scripts/edgar_backfill.py                  # since 2023-01-01, store + report
  python scripts/edgar_backfill.py --since 2024-01-01
  python scripts/edgar_backfill.py --report-only    # skip the sweep, just print current DB
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime
from pathlib import Path

# Allow running as `python scripts/edgar_backfill.py` from the repo root —
# CPython puts scripts/ on sys.path, not the project root, so add it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("edgar_backfill")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _fmt_usd(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:,.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:,.1f}M"
    return f"${v:,.0f}"


async def main() -> int:
    ap = argparse.ArgumentParser(description="EDGAR backfill + acceptance report.")
    ap.add_argument("--since", type=str, default="2023-01-01",
                    help="Earliest filing date to ingest (YYYY-MM-DD).")
    ap.add_argument("--report-only", action="store_true",
                    help="Skip the network sweep; print from what's already in the DB.")
    args = ap.parse_args()

    from api.app.config import get_settings
    if not get_settings().EDGAR_USER_AGENT_EMAIL and not args.report_only:
        print("ERROR: EDGAR_USER_AGENT_EMAIL is not set — SEC requires it. "
              "Set it in .env and retry (or use --report-only).")
        return 2

    from api.app.db import get_session as db_session
    from api.app.hedge_funds.config_loader import load_managers_from_config
    from api.app.hedge_funds.repo import HedgeFundRepo
    from api.app.hedge_funds.overlap import cross_fund_overlap

    # Ensure managers exist (config is source of truth).
    await load_managers_from_config()

    if not args.report_only:
        since = datetime.strptime(args.since, "%Y-%m-%d").date()
        print(f"\n=== Backfilling EDGAR filings since {since} (this hits SEC; be patient) ===")
        from api.app.hedge_funds.poller import run_edgar_sweep
        summary = await run_edgar_sweep(since=since, emit_alerts=False)
        if summary.get("skipped_reason"):
            print(f"Sweep skipped: {summary['skipped_reason']}")
            return 2
        print(f"Sweep: {summary['managers_scanned']} managers, "
              f"{summary['new_filings']} new filings, "
              f"{summary['holdings_upserted']} holdings, "
              f"{summary['changes_computed']} changes.\n")

    # ---- Top-20 holdings per manager ----
    async with db_session() as s:
        repo = HedgeFundRepo(s)
        managers = await repo.list_managers(active_only=False)
        for m in managers:
            if m.macro_only:
                print(f"\n### {m.name} — macro-only, no single-name holdings tracked")
                continue
            holdings = await repo.latest_holdings(m.id, limit=20)
            print(f"\n### {m.name} — top {len(holdings)} (latest 13F)")
            if not holdings:
                print("   (no holdings ingested)")
                continue
            for i, h in enumerate(holdings, 1):
                label = h.ticker or h.issuer_name[:34]
                pc = f" [{h.put_call_flag}]" if h.put_call_flag else ""
                print(f"   {i:>2}. {label:<34}{pc:<5} {_fmt_usd(h.value_usd):>11}  "
                      f"{h.shares:>14,.0f} sh  {h.pct_of_portfolio or 0:>5.1f}%")

    # ---- Cross-fund overlap (2+ managers) ----
    rows = await cross_fund_overlap(min_managers=2, limit=40)
    print(f"\n\n=== Cross-fund overlap — {len(rows)} names held by 2+ managers ===")
    for r in rows:
        names = ", ".join(mm["name"].split("—")[0].strip() for mm in r["managers"])
        label = r["ticker"] or r["issuer_name"][:30]
        print(f"   {label:<32} {r['manager_count']}×  {_fmt_usd(r['aggregate_value_usd']):>11}   {names}")
    if not rows:
        print("   (none yet — need 2+ managers with ingested 13F holdings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
