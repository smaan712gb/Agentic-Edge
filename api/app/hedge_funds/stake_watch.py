"""Subject-company 13D/13G stake watch — the near-real-time institutional
layer over our HOLDINGS and theme universe.

The hedge-fund poller (``poller.py``) is *filer*-keyed: it watches a fixed set
of legendary managers. This watch is *subject*-keyed: for every name we hold or
track, it asks "who is filing a 13D/13G on THIS company, and is anyone abruptly
cutting their stake?" — the question the manager poller structurally can't
answer.

How it works
------------
* ``EdgarProvider.lookup_cik_by_ticker`` maps each subject ticker to its CIK.
* A subject company's submissions feed includes the Schedule 13D/13G (and
  amendments) filed about it (verified against live EDGAR).
* The FILER is recovered from the accession's leading CIK — the SEC assigns the
  accession under the submitting entity — so we can chain one filer's stake in
  one subject across amendments without parsing fragile cover-page names.
* ``percent_of_class`` comes from the cover-page parse. Comparing it to the same
  filer's prior filing yields ``new / increase / reduce / exit``.

Signals emitted (decision-support; never auto-executes a trade)
---------------------------------------------------------------
* **13D on a holding** → activist stake → Tier-1 alert.
* **Abrupt reduction / exit on a HOLDING** (13G/A dropping materially or below
  the 5% threshold) → warning + a ``stake_reduction_flagged`` audit row. This is
  the operator's early institutional-exit cue; per the high-beta no-drawdown
  policy it FLAGS for review and never auto-closes.
* **New 13D/13G on a universe (non-held) name** → info (accumulation lead).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

from ..config import get_settings
from ..db import StakeFiling, ThemeSymbol, TradeIntent, get_session as db_session

logger = logging.getLogger("agentic_edge.hedge_funds")

# Recurring look-back — comfortably covers a missed run or two. The backfill
# entrypoint can pass a far-back date.
_DEFAULT_LOOKBACK_DAYS = 14

# Material change threshold on percent-of-class (absolute points) before we call
# it an increase/reduce rather than noise/rounding.
_MATERIAL_PCT_DELTA = 1.0
# Below this percent-of-class we treat a reduction as effectively an exit (13G/A
# filed to report the holder ceased to be a >5% owner often reports ~0 or <5).
_EXIT_PCT_FLOOR = 5.0

_OPEN_STATES = ["pmcc_full", "leap_pending", "leap_open", "leap_open_naked", "closing"]
_OPEN_INTENT_STATUSES = ["filled", "submitted"]

# Event filings (Tier-1, beyond 13D/13G) that change a close/hold/add call on an
# OPTIONS book — pulled from the SAME per-subject filings list, so no extra
# EDGAR calls. Each maps to a category + a one-line "why it matters".
#   tender / merger / going-private → deal event: stock pins to deal price and
#     IV collapses → a LEAP's remaining upside evaporates (close/hold).
#   dilution → secondary/ATM/shelf takedown → near-term bearish (trim/hold).
#   late_filing → can't file on time → accounting red flag (thesis-break review).
# Accession-deduped in-process (rare forms; re-alert once after restart is fine).
_EVENT_SEEN: set[str] = set()


def _classify_event_form(form: str) -> Optional[tuple[str, str]]:
    """Map a form type to (category, why). None if it's not a watched event."""
    f = (form or "").upper()
    if "14D" in f or "SC TO" in f or "SCHEDULE TO" in f or f.startswith("TO-"):
        return "tender", "tender offer — stock pins to deal price, IV collapses"
    if "13E3" in f or "13E-3" in f:
        return "going_private", "going-private — deal event, IV collapses"
    if "DEFM14A" in f or f == "425" or f.startswith("425 ") or f == "S-4":
        return "merger", "merger filing — underlying may change (cash vs stock)"
    if f.startswith("424B") or f == "S-3":
        return "dilution", "secondary/shelf — dilution, near-term bearish"
    if f.startswith("NT 10-K") or f.startswith("NT 10-Q") or f in ("NT 10-K", "NT 10-Q"):
        return "late_filing", "late filing — accounting red flag"
    return None


