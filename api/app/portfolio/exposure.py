"""Delta-adjusted exposure — what the book is actually worth.

Every exposure gate in this system measured PREMIUM PAID. For a long-call book
that is the maximum loss, which makes it the right number for a risk-of-ruin
question and the wrong number for a "how invested am I" question.

Measured live on 2026-08-18:

    premium at risk      $530,859   = 39.9% of NAV   <- what the gates saw
    delta-adjusted       $828,755   = 62.2% of NAV   <- what was actually owned
    implied leverage     1.56x

The system believed it held 40% with 60% of NAV in dry powder. It held 62%.
Every add/trim/halt decision was taken against a denominator understating true
exposure by 22 points of NAV. The old aggregate cap of "100% of NAV in
premium" would have permitted roughly 156% real exposure.

Deep-ITM LEAPs run 0.80-0.90 delta, so a $100k premium position controls
$120k-$210k of stock depending on strike and spot. That leverage is the point
of the structure; it just has to be measured.

Definitions used here:

    notional   qty x 100 x delta x spot        (options)
               qty x price                     (stock)
    exposure   notional / NAV

Delta comes from the broker's option quote. When the quote carries no greeks
(common after hours) a moneyness-based estimate is used rather than dropping
the position from the total, because silently under-counting exposure is the
exact failure this module exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("agentic_edge.portfolio")

# Fallback delta for a long-dated call when the broker returns no greeks.
# The book is deliberately deep ITM (LEAP_DELTA_TARGET 0.85), so this is close
# for the intended structure and errs slightly HIGH — over-stating exposure is
# the safe direction for a gate deciding whether to add more.
_DEFAULT_LEAP_DELTA = 0.85
# Below this moneyness a call is far OTM; treat its delta as small rather than
# assuming the deep-ITM default.
_OTM_DELTA = 0.25


def estimate_delta(*, spot: Optional[float], strike: Optional[float]) -> float:
    """Crude delta from moneyness, for when the broker returns no greeks. Pure.

    Not a pricing model — a guard. The alternative when greeks are missing is
    to count the position as zero exposure, which would tell the system it has
    room to add when it does not.
    """
    if not spot or not strike or spot <= 0 or strike <= 0:
        return _DEFAULT_LEAP_DELTA
    moneyness = spot / strike
    if moneyness >= 1.25:        # deep ITM
        return 0.92
    if moneyness >= 1.05:        # comfortably ITM
        return 0.80
    if moneyness >= 0.95:        # near the money
        return 0.55
    if moneyness >= 0.85:
        return 0.35
    return _OTM_DELTA


def position_notional(
    *, qty: float, sec_type: str, delta: Optional[float],
    spot: Optional[float], strike: Optional[float], price: Optional[float] = None,
) -> float:
    """Delta-adjusted notional for one position. Pure.

    Options are qty x 100 x delta x spot. Stock has delta 1 by definition, so
    the same formula collapses to qty x price.
    """
    st = (sec_type or "").upper()
    if st in ("STK", "", "STOCK"):
        return float(qty) * float(price or spot or 0.0)
    d = delta if delta is not None else estimate_delta(spot=spot, strike=strike)
    return float(qty) * 100.0 * float(d) * float(spot or 0.0)


@dataclass
class PositionExposure:
    symbol: str
    qty: float
    sec_type: str
    strike: Optional[float]
    expiry: Optional[str]
    premium: float          # what was paid = max loss on a long call
    delta: Optional[float]
    delta_source: str       # "broker" | "estimated"
    spot: Optional[float]
    notional: float         # delta-adjusted
    sleeve: str = "core"

    @property
    def leverage(self) -> float:
        return (self.notional / self.premium) if self.premium else 0.0


@dataclass
class BookExposure:
    nav: float
    positions: list[PositionExposure] = field(default_factory=list)
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    degraded: bool = False          # some position could not be priced
    notes: list[str] = field(default_factory=list)

    @property
    def premium(self) -> float:
        return sum(p.premium for p in self.positions)

    @property
    def notional(self) -> float:
        return sum(p.notional for p in self.positions)

    @property
    def exposure_pct(self) -> float:
        """Delta-adjusted exposure as a fraction of NAV — THE number."""
        return (self.notional / self.nav) if self.nav > 0 else 0.0

    @property
    def premium_pct(self) -> float:
        return (self.premium / self.nav) if self.nav > 0 else 0.0

    @property
    def leverage(self) -> float:
        return (self.notional / self.premium) if self.premium else 0.0

    def by_sleeve(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self.positions:
            out[p.sleeve] = out.get(p.sleeve, 0.0) + p.notional
        return out

    def sleeve_pct(self) -> dict[str, float]:
        if self.nav <= 0:
            return {}
        return {k: v / self.nav for k, v in self.by_sleeve().items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "nav": round(self.nav, 2),
            "premium": round(self.premium, 2),
            "premium_pct": round(self.premium_pct, 4),
            "notional": round(self.notional, 2),
            "exposure_pct": round(self.exposure_pct, 4),
            "leverage": round(self.leverage, 3),
            "degraded": self.degraded,
            "notes": self.notes,
            "sleeves": {k: round(v, 4) for k, v in self.sleeve_pct().items()},
            "positions": [
                {
                    "symbol": p.symbol, "qty": p.qty, "sec_type": p.sec_type,
                    "strike": p.strike, "expiry": p.expiry,
                    "premium": round(p.premium, 2), "delta": p.delta,
                    "delta_source": p.delta_source, "spot": p.spot,
                    "notional": round(p.notional, 2),
                    "leverage": round(p.leverage, 2), "sleeve": p.sleeve,
                    "pct_of_nav": round(p.notional / self.nav, 4) if self.nav else 0.0,
                }
                for p in sorted(self.positions, key=lambda x: -x.notional)
            ],
        }


async def _spot_map(symbols: list[str]) -> dict[str, float]:
    """Batch spot quotes.

    FMP is used because it is the vendor proven to work through this
    environment's TLS-intercepting proxy; the broker's equity quote path
    returned empty during testing, and yfinance dies on curl_cffi cert
    verification here.
    """
    out: dict[str, float] = {}
    if not symbols:
        return out
    try:
        from tradingagents.dataflows.providers.fmp import FmpProvider
        fmp = FmpProvider()
        try:
            for i in range(0, len(symbols), 40):
                chunk = symbols[i:i + 40]
                body = await fmp._http.get_json(
                    "/stable/batch-quote",
                    params={"symbols": ",".join(chunk), "apikey": fmp._api_key},
                )
                for r in (body or []):
                    try:
                        out[str(r.get("symbol", "")).upper()] = float(r["price"])
                    except (TypeError, ValueError, KeyError):
                        continue
        finally:
            try:
                await fmp.aclose()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("exposure: spot fetch failed: %s", e)
    return out


async def compute_book_exposure(ib: Any, *, with_greeks: bool = True) -> BookExposure:
    """Delta-adjusted exposure for the whole book.

    ``with_greeks=False`` skips the per-contract quote round trips and relies
    on the moneyness estimate — a cheap read for use inside a hot loop. The
    estimate errs high, so a gate using it stays conservative about adding.
    """
    from sqlalchemy import select

    from ..db import TradeIntent, get_session as db_session

    nav = 0.0
    try:
        acct = await ib.get_account_summary()
        nav = float(acct.get("NetLiquidation") or acct.get("EquityWithLoanValue") or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("exposure: NAV read failed: %s", e)

    try:
        raw = [p for p in await ib.get_positions() if float(p.get("qty") or 0) != 0]
    except Exception as e:  # noqa: BLE001
        logger.warning("exposure: positions read failed: %s", e)
        return BookExposure(nav=nav, degraded=True, notes=[f"positions unreadable: {e}"])

    # Sleeve tags live on the managing intent's walking_config.
    sleeves: dict[str, str] = {}
    try:
        async with db_session() as s:
            rows = (await s.execute(
                select(TradeIntent.symbol, TradeIntent.walking_config)
                .where(TradeIntent.status.in_(["filled", "submitting", "submitted", "closing"]))
                .where(TradeIntent.position_state.in_(
                    ["pmcc_full", "leap_pending", "leap_open", "leap_open_naked", "closing"]))
            )).all()
        for sym, cfg in rows:
            sleeves[(sym or "").upper()] = str((cfg or {}).get("sleeve") or "core")
    except Exception as e:  # noqa: BLE001
        logger.debug("exposure: sleeve lookup failed: %s", e)

    spots = await _spot_map(sorted({(p.get("symbol") or "").upper() for p in raw}))

    book = BookExposure(nav=nav)
    for p in raw:
        sym = (p.get("symbol") or "").upper()
        qty = float(p.get("qty") or 0)
        sec = str(p.get("secType") or p.get("sec_type") or "").upper()
        strike = p.get("strike")
        strike = float(strike) if strike is not None else None
        premium = abs(qty) * float(p.get("avg_price") or 0) * (100.0 if sec == "OPT" else 1.0)
        spot = spots.get(sym)

        delta: Optional[float] = None
        source = "estimated"
        if with_greeks and sec == "OPT":
            try:
                q = await ib.get_option_quote(
                    symbol=sym, expiry=str(p.get("expiry") or ""),
                    strike=float(strike or 0), right=str(p.get("right") or "C")[:1] or "C",
                )
                d = q.get("delta")
                if d is not None:
                    delta, source = float(d), "broker"
            except Exception:  # noqa: BLE001 — fall through to the estimate
                pass
        if delta is None:
            delta = estimate_delta(spot=spot, strike=strike)

        if spot is None and sec == "OPT":
            book.degraded = True
            book.notes.append(f"{sym}: no spot — notional understated")

        book.positions.append(PositionExposure(
            symbol=sym, qty=qty, sec_type=sec or "STK", strike=strike,
            expiry=str(p.get("expiry") or "") or None, premium=premium,
            delta=round(float(delta), 4), delta_source=source, spot=spot,
            notional=position_notional(
                qty=qty, sec_type=sec, delta=delta, spot=spot, strike=strike,
                price=float(p.get("last_price") or 0) or None),
            sleeve=sleeves.get(sym, "core"),
        ))

    if nav <= 0:
        book.degraded = True
        book.notes.append("NAV unreadable — exposure percentages are meaningless")
    return book
