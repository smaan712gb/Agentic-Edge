"""EDGAR sweep — discover, ingest, diff, and alert on manager filings.

``run_edgar_sweep`` is the unit the scheduler fires and the backfill script
calls. It is idempotent at the filing grain (``filings.accession_no`` is
unique; already-stored filings are skipped) and defensive per-filing (one bad
document never aborts the sweep).

For each active, non-macro-only manager:
  * list filings since the cutoff across all its CIKs,
  * ingest new 13F-HR holdings (deduped, value-normalised),
  * recompute quarter-over-quarter position changes from the latest two
    13F periods,
  * record 13D/13G and Form 4 filings and fire alerts (13D = activist
    stake → Tier-1; new-13F deltas → Tier-2).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

from ..config import get_settings
from ..db import Filing, FundHolding, PositionChange, get_session as db_session
from .repo import HedgeFundRepo

logger = logging.getLogger("agentic_edge.hedge_funds")

# Forms we ingest. 13F-HR is the bulk signal; 13D/G are activist/passive
# stakes; 4 is insider transactions (managers that are >10% owners).
_FORMS = {"13F-HR", "13F-HR/A", "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "4", "4/A"}
_13F_FORMS = {"13F-HR", "13F-HR/A"}
_SC13_FORMS = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}

# Default look-back for the recurring sweep — comfortably covers a missed day
# or two without re-scanning years of history every 15 minutes.
_DEFAULT_LOOKBACK_DAYS = 8


def _as_dt(d: Optional[date]) -> Optional[datetime]:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce a possibly-naive datetime to tz-aware UTC. SQLite (dev) reads
    DateTime(timezone=True) columns back as naive, so any comparison between a
    freshly-built aware datetime and a DB-loaded one must normalise first."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def run_edgar_sweep(
    *,
    since: Optional[date] = None,
    emit_alerts: bool = True,
) -> dict[str, Any]:
    """Sweep EDGAR for all tracked managers. ``since`` defaults to an 8-day
    look-back; the backfill passes a far-back date and ``emit_alerts=False``."""
    settings = get_settings()
    summary: dict[str, Any] = {
        "managers_scanned": 0, "filings_seen": 0, "new_filings": 0,
        "holdings_upserted": 0, "changes_computed": 0, "alerts": 0, "skipped_reason": None,
    }

    if not settings.EDGAR_POLL_ENABLED:
        summary["skipped_reason"] = "EDGAR_POLL_ENABLED=false"
        return summary
    if not settings.EDGAR_USER_AGENT_EMAIL:
        # SEC 403s anonymous requests — no point hitting it.
        summary["skipped_reason"] = "EDGAR_USER_AGENT_EMAIL unset"
        logger.warning("EDGAR sweep skipped: EDGAR_USER_AGENT_EMAIL not set")
        return summary

    try:
        from tradingagents.dataflows.providers.registry import get_provider
        edgar = get_provider("edgar")
    except Exception as e:
        summary["skipped_reason"] = f"edgar provider unavailable: {e}"
        logger.warning("EDGAR sweep skipped: %s", e)
        return summary

    cutoff = since or (datetime.now(timezone.utc).date() - timedelta(days=_DEFAULT_LOOKBACK_DAYS))

    # Snapshot the manager list (id, slug, macro_only, ciks) so we don't hold
    # a session across network calls.
    async with db_session() as s:
        managers = await HedgeFundRepo(s).list_managers(active_only=True)
        manager_snap = [
            (m.id, m.slug, m.name, m.macro_only, [c.cik for c in m.ciks])
            for m in managers
        ]

    for mgr_id, slug, name, macro_only, ciks in manager_snap:
        if macro_only or not ciks:
            continue
        summary["managers_scanned"] += 1
        ingested_13f_for_mgr = False

        for cik in ciks:
            try:
                refs = await edgar.list_filings(cik, forms=_FORMS, since=cutoff)
            except Exception as e:
                logger.warning("EDGAR: list_filings failed for %s (%s): %s", name, cik, e)
                continue
            summary["filings_seen"] += len(refs)

            for ref in refs:
                async with db_session() as s:
                    if await HedgeFundRepo(s).filing_exists(ref.accession_no):
                        continue
                try:
                    n_hold = await _ingest_filing(
                        edgar, mgr_id=mgr_id, manager_name=name, cik=cik, ref=ref,
                        emit_alerts=emit_alerts, summary=summary,
                    )
                    summary["new_filings"] += 1
                    summary["holdings_upserted"] += n_hold
                    if ref.form.upper() in _13F_FORMS:
                        ingested_13f_for_mgr = True
                except Exception as e:
                    logger.exception("EDGAR: failed to ingest %s for %s: %s",
                                     ref.accession_no, name, e)

        if ingested_13f_for_mgr:
            try:
                n_changes = await _recompute_changes(mgr_id)
                summary["changes_computed"] += n_changes
                if emit_alerts and n_changes:
                    await _alert_13f_changes(mgr_id, name)
                    summary["alerts"] += 1
            except Exception as e:
                logger.exception("EDGAR: change recompute failed for %s: %s", name, e)

    logger.info(
        "EDGAR sweep: %d managers, %d new filings, %d holdings, %d changes, %d alerts",
        summary["managers_scanned"], summary["new_filings"],
        summary["holdings_upserted"], summary["changes_computed"], summary["alerts"],
    )
    return summary


async def _ingest_filing(
    edgar: Any, *, mgr_id: int, manager_name: str, cik: str, ref: Any,
    emit_alerts: bool, summary: dict[str, Any],
) -> int:
    """Record one filing and its payload. Returns holdings stored."""
    form = ref.form.upper()
    raw_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{ref.accession_nodashes}/"
    n_holdings = 0

    async with db_session() as s:
        repo = HedgeFundRepo(s)
        filing = Filing(
            manager_id=mgr_id, cik=cik, form_type=ref.form,
            accession_no=ref.accession_no,
            filed_at=_as_dt(ref.filed_at), period_end=_as_dt(ref.period_end),
            raw_url=raw_url,
        )
        s.add(filing)
        await s.flush()
        filing_id = filing.id

        if form in _13F_FORMS:
            holdings = await edgar.fetch_13f_holdings(ref, cik)
            total_value = sum(h.value_usd for h in holdings) or 1.0
            for h in holdings:
                ticker = await repo.resolve_ticker(h.cusip)
                s.add(FundHolding(
                    manager_id=mgr_id, filing_id=filing_id,
                    cusip=h.cusip, issuer_name=h.issuer_name, ticker=ticker,
                    value_usd=h.value_usd, shares=h.shares,
                    put_call_flag=h.put_call_flag,
                    pct_of_portfolio=round(100.0 * h.value_usd / total_value, 3),
                    period_end=_as_dt(ref.period_end),
                ))
            n_holdings = len(holdings)
            filing.parsed_json = {"holdings": n_holdings, "total_value_usd": total_value}

        elif form in _SC13_FORMS:
            sig = await edgar.fetch_sc13(ref, cik)
            filing.parsed_json = {
                "subject": sig.subject_company, "cusip": sig.cusip,
                "percent_of_class": sig.percent_of_class,
            }

        elif form in ("4", "4/A"):
            f4 = await edgar.fetch_form4(ref, cik)
            if f4 is not None:
                filing.parsed_json = {
                    "issuer_ticker": f4.issuer_ticker,
                    "net_open_market_shares": f4.net_open_market_shares,
                    "txn_count": len(f4.transactions),
                }

        # Bump the manager's last-filing marker.
        from ..db import HedgeFundManager
        mgr = await s.get(HedgeFundManager, mgr_id)
        if mgr is not None and ref.filed_at is not None:
            fdt = _as_dt(ref.filed_at)
            prior = _aware(mgr.last_filing_at)
            if prior is None or (fdt and fdt > prior):
                mgr.last_filing_at = fdt

    # Alerts (outside the write txn so a slow Slack post can't hold the row lock).
    if emit_alerts:
        await _alert_for_filing(form, manager_name, ref, summary)
    return n_holdings


async def _alert_for_filing(form: str, manager_name: str, ref: Any, summary: dict[str, Any]) -> None:
    from ..autotrade.alerts import alert
    if form in _SC13_FORMS:
        # Activist/large passive stake — Tier-1.
        level = "warning" if form.startswith("SC 13D") else "info"
        await alert(
            level=level,
            title=f"{manager_name}: {ref.form} filed",
            body=f"Stake disclosure filed {ref.filed_at}. acc={ref.accession_no}",
        )
        summary["alerts"] += 1
    elif form in ("4", "4/A"):
        await alert(
            level="info",
            title=f"{manager_name}: Form 4 (insider) filed",
            body=f"Insider transaction filed {ref.filed_at}. acc={ref.accession_no}",
        )
        summary["alerts"] += 1


async def _recompute_changes(mgr_id: int) -> int:
    """Diff the latest two 13F periods into PositionChange rows. Long
    underlying only (put_call_flag == '') — option notional swings are noisy."""
    async with db_session() as s:
        repo = HedgeFundRepo(s)
        periods = await repo.latest_13f_periods(mgr_id, limit=2)
        if len(periods) < 2:
            return 0
        cur_period, prior_period = periods[0], periods[1]

        def _long_map(holdings: list[FundHolding]) -> dict[str, FundHolding]:
            return {h.cusip: h for h in holdings if h.put_call_flag == ""}

        cur = _long_map(await repo.holdings_for_period(mgr_id, cur_period))
        prior = _long_map(await repo.holdings_for_period(mgr_id, prior_period))

        # Clear any prior compute for this current_period (idempotent rerun).
        existing = (await s.execute(
            select(PositionChange)
            .where(PositionChange.manager_id == mgr_id)
            .where(PositionChange.current_period == cur_period)
        )).scalars().all()
        for row in existing:
            await s.delete(row)
        await s.flush()

        count = 0
        for cusip in set(cur) | set(prior):
            cur_sh = cur[cusip].shares if cusip in cur else 0.0
            prior_sh = prior[cusip].shares if cusip in prior else 0.0
            if cur_sh == prior_sh:
                change_type = "hold"
            elif prior_sh == 0:
                change_type = "new"
            elif cur_sh == 0:
                change_type = "exit"
            elif cur_sh > prior_sh:
                change_type = "add"
            else:
                change_type = "trim"
            change_pct = ((cur_sh - prior_sh) / prior_sh) if prior_sh > 0 else None
            ref_h = cur.get(cusip) or prior.get(cusip)
            s.add(PositionChange(
                manager_id=mgr_id, cusip=cusip,
                ticker=(ref_h.ticker if ref_h else None),
                issuer_name=(ref_h.issuer_name if ref_h else None),
                prior_shares=prior_sh, current_shares=cur_sh,
                change_pct=change_pct, change_type=change_type,
                prior_period=prior_period, current_period=cur_period,
            ))
            count += 1
        return count


async def _alert_13f_changes(mgr_id: int, manager_name: str) -> None:
    """Tier-2 digest: the headline new buys / exits from the latest 13F."""
    from ..autotrade.alerts import alert
    async with db_session() as s:
        changes = await HedgeFundRepo(s).top_changes(mgr_id, limit=5)
    if not changes:
        return
    parts = []
    for c in changes:
        label = c.ticker or c.issuer_name or c.cusip
        parts.append(f"{c.change_type.upper()} {label}")
    await alert(
        level="info",
        title=f"{manager_name}: new 13F — {len(changes)} notable moves",
        body=" · ".join(parts),
    )