def _as_dt(d) -> Optional[datetime]:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def _filer_cik_from_accession(accession_no: str) -> str:
    """The accession's leading 10 digits are the submitting (filer) CIK."""
    head = (accession_no or "").split("-")[0]
    digits = "".join(ch for ch in head if ch.isdigit())
    return digits.zfill(10) if digits else ""


def _classify(prior: Optional[float], current: Optional[float], *, is_amendment: bool) -> str:
    """Map a (prior%, current%) pair to a change_type."""
    if prior is None:
        return "new" if not is_amendment else "initial"
    if current is None:
        return "hold"
    delta = current - prior
    if current <= 0.0 or (current < _EXIT_PCT_FLOOR <= prior):
        return "exit"
    if delta <= -_MATERIAL_PCT_DELTA:
        return "reduce"
    if delta >= _MATERIAL_PCT_DELTA:
        return "increase"
    return "hold"


async def _subject_symbols() -> tuple[list[str], set[str]]:
    """Return (ordered subject tickers, set-of-holding tickers). Holdings come
    first so they're processed even if a universe scan is truncated."""
    async with db_session() as s:
        held_rows = (
            await s.execute(
                select(TradeIntent.symbol)
                .where(TradeIntent.status.in_(_OPEN_INTENT_STATUSES))
                .where(TradeIntent.position_state.in_(_OPEN_STATES))
                .distinct()
            )
        ).all()
        theme_rows = (await s.execute(select(ThemeSymbol.symbol).distinct())).all()
    holdings = {(r[0] or "").upper() for r in held_rows if r[0]}
    universe = {(r[0] or "").upper() for r in theme_rows if r[0]}
    ordered = list(holdings) + sorted(universe - holdings)
    return ordered, holdings


async def run_stake_watch(
    *, since: Optional[date] = None, emit_alerts: bool = True,
) -> dict[str, Any]:
    """Sweep 13D/13G filings for our holdings + theme universe by subject CIK."""
    settings = get_settings()
    summary: dict[str, Any] = {
        "subjects_scanned": 0, "filings_seen": 0, "new_filings": 0,
        "reductions_flagged": 0, "activist_flagged": 0, "event_filings": 0,
        "alerts": 0, "skipped_reason": None,
    }
    if not settings.EDGAR_POLL_ENABLED:
        summary["skipped_reason"] = "EDGAR_POLL_ENABLED=false"
        return summary
    if not settings.EDGAR_USER_AGENT_EMAIL:
        summary["skipped_reason"] = "EDGAR_USER_AGENT_EMAIL unset"
        logger.warning("stake watch skipped: EDGAR_USER_AGENT_EMAIL not set")
        return summary

    try:
        from tradingagents.dataflows.providers.registry import get_provider
        edgar = get_provider("edgar")
    except Exception as e:
        summary["skipped_reason"] = f"edgar provider unavailable: {e}"
        logger.warning("stake watch skipped: %s", e)
        return summary

    cutoff = since or (datetime.now(timezone.utc).date() - timedelta(days=_DEFAULT_LOOKBACK_DAYS))
    subjects, holdings = await _subject_symbols()
    if not subjects:
        return summary

    # Preload known accessions so repeat runs are cheap and idempotent.
    async with db_session() as s:
        known = {
            r[0] for r in (
                await s.execute(select(StakeFiling.accession_no))
            ).all()
        }

    for ticker in subjects:
        try:
            cik = await edgar.lookup_cik_by_ticker(ticker)
        except Exception as e:
            logger.debug("stake watch: CIK lookup failed for %s: %s", ticker, e)
            continue
        if not cik:
            continue
        summary["subjects_scanned"] += 1
        try:
            refs = await edgar.list_filings(cik, since=cutoff)
        except Exception as e:
            logger.warning("stake watch: list_filings failed for %s (%s): %s", ticker, cik, e)
            continue

        # Only Schedule 13D/13G family; oldest-first so amendment chains compute
        # prior% correctly within a single run.
        sc13 = [r for r in refs
                if ("13D" in r.form.upper() or "13G" in r.form.upper())
                and r.accession_no not in known]
        sc13.sort(key=lambda r: (r.filed_at or date.min))
        summary["filings_seen"] += len(sc13)

        for ref in sc13:
            try:
                await _ingest_stake(
                    edgar, ticker=ticker, subject_cik=cik, ref=ref,
                    is_holding=ticker in holdings, emit_alerts=emit_alerts, summary=summary,
                )
                known.add(ref.accession_no)
                summary["new_filings"] += 1
            except Exception as e:
                logger.exception("stake watch: ingest failed for %s %s: %s",
                                 ticker, ref.accession_no, e)

        # Deal / dilution / late-filing events from the SAME refs (no extra calls).
        for ref in refs:
            if ref.accession_no in _EVENT_SEEN:
                continue
            cat = _classify_event_form(ref.form)
            if cat is None:
                continue
            _EVENT_SEEN.add(ref.accession_no)
            summary["event_filings"] += 1
            if emit_alerts:
                try:
                    await _alert_event_filing(
                        ticker=ticker, ref=ref, category=cat[0], why=cat[1],
                        is_holding=ticker in holdings, summary=summary,
                    )
                except Exception as e:
                    logger.exception("stake watch: event alert failed for %s %s: %s",
                                     ticker, ref.accession_no, e)

    logger.info(
        "stake watch: %d subjects, %d new 13D/G, %d reductions, %d activist",
        summary["subjects_scanned"], summary["new_filings"],
        summary["reductions_flagged"], summary["activist_flagged"],
    )
    return summary


