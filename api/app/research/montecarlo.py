"""Monte-Carlo engine — outcome distribution, position sizing, exit stress.

Calibrates drift + volatility from a symbol's recent daily returns, simulates
many forward price paths, and reports the *distribution* of outcomes rather than
a point estimate:

  * terminal-return percentiles, probability of loss, CVaR (expected shortfall)
  * max-drawdown distribution across paths
  * suggested position size — fractional-Kelly and volatility-target, bounded
  * exit stress — probability a given stop/drawdown is breached within horizon

Sizing output is DECISION-SUPPORT: a suggested %-of-NAV with its rationale,
never an order. The simulation core (``simulate_gbm``, ``terminal_stats``,
``kelly_fraction``, ``vol_target_weight``, ``prob_breach_drawdown``) is pure and
seeded, so it is fully deterministic and unit-tested.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Iterable, Optional

import numpy as np

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
DEFAULT_PATHS = 10_000
DEFAULT_HORIZON = 20
# Bounded sizing — research suggestion, never a mandate. Caps mirror the
# conservative real-money posture (no single-name concentration blowups).
KELLY_FRACTION = 0.25          # quarter-Kelly
MAX_WEIGHT = 0.10              # never suggest >10% of NAV to one name
TARGET_ANNUAL_VOL = 0.12      # vol-target sleeve: 12% annualised


# ---------------------------------------------------------------------------
# Pure simulation core (unit-tested, seeded)
# ---------------------------------------------------------------------------


def calibrate(closes: list[float]) -> tuple[float, float]:
    """(mu_daily, sigma_daily) from a close series via log returns.

    Returns (0.0, 0.0) when there are too few points to estimate.
    """
    if len(closes) < 5:
        return 0.0, 0.0
    arr = np.asarray(closes, dtype="float64")
    arr = arr[arr > 0]
    if len(arr) < 5:
        return 0.0, 0.0
    rets = np.diff(np.log(arr))
    return float(rets.mean()), float(rets.std(ddof=1))


def simulate_gbm(s0: float, mu_daily: float, sigma_daily: float, days: int,
                 n_paths: int, seed: int = 7) -> np.ndarray:
    """Geometric-Brownian-motion paths. Shape (n_paths, days+1), column 0 = s0.

    Uses the exact GBM step with the -0.5*sigma^2 Itô correction so the drift is
    unbiased. Deterministic for a given seed.
    """
    rng = np.random.default_rng(seed)
    shocks = rng.standard_normal(size=(n_paths, days))
    step = (mu_daily - 0.5 * sigma_daily ** 2) + sigma_daily * shocks
    logpaths = np.cumsum(step, axis=1)
    paths = s0 * np.exp(logpaths)
    return np.column_stack([np.full(n_paths, s0), paths])


def terminal_stats(paths: np.ndarray) -> dict[str, float]:
    """Distribution of terminal return = path[:, -1] / path[:, 0] - 1."""
    term_ret = paths[:, -1] / paths[:, 0] - 1.0
    p05 = float(np.percentile(term_ret, 5))
    return {
        "mean_return": round(float(term_ret.mean()), 5),
        "median_return": round(float(np.median(term_ret)), 5),
        "p05": round(p05, 5),
        "p25": round(float(np.percentile(term_ret, 25)), 5),
        "p75": round(float(np.percentile(term_ret, 75)), 5),
        "p95": round(float(np.percentile(term_ret, 95)), 5),
        "prob_loss": round(float((term_ret < 0).mean()), 4),
        "cvar05": round(float(term_ret[term_ret <= p05].mean()) if (term_ret <= p05).any() else p05, 5),
    }


def path_max_drawdowns(paths: np.ndarray) -> np.ndarray:
    """Per-path worst peak-to-trough drawdown (<= 0)."""
    running_peak = np.maximum.accumulate(paths, axis=1)
    dd = paths / running_peak - 1.0
    return dd.min(axis=1)


def prob_breach_drawdown(paths: np.ndarray, threshold: float) -> float:
    """Fraction of paths whose max drawdown breaches ``threshold`` (e.g. -0.15)."""
    return round(float((path_max_drawdowns(paths) <= threshold).mean()), 4)


def kelly_fraction(mu_daily: float, sigma_daily: float, horizon_days: int) -> float:
    """Kelly weight f* = mu/sigma^2 over the horizon (clipped to [0, 1])."""
    if sigma_daily <= 0:
        return 0.0
    mu_h = mu_daily * horizon_days
    var_h = (sigma_daily ** 2) * horizon_days
    return float(np.clip(mu_h / var_h, 0.0, 1.0))


def vol_target_weight(sigma_daily: float, target_annual_vol: float = TARGET_ANNUAL_VOL) -> float:
    """Weight so the position's annualised vol equals the target (clipped)."""
    ann_vol = sigma_daily * np.sqrt(TRADING_DAYS)
    if ann_vol <= 0:
        return 0.0
    return float(np.clip(target_annual_vol / ann_vol, 0.0, 1.0))


