"""Sector-dispersion IC study — does the premarket ranking predict the day?

The go/no-go test for an intraday market-neutral sector strategy. It asks one
question, in the cheapest form that can answer it:

    Does a sector's *residual* overnight move (its gap beyond what its beta
    predicts) rank-predict its *residual* return from 09:33 to a later point
    in the same session?

If the answer is "no", nothing downstream matters — the breadth data, the
factor overlay, the execution layer are all elaborations on a signal with no
content. That is the point of running this first: it costs a day instead of a
quarter.

WHAT IT MEASURES
  1. **Rank IC** — Spearman(signal, forward residual return) computed
     cross-sectionally per day, then averaged over days with a t-stat on the
     daily series. This is the Grinold/Kahn construction: the IC's *stability*
     across days is the evidence, not a single pooled correlation.
  2. **The tradeable version** — the top-3 vs bottom-3 spread return, gross and
     net of an explicit round-trip cost. IC can be real and still unprofitable;
     this is the number that decides.
  3. **Beta drift** — the net beta a naive 50/50 dollar-neutral top-3/bottom-3
     book carries. Quantifies how much of any raw edge is just market
     direction wearing a dispersion costume.
  4. **Alpha decay** — IC at 09:33→10:30 vs 09:33→15:50, which says whether the
     edge (if any) lives in the first hour or persists to the close.

DISCIPLINE (same as the other harnesses in this package): strictly offline and
read-only. It recommends; it never writes config and never trades.

    cd api && python -m app.research.fetch_sector_bars     # populate cache once
    cd api && python -m app.research.sector_dispersion_ic  # then this, offline
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .feature_research import spearman_ic
from .fetch_sector_bars import DEFAULT_CACHE, MARKET, SECTORS, UNIVERSE
from .polygon_bars import PolygonClient, RateLimiter, chunk_ranges, load_api_key

logger = logging.getLogger(__name__)

ET = "America/New_York"

# Signal is struck at 09:20; entry is 09:33 (the three-minute confirmation slot).
# Nothing between those two timestamps is allowed to inform the signal.
SIGNAL_TIME = (9, 20)
ENTRY_TIME = (9, 33)
HORIZONS: dict[str, tuple[int, int]] = {"1030": (10, 30), "1550": (15, 50)}

BETA_WINDOW = 60       # trading days for rolling beta
VOL_WINDOW = 20        # trading days for the overnight-vol standardiser
BETA_SHRINK = 0.7      # 0.7*beta_hat + 0.3*1.0
MIN_SECTORS_PER_DAY = 8
BASKET_N = 3           # top-3 / bottom-3

# One-way transaction cost per leg, in basis points of notional traded.
# 4 one-way legs per round trip (2 in, 2 out) on a 1-unit-per-side book.
COST_BPS_ONE_WAY = 1.0
LEGS_PER_ROUND_TRIP = 4


# --------------------------------------------------------------------- loading
def _bars_to_frame(bars: list[dict]) -> pd.DataFrame:
    """Polygon agg rows -> tz-aware ET frame with a `session` (calendar) column."""
    if not bars:
        return pd.DataFrame(columns=["ts", "close", "session", "hm"])
    df = pd.DataFrame(bars)
    df = df.rename(columns={"t": "ms", "c": "close", "o": "open", "v": "volume"})
    df["ts"] = pd.to_datetime(df["ms"], unit="ms", utc=True).dt.tz_convert(ET)
    df["session"] = df["ts"].dt.date
    df["hm"] = df["ts"].dt.hour * 60 + df["ts"].dt.minute
    return df[["ts", "close", "volume", "session", "hm"]].sort_values("ts")


def _at_or_before(day: pd.DataFrame, hm: int, *, max_staleness: int) -> tuple[float, int]:
    """Last print at or before `hm`, plus its staleness in minutes.

    Premarket sector-ETF liquidity is thin — XLRE/XLB can go many minutes
    between prints. Returning staleness lets the caller drop observations where
    the "09:20 price" is really an 08:15 price, which would otherwise quietly
    become a stale-signal bias.
    """
    elig = day[day["hm"] <= hm]
    if elig.empty:
        return float("nan"), 10**6
    row = elig.iloc[-1]
    stale = int(hm - row["hm"])
    if stale > max_staleness:
        return float("nan"), stale
    return float(row["close"]), stale


def load_prices(cache: Path, start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the (session x ticker) price panel and a data-quality frame.

    Returns
      panel : long frame with prev_close / px_0920 / px_0933 / px_1030 / px_1550
      dq    : per-ticker premarket coverage + staleness stats
    """
    client = PolygonClient(load_api_key(), cache, RateLimiter())
    chunks = chunk_ranges(start, end, days=90)

    recs: list[dict] = []
    dq_recs: list[dict] = []
    for ticker in UNIVERSE:
        daily = _bars_to_frame(client.daily_bars(ticker, start, end))
        daily_close = daily.set_index("session")["close"]
        prev_close = daily_close.shift(1)

        # Ex-dividend sessions: Polygon adjusts splits but NOT dividends, so the
        # overnight "gap" on an ex-date is really the distribution. Dropping
        # these removes a recurring fake signal ~4x/year/ticker.
        ex_dates = {
            pd.to_datetime(d["ex_dividend_date"]).date()
            for d in client.dividends(ticker)
            if d.get("ex_dividend_date")
        }

        minute = pd.concat(
            [_bars_to_frame(client.minute_bars(ticker, a, b)) for a, b in chunks],
            ignore_index=True,
        )
        if minute.empty:
            logger.warning("%s: no minute bars in cache", ticker)
            continue

        n_stale, n_missing, staleness = 0, 0, []
        for session, day in minute.groupby("session"):
            if session in ex_dates:
                continue
            pc = prev_close.get(session, float("nan"))
            if not (pc == pc):
                continue
            px_sig, stale = _at_or_before(day, SIGNAL_TIME[0] * 60 + SIGNAL_TIME[1],
                                          max_staleness=45)
            if px_sig != px_sig:
                n_missing += 1
            else:
                staleness.append(stale)
                if stale > 10:
                    n_stale += 1
            px_entry, _ = _at_or_before(day, ENTRY_TIME[0] * 60 + ENTRY_TIME[1],
                                        max_staleness=5)
            rec = {
                "session": session, "ticker": ticker,
                "prev_close": pc, "px_0920": px_sig, "px_0933": px_entry,
                "stale_min": stale,
            }
            for name, (h, m) in HORIZONS.items():
                rec[f"px_{name}"], _ = _at_or_before(day, h * 60 + m, max_staleness=5)
            recs.append(rec)

        total = n_missing + len(staleness)
        dq_recs.append({
            "ticker": ticker,
            "sessions": total,
            "premarket_coverage": (len(staleness) / total) if total else 0.0,
            "median_staleness_min": float(np.median(staleness)) if staleness else float("nan"),
            "pct_stale_gt10min": (n_stale / len(staleness)) if staleness else float("nan"),
        })

    return pd.DataFrame.from_records(recs), pd.DataFrame.from_records(dq_recs)