async def _ingest_stake(
    edgar: Any, *, ticker: str, subject_cik: str, ref: Any,
    is_holding: bool, emit_alerts: bool, summary: dict[str, Any],
) -> None:
    form = ref.form.upper()
    is_activist = "13D" in form
    is_amendment = "/A" in form or form.endswith("A")
    filer_cik = _filer_cik_from_accession(ref.accession_no)

    try:
        sig = await edgar.fetch_sc13(ref, subject_cik)
        current_pct = sig.percent_of_class
    except Exception:
        current_pct = None

    # Prior filing by the SAME filer on the SAME subject (most recent before this).
    async with db_session() as s:
        prior_row = (
            await s.execute(
                select(StakeFiling)
                .where(StakeFiling.subject_cik == subject_cik)
                .where(StakeFiling.filer_cik == filer_cik)
                .order_by(StakeFiling.filed_at.desc().nullslast())
                .limit(1)
            )
        ).scalars().first()
        prior_pct = prior_row.percent_of_class if prior_row else None

        change_type = _classify(prior_pct, current_pct, is_amendment=is_amendment)
        raw_url = f"https://www.sec.gov/Archives/edgar/data/{int(subject_cik)}/{ref.accession_nodashes}/"
        s.add(StakeFiling(
            subject_ticker=ticker, subject_cik=subject_cik, filer_cik=filer_cik,
            form_type=ref.form, accession_no=ref.accession_no, filed_at=_as_dt(ref.filed_at),
            percent_of_class=current_pct, prior_percent=prior_pct,
            change_type=change_type, is_activist=is_activist, is_holding=is_holding,
            raw_url=raw_url,
        ))

    if emit_alerts:
        await _alert_stake(
            ticker=ticker, ref=ref, filer_cik=filer_cik, is_holding=is_holding,
            is_activist=is_activist, current_pct=current_pct, prior_pct=prior_pct,
            change_type=change_type, summary=summary,
        )