def suggested_weight(mu_daily: float, sigma_daily: float, horizon_days: int) -> dict[str, float]:
    """Blend quarter-Kelly with the vol-target sleeve, capped at MAX_WEIGHT."""
    kelly = KELLY_FRACTION * kelly_fraction(mu_daily, sigma_daily, horizon_days)
    vt = vol_target_weight(sigma_daily)
    blended = min(kelly, vt)          # the more conservative of the two
    return {
        "kelly_quarter": round(min(kelly, MAX_WEIGHT), 4),
        "vol_target": round(min(vt, MAX_WEIGHT), 4),
        "suggested": round(min(blended, MAX_WEIGHT), 4),
    }


# ---------------------------------------------------------------------------
# Orchestration (network: fetch the symbol's series)
# ---------------------------------------------------------------------------


@dataclass
class MonteCarloReport:
    symbol: str
    horizon_days: int
    n_paths: int
    spot: Optional[float]
    mu_daily: float
    sigma_daily: float
    annualised_vol: float
    terminal: dict
    max_drawdown: dict
    sizing: dict
    exit_stress: dict
    warnings: list[str] = field(default_factory=list)


async def run_montecarlo(symbol: str, horizon_days: int = DEFAULT_HORIZON,
                         n_paths: int = DEFAULT_PATHS, seed: int = 7,
                         stop_drawdowns: tuple[float, ...] = (-0.10, -0.15, -0.20)
                         ) -> MonteCarloReport:
    from .labeler import _price_series
    symbol = symbol.strip().upper()
    series = await _price_series(symbol, lookback_days=180)
    warnings: list[str] = []
    closes = [px for _, px in sorted(series, key=lambda x: x[0])]
    if len(closes) < 30:
        warnings.append(
            f"THIN CALIBRATION: {len(closes)} daily closes for {symbol} "
            f"(want >=30). Drift/vol estimates are noisy; treat the distribution "
            f"as indicative."
        )

    mu, sigma = calibrate(closes)
    spot = closes[-1] if closes else None

    if spot is None or sigma <= 0:
        return MonteCarloReport(
            symbol, horizon_days, n_paths, spot, mu, sigma, 0.0,
            {}, {}, {}, {}, warnings + ["insufficient price data to simulate"],
        )

    paths = simulate_gbm(spot, mu, sigma, horizon_days, n_paths, seed)
    term = terminal_stats(paths)
    dd = path_max_drawdowns(paths)
    sizing = suggested_weight(mu, sigma, horizon_days)
    exit_stress = {f"{int(t*100)}pct": prob_breach_drawdown(paths, t) for t in stop_drawdowns}

    return MonteCarloReport(
        symbol=symbol, horizon_days=horizon_days, n_paths=n_paths, spot=round(spot, 2),
        mu_daily=round(mu, 6), sigma_daily=round(sigma, 6),
        annualised_vol=round(sigma * float(np.sqrt(TRADING_DAYS)), 4),
        terminal=term,
        max_drawdown={
            "mean": round(float(dd.mean()), 5),
            "p05": round(float(np.percentile(dd, 5)), 5),
            "median": round(float(np.median(dd)), 5),
        },
        sizing=sizing, exit_stress=exit_stress, warnings=warnings,
    )


def print_report(rep: MonteCarloReport) -> None:
    line = "=" * 70
    print(line)
    print(f"MONTE CARLO  —  {rep.symbol}   ({rep.n_paths:,} paths, {rep.horizon_days}d)")
    print(line)
    if rep.warnings:
        for w in rep.warnings:
            print(f"  ! {w}")
        print()
    print(f"spot={rep.spot}  mu/day={rep.mu_daily:+.5f}  vol/day={rep.sigma_daily:.5f}  "
          f"ann.vol={rep.annualised_vol:.1%}")
    if rep.terminal:
        t = rep.terminal
        print(f"terminal return : mean={t['mean_return']:+.2%}  median={t['median_return']:+.2%}  "
              f"p05={t['p05']:+.2%}  p95={t['p95']:+.2%}")
        print(f"                  prob_loss={t['prob_loss']:.1%}  CVaR05={t['cvar05']:+.2%}")
    if rep.sizing:
        s = rep.sizing
        print(f"sizing (%NAV)   : suggested={s['suggested']:.1%}  "
              f"(¼Kelly={s['kelly_quarter']:.1%}, vol-target={s['vol_target']:.1%})")
    if rep.exit_stress:
        print(f"exit stress     : " + "  ".join(f"P(dd≤{k})={v:.1%}" for k, v in rep.exit_stress.items()))
    print()
    print("DECISION-SUPPORT: suggested size is a research output, never an order.")
    print(line)


def main(argv: Optional[Iterable[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    try:  # pragma: no cover
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol")
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    ap.add_argument("--paths", type=int, default=DEFAULT_PATHS)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    rep = asyncio.run(run_montecarlo(args.symbol, args.horizon, args.paths, args.seed))
    if args.json:
        print(json.dumps(asdict(rep), default=str, indent=2))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