# ------------------------------------------------------------------- mechanics
def build_returns(panel: pd.DataFrame) -> pd.DataFrame:
    """Log returns: overnight (prev close -> 09:20) and forward (09:33 -> H)."""
    df = panel.copy()
    df["r_on"] = np.log(df["px_0920"] / df["prev_close"])
    for name in HORIZONS:
        df[f"r_fwd_{name}"] = np.log(df[f"px_{name}"] / df["px_0933"])
    return df


def _rolling_beta(sector: pd.Series, market: pd.Series, window: int) -> pd.Series:
    """Rolling OLS beta, strictly causal (shifted so day t uses data < t)."""
    cov = sector.rolling(window, min_periods=window // 2).cov(market)
    var = market.rolling(window, min_periods=window // 2).var()
    beta = (cov / var).replace([np.inf, -np.inf], np.nan)
    return (BETA_SHRINK * beta + (1 - BETA_SHRINK) * 1.0).shift(1)


def residualise(df: pd.DataFrame) -> pd.DataFrame:
    """Add betas and market-residual returns.

    Overnight and intraday betas are estimated separately — a sector's
    sensitivity to the market across the gap is not the same as its sensitivity
    during the session, and using one for the other leaks market direction into
    what is supposed to be the residual.
    """
    merged = df.copy()
    for name in ["r_on"] + [f"r_fwd_{h}" for h in HORIZONS]:
        w = df.pivot(index="session", columns="ticker", values=name)
        if MARKET not in w.columns:
            raise RuntimeError(f"{MARKET} missing from panel — cannot residualise")
        mkt = w[MARKET]
        betas, resids = {}, {}
        for tk in SECTORS:
            if tk not in w.columns:
                continue
            beta = _rolling_beta(w[tk], mkt, BETA_WINDOW)
            betas[tk], resids[tk] = beta, w[tk] - beta * mkt

        def _long(d: dict, col: str) -> pd.DataFrame:
            return (pd.DataFrame(d).reset_index()
                    .melt(id_vars="session", var_name="ticker", value_name=col))

        both = _long(betas, f"beta_{name}").merge(
            _long(resids, f"resid_{name}"), on=["session", "ticker"], how="outer")
        merged = merged.merge(both, on=["session", "ticker"], how="left")
    return merged


def build_signal(df: pd.DataFrame) -> pd.DataFrame:
    """z = residual overnight return / its own trailing 20d stdev.

    Standardising per sector is what makes a 1% SMH gap comparable to a 1% XLU
    gap. The trailing window is shifted so day t never sees its own value.
    """
    df = df.sort_values(["ticker", "session"]).copy()
    g = df.groupby("ticker")["resid_r_on"]
    trailing_sd = g.transform(
        lambda s: s.shift(1).rolling(VOL_WINDOW, min_periods=VOL_WINDOW // 2).std()
    )
    df["signal_z"] = df["resid_r_on"] / trailing_sd
    df["trailing_sd"] = trailing_sd
    return df


# --------------------------------------------------------------------- results
@dataclass
class HorizonResult:
    horizon: str
    n_days: int
    mean_ic: float
    ic_std: float
    ic_tstat: float
    ic_hit_rate: float
    spread_bps_gross: float
    spread_bps_net: float
    spread_tstat: float
    spread_hit_rate: float
    sharpe_gross: float
    sharpe_net: float
    mean_net_beta: float
    raw_spread_bps: float
    high_disp_mean_ic: float
    high_disp_n_days: int


@dataclass
class Report:
    window: str
    n_sessions: int
    n_sector_obs: int
    data_quality: list[dict]
    horizons: list[dict]
    warnings: list[str] = field(default_factory=list)


def _tstat(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 3 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))


def analyse_horizon(df: pd.DataFrame, horizon: str) -> HorizonResult:
    fwd = f"resid_r_fwd_{horizon}"
    raw = f"r_fwd_{horizon}"
    beta_col = f"beta_r_fwd_{horizon}"

    ics, spreads, raw_spreads, net_betas, disp = [], [], [], [], []
    for session, day in df.groupby("session"):
        d = day.dropna(subset=["signal_z", fwd])
        if len(d) < MIN_SECTORS_PER_DAY:
            continue
        ics.append(spearman_ic(d["signal_z"], d[fwd]))
        disp.append(float(d["resid_r_on"].std(ddof=1)))

        ranked = d.sort_values("signal_z", ascending=False)
        top, bot = ranked.head(BASKET_N), ranked.tail(BASKET_N)
        spreads.append(float(top[fwd].mean() - bot[fwd].mean()))
        raw_spreads.append(float(top[raw].mean() - bot[raw].mean()))
        net_betas.append(float(top[beta_col].mean() - bot[beta_col].mean()))

    ic = pd.Series(ics, dtype="float64").dropna()
    sp = pd.Series(spreads, dtype="float64").dropna()
    cost = COST_BPS_ONE_WAY * LEGS_PER_ROUND_TRIP / 1e4
    sp_net = sp - cost

    # Does the dispersion gate help? Compare IC on the top-40% dispersion days.
    dser = pd.Series(disp, dtype="float64")
    hi = dser >= dser.quantile(0.6) if len(dser) == len(ics) else pd.Series(dtype=bool)
    hi_ic = pd.Series(ics, dtype="float64")[hi.values].dropna() if len(hi) else pd.Series(dtype="float64")

    ann = math.sqrt(252)
    return HorizonResult(
        horizon=horizon,
        n_days=int(len(ic)),
        mean_ic=float(ic.mean()) if len(ic) else float("nan"),
        ic_std=float(ic.std(ddof=1)) if len(ic) > 1 else float("nan"),
        ic_tstat=_tstat(ic),
        ic_hit_rate=float((ic > 0).mean()) if len(ic) else float("nan"),
        spread_bps_gross=float(sp.mean() * 1e4) if len(sp) else float("nan"),
        spread_bps_net=float(sp_net.mean() * 1e4) if len(sp) else float("nan"),
        spread_tstat=_tstat(sp),
        spread_hit_rate=float((sp > 0).mean()) if len(sp) else float("nan"),
        sharpe_gross=float(sp.mean() / sp.std(ddof=1) * ann) if len(sp) > 1 and sp.std(ddof=1) else float("nan"),
        sharpe_net=float(sp_net.mean() / sp_net.std(ddof=1) * ann) if len(sp) > 1 and sp_net.std(ddof=1) else float("nan"),
        mean_net_beta=float(np.nanmean(net_betas)) if net_betas else float("nan"),
        raw_spread_bps=float(np.nanmean(raw_spreads) * 1e4) if raw_spreads else float("nan"),
        high_disp_mean_ic=float(hi_ic.mean()) if len(hi_ic) else float("nan"),
        high_disp_n_days=int(len(hi_ic)),
    )


def run_study(cache: Path, start: date, end: date) -> tuple[Report, pd.DataFrame]:
    panel, dq = load_prices(cache, start, end)
    if panel.empty:
        return Report(f"{start}..{end}", 0, 0, [], [],
                      ["no cached bars — run app.research.fetch_sector_bars first"]), panel

    df = build_signal(residualise(build_returns(panel)))
    results = [analyse_horizon(df, h) for h in HORIZONS]

    warnings: list[str] = []
    worst = dq[dq["ticker"].isin(SECTORS)]["premarket_coverage"].min() if not dq.empty else 1.0
    if worst < 0.9:
        thin = dq[(dq["premarket_coverage"] < 0.9) & (dq["ticker"].isin(SECTORS))]
        warnings.append(
            "THIN PREMARKET DATA: " + ", ".join(
                f"{r.ticker} {r.premarket_coverage:.0%}" for r in thin.itertuples()
            ) + " of sessions have a usable 09:20 print. Sectors that cannot be "
            "ranked premarket cannot be traded on this signal."
        )
    n_days = max((r.n_days for r in results), default=0)
    if n_days < 250:
        warnings.append(
            f"SHORT SAMPLE: {n_days} usable sessions. An IC t-stat needs ~2 years "
            "of daily cross-sections before it separates skill from luck."
        )
    warnings.append(
        "IN-SAMPLE, UNCONDITIONAL: no confirmation filter, no event-day "
        "exclusions, no out-of-sample split. This measures whether raw signal "
        "content exists — it is a floor on quality, not a backtest."
    )

    return Report(
        window=f"{start}..{end}",
        n_sessions=int(df["session"].nunique()),
        n_sector_obs=int(df[df["ticker"].isin(SECTORS)]["signal_z"].notna().sum()),
        data_quality=dq.to_dict("records"),
        horizons=[asdict(r) for r in results],
        warnings=warnings,
    ), df


# ---------------------------------------------------------------------- output
def _f(x: float, nd: int = 3) -> str:
    return "   n/a" if x is None or x != x else f"{x:+.{nd}f}"


def print_report(rep: Report) -> None:
    line = "=" * 78
    print(line)
    print("SECTOR DISPERSION IC  —  does the 09:20 ranking predict the session?")
    print(line)
    print(f"window             : {rep.window}")
    print(f"sessions           : {rep.n_sessions}")
    print(f"sector observations: {rep.n_sector_obs}")
    print()

    if rep.data_quality:
        print("DATA QUALITY (premarket 09:20 print availability)")
        print(f"  {'ticker':7s} {'sessions':>9s} {'coverage':>9s} {'med stale':>10s} {'>10min':>8s}")
        for r in rep.data_quality:
            print(f"  {r['ticker']:7s} {r['sessions']:>9d} {r['premarket_coverage']:>8.0%} "
                  f"{r['median_staleness_min']:>9.0f}m {r['pct_stale_gt10min']:>7.0%}")
        print()

    print("RANK IC  (Spearman signal vs forward residual return, per-day cross-section)")
    print(f"  {'horizon':10s} {'days':>5s} {'mean IC':>9s} {'IC sd':>7s} {'t-stat':>8s} {'IC>0':>6s} {'hi-disp IC':>11s}")
    for h in rep.horizons:
        print(f"  09:33->{h['horizon']:4s} {h['n_days']:>5d} {_f(h['mean_ic']):>9s} "
              f"{h['ic_std']:>7.3f} {_f(h['ic_tstat'],2):>8s} {h['ic_hit_rate']:>5.0%} "
              f"{_f(h['high_disp_mean_ic']):>11s}")
    print()

    print("TRADEABLE  (top-3 minus bottom-3, residualised; cost = 4 legs x 1.0bp)")
    print(f"  {'horizon':10s} {'gross bp':>9s} {'net bp':>8s} {'t-stat':>8s} {'win%':>6s} "
          f"{'SR gross':>9s} {'SR net':>8s}")
    for h in rep.horizons:
        print(f"  09:33->{h['horizon']:4s} {_f(h['spread_bps_gross'],2):>9s} "
              f"{_f(h['spread_bps_net'],2):>8s} {_f(h['spread_tstat'],2):>8s} "
              f"{h['spread_hit_rate']:>5.0%} {_f(h['sharpe_gross'],2):>9s} {_f(h['sharpe_net'],2):>8s}")
    print()

    print("BETA DRIFT  (naive 50/50 dollar-neutral top-3/bottom-3)")
    for h in rep.horizons:
        print(f"  09:33->{h['horizon']:4s}  mean net beta {_f(h['mean_net_beta'],2)}  "
              f"| raw (un-residualised) spread {_f(h['raw_spread_bps'],2)} bp")
    print()

    if rep.warnings:
        print("WARNINGS")
        for w in rep.warnings:
            print(f"  ! {w}")
        print()

    print("READING IT: a mean IC of ~0.03+ with |t| > 2 is a real (if small) signal.")
    print("|t| < 2 means the daily ICs are indistinguishable from noise. The net")
    print("Sharpe column is the one that decides — IC can be real and still not")
    print("survive costs. PROMOTION GATE: research only; no capital moves on this.")
    print(line)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:  # pragma: no cover - environment dependent
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--end", default="2026-08-11")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dump", default=None, help="write the per-day panel to CSV")
    args = ap.parse_args(argv)

    end = date.fromisoformat(args.end)
    start = end - timedelta(days=int(365 * args.years))
    rep, df = run_study(Path(args.cache), start, end)

    if args.dump and not df.empty:
        df.to_csv(args.dump, index=False)
    if args.json:
        print(json.dumps(asdict(rep), default=str, indent=2))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