async def _alert_stake(
    *, ticker: str, ref: Any, filer_cik: str, is_holding: bool, is_activist: bool,
    current_pct: Optional[float], prior_pct: Optional[float], change_type: str,
    summary: dict[str, Any],
) -> None:
    from ..autotrade.alerts import alert

    pct_str = f"{current_pct:.2f}%" if current_pct is not None else "?"
    prior_str = f"{prior_pct:.2f}%" if prior_pct is not None else "—"

    # Component C: abrupt reduction/exit on a name WE HOLD — the early
    # institutional-exit cue. Flag-only (operator confirms); never auto-closes.
    if is_holding and change_type in ("reduce", "exit"):
        summary["reductions_flagged"] += 1
        await _record_holding_exit_signal(
            ticker=ticker, ref=ref, filer_cik=filer_cik,
            current_pct=current_pct, prior_pct=prior_pct, change_type=change_type,
        )
        await alert(
            level="warning",
            title=f"INSTITUTIONAL {change_type.upper()} on holding: {ticker}",
            body=(f"Filer CIK {filer_cik} {ref.form}: {prior_str} → {pct_str} "
                  f"({ref.filed_at}). Early institutional-exit signal — review "
                  f"(flag-only; not auto-closed on this high-beta book)."),
        )
        summary["alerts"] += 1
        return

    if is_activist:
        summary["activist_flagged"] += 1
        await alert(
            level="critical" if is_holding else "warning",
            title=f"ACTIVIST 13D{' on holding' if is_holding else ''}: {ticker}",
            body=(f"Filer CIK {filer_cik} filed {ref.form} ({pct_str} of class, "
                  f"{ref.filed_at}). acc={ref.accession_no}."),
        )
        summary["alerts"] += 1
        return

    # New / increased passive stake — accumulation lead.
    if change_type in ("new", "increase"):
        await alert(
            level="info",
            title=f"13G {change_type} on {'holding' if is_holding else 'universe'}: {ticker}",
            body=f"Filer CIK {filer_cik} {ref.form}: {prior_str} → {pct_str} ({ref.filed_at}).",
        )
        summary["alerts"] += 1


async def _alert_event_filing(
    *, ticker: str, ref: Any, category: str, why: str,
    is_holding: bool, summary: dict[str, Any],
) -> None:
    """Alert + audit a deal/dilution/late-filing event. Flag-only (operator
    confirms any close); louder on names we hold."""
    from ..autotrade.alerts import alert
    from ..autotrade.auto_gate import AutoGateResult, record_auto_action

    held_tag = " on HOLDING" if is_holding else ""
    # Deal events on a position we hold are the highest-urgency here.
    deal = category in ("tender", "going_private", "merger")
    level = "critical" if (deal and is_holding) else ("warning" if is_holding else "info")

    async with db_session() as s:
        await record_auto_action(
            s, loop="stake_watch", action_type=f"event_filing_{category}",
            gate_result=AutoGateResult(passed=True, failures=[]),
            symbol=ticker,
            payload={
                "category": category, "why": why, "form": ref.form,
                "accession": ref.accession_no, "filed_at": str(ref.filed_at),
                "is_holding": is_holding,
                "disposition": "flag_only_operator_confirms",
            },
            outcome="flagged",
        )
    await alert(
        level=level,
        title=f"{ref.form} ({category}){held_tag}: {ticker}",
        body=f"{why}. Filed {ref.filed_at}. acc={ref.accession_no}"
             + ("  Review close/hold — flag-only, not auto-closed." if is_holding else "."),
    )
    summary["alerts"] += 1


async def _record_holding_exit_signal(
    *, ticker: str, ref: Any, filer_cik: str,
    current_pct: Optional[float], prior_pct: Optional[float], change_type: str,
) -> None:
    """Audit row mirroring the maintenance loop's auto_actions so the operator
    sees stake reductions on holdings in the same trail as other exit signals."""
    from ..autotrade.auto_gate import AutoGateResult, record_auto_action
    async with db_session() as s:
        await record_auto_action(
            s, loop="stake_watch", action_type="stake_reduction_flagged",
            gate_result=AutoGateResult(passed=True, failures=[]),
            symbol=ticker,
            payload={
                "filer_cik": filer_cik, "form": ref.form,
                "accession": ref.accession_no,
                "prior_percent": prior_pct, "current_percent": current_pct,
                "change_type": change_type,
                "disposition": "flag_only_operator_confirms",
                "filed_at": str(ref.filed_at),
            },
            outcome="flagged",
        )
