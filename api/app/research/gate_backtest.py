"""Point-in-time backtest of the portfolio gates, across eras and complexes.

The problem this solves is sample size. The AI-infrastructure index carries
about three years of weekly bars, which contains perhaps four dislocations and
— decisively — not one theme break. Thresholds cannot be judged on that, and
the theme-break path cannot be judged on it at all, because the event has never
happened inside the window.

The way out is that none of the gates is AI-specific. ``weekly_regime_score``,
``exhaustion_score``, ``selling_exhaustion_score``, the accumulation and trim
gates and the exposure state machine all take generic measurements: trend,
relative strength, breadth, volatility-scaled extension, confluence. They
describe how a high-beta thematic complex behaves, not how semiconductors
behave. So the rules can be replayed against OTHER complexes with decades of
history — including several whose thesis genuinely broke — and the question
becomes the one worth answering: does this machinery work on a growth complex
in general, of which the current book is one instance?

Eight baskets, chosen so the sample contains real busts rather than only
survivors' good years:

    ai_infra        the live book — the thing being decided about
    dotcom_infra    1995-2005 networking/semis capex boom and collapse. The
                    closest structural analog there is: a capex supercycle
                    sold as permanent, which broke.
    semis_cycle     2003- repeated 30-50% cyclical drawdowns inside an intact
                    secular uptrend — exactly the case the gates must NOT
                    mistake for a theme break.
    energy_capex    2004- a genuine theme break in 2014 that never recovered
                    its leadership.
    cloud_saas      2013- a long orderly advance; tests false-positive rate.
    hypergrowth     2019- the 2021-22 collapse, the fastest high-beta break
                    in the sample.
    china_tech      2015- a regulatory break: fundamentals intact, thesis dead.
    biotech         2013- a complex that tops and bases on its own clock.

Two properties make the result mean something:

    THE HARNESS CALLS PRODUCTION CODE. Each week is evaluated by
    ``portfolio.daily.evaluate_index`` — the same function the 16:20 job calls.
    A harness that scores a parallel copy of the rules measures the copy, and
    every threshold it blesses is blessed for code that is not running.

    NO LOOKAHEAD IS POSSIBLE BY CONSTRUCTION. Replay truncates the IndexState
    to bars up to week t and hands that over. Every gate reads only from the
    object's own series, so there is nothing later in it to peek at — the
    guarantee is structural rather than a rule someone has to keep obeying.

Known limits, stated because a backtest that hides them is worse than none:

    SURVIVORSHIP. Constituents are today's known names, so each basket omits
    members that delisted. This flatters the hold-through-it case. It is
    mitigated, not removed, by including baskets that broke while their
    survivors kept trading.

    NO CAP-WEIGHTED TWIN. Historical market caps are not available, and
    applying today's caps to 2005 would be lookahead. The ``narrowing``
    condition therefore never fires in replay, so exhaustion is scored out of
    four conditions rather than five and the trim gate is tested slightly
    harder than it runs live.

    EXPOSURE, NOT OPTIONS. The equity curve models exposure against the index,
    while the live book holds leveraged LEAPS. Both drawdowns and gains are
    understated; the comparison between gated and ungated is the meaningful
    output, not the absolute return.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median
from typing import Any, Optional

logger = logging.getLogger("agentic_edge.backtest")

BACKTEST_ACTION_TYPE = "gate_backtest"

# Weekly bars required before the first evaluation. A 50-week average plus a
# 14-period ATR plus room for pivots — below this the gates are reading noise.
MIN_WARMUP_WEEKS = 60

# Forward horizons, in weeks. Chosen to span the decision's own timeframe: a
# weekly framework that only works at one horizon has found an artefact.
HORIZONS = (4, 13, 26, 52)

# Exposure moved per week when the book is outside its band. The live rule is
# one state-step per day; against weekly bars this is the nearest honest
# equivalent, and the result is reported as a band-following curve rather than
# a trade-by-trade simulation.
EXPOSURE_STEP = 0.10


@dataclass
class Basket:
    key: str
    label: str
    symbols: list[str]
    benchmark: str = "SPY"
    note: str = ""


# Deliberately survivors plus genuine busts. A sample of complexes that all
# worked would answer a different question than the one being asked.
BASKETS: list[Basket] = [
    Basket("ai_infra", "AI infrastructure (live book)", [], benchmark="QQQ",
           note="constituents loaded from the live theme universe"),
    Basket("dotcom_infra", "Networking/semis capex 1995-2005",
           ["CSCO", "INTC", "MSFT", "ORCL", "TXN", "AMAT", "LRCX", "KLAC",
            "ADI", "MU", "GLW", "QCOM", "JNPR", "CIEN", "MSI"],
           note="the closest structural analog: a capex supercycle that broke"),
    Basket("semis_cycle", "Semiconductors 2003-",
           ["NVDA", "AMD", "MU", "AMAT", "LRCX", "KLAC", "ADI", "TXN", "INTC",
            "MCHP", "ON", "SWKS", "TER", "MPWR", "QCOM", "AVGO"],
           note="deep cyclical drawdowns inside an intact secular uptrend"),
    Basket("energy_capex", "Energy capex 2004-",
           ["XOM", "CVX", "SLB", "HAL", "OXY", "COP", "EOG", "DVN", "MRO",
            "PSX", "VLO", "BKR"],
           note="a real theme break in 2014 that never regained leadership"),
    Basket("cloud_saas", "Cloud/SaaS 2013-",
           ["CRM", "ADBE", "NOW", "WDAY", "VEEV", "INTU", "ADSK", "ORCL",
            "MSFT", "PANW"],
           note="a long orderly advance — tests the false-positive rate"),
    Basket("hypergrowth", "High-beta growth 2019-",
           ["SHOP", "SQ", "ROKU", "ZM", "DOCU", "TDOC", "TWLO", "NET", "DDOG",
            "OKTA", "PTON", "U"],
           note="the 2021-22 collapse: the fastest high-beta break in the sample"),
    Basket("china_tech", "China internet 2015-",
           ["BABA", "JD", "BIDU", "NTES", "PDD", "TCOM", "YUMC"],
           note="regulatory break: fundamentals intact, thesis dead"),
    Basket("biotech", "Large-cap biotech 2013-",
           ["REGN", "VRTX", "AMGN", "GILD", "BIIB", "ILMN", "ALNY", "INCY"],
           note="tops and bases on its own clock, uncorrelated to tech capex"),
]


# ---------------------------------------------------------------------------
# Point-in-time replay
# ---------------------------------------------------------------------------


def _iso_key(d: date) -> tuple[int, int]:
    y, w, _ = d.isocalendar()
    return (y, w)


def _breadth_series(weekly_by_symbol: dict[str, list], index_weeks: list,
                    period: int = 20) -> list[float]:
    """Fraction of constituents above their OWN n-week average, per index week.

    Aligned on ISO week rather than exact date: constituents do not all trade
    the same last session of a week, and matching on ``Bar.d`` silently drops
    any name whose week ended a day early.

    Precomputed for the whole history and then sliced, because recomputing it
    inside the walk is the difference between a replay that finishes in seconds
    and one that finishes in an hour.
    """
    from ..portfolio.basket_index import sma

    per_symbol_by_week: dict[str, dict[tuple[int, int], bool]] = {}
    for sym, wk in weekly_by_symbol.items():
        flags: dict[tuple[int, int], bool] = {}
        for i in range(period, len(wk)):
            m = sma(wk[: i + 1], period)
            if m is not None:
                flags[_iso_key(wk[i].d)] = bool(wk[i].c > m)
        per_symbol_by_week[sym] = flags

    out: list[float] = []
    for b in index_weeks:
        k = _iso_key(b.d)
        vals = [f[k] for f in per_symbol_by_week.values() if k in f]
        out.append(round(sum(1 for v in vals if v) / len(vals), 4) if vals else 0.0)
    return out


@dataclass
class WeekResult:
    week: date
    close: float
    instruction: str
    state: str
    regime: int
    exhaustion: int
    selling_exhaustion: int
    accum_action: str
    trim_action: str
    accum_blocked: list[str] = field(default_factory=list)
    confluence: int = 0
    extension_atr: Optional[float] = None
    correction_atr: Optional[float] = None
    exposure: float = 1.0
    fwd: dict[int, Optional[float]] = field(default_factory=dict)


def replay_basket(
    *, weekly: list, benchmark_weekly: list, weekly_by_symbol: dict[str, list],
    warmup: int = MIN_WARMUP_WEEKS,
) -> list[WeekResult]:
    """Walk the history week by week, evaluating production code at each step.

    The exposure carried into week t is whatever the previous weeks' decisions
    produced, so the state machine's one-step-at-a-time behaviour and its
    band-as-target-not-trigger rule are both exercised as they would be live —
    evaluating each week from a clean 100% would test a system nobody runs.
    """
    from ..portfolio.basket_index import IndexState, atr, compute_levels
    from ..portfolio.daily import evaluate_index

    if len(weekly) <= warmup + max(HORIZONS):
        return []

    breadth_all = _breadth_series(weekly_by_symbol, weekly, period=20)
    breadth_w50 = _breadth_series(weekly_by_symbol, weekly, period=50)

    bench_by_week = {_iso_key(b.d): b for b in benchmark_weekly}

    results: list[WeekResult] = []
    exposure = 1.0
    prev_state: Optional[str] = None

    for t in range(warmup, len(weekly)):
        wk = weekly[: t + 1]
        # Benchmark truncated to the same instant. Built from the index's own
        # weeks so a benchmark holiday cannot shift the two series relative to
        # each other and manufacture relative strength that never existed.
        bench = [bench_by_week[k] for k in (_iso_key(b.d) for b in wk)
                 if k in bench_by_week]

        idx = IndexState(as_of=wk[-1].d, n_constituents=len(weekly_by_symbol))
        idx.weekly = wk
        idx.weekly_atr = atr(wk, period=14)
        idx.close = wk[-1].c
        idx.levels = compute_levels(wk)
        idx.breadth_above_w20 = breadth_all[t]
        idx.breadth_above_w50 = breadth_w50[t]
        idx.breadth_history = breadth_all[max(0, t - 25): t + 1]
        idx.benchmark_weekly = bench
        # cap_weighted_weekly intentionally left empty — see module docstring.

        try:
            d = evaluate_index(idx, exposure_pct=exposure, previous_state=prev_state)
        except Exception as e:  # noqa: BLE001 — one bad week must not end the replay
            logger.debug("replay: week %s failed: %s", wk[-1].d, e)
            continue

        prev_state = d["state"]
        lo, hi = d["target_band"]
        if d["instruction"] == "add":
            exposure = min(exposure + EXPOSURE_STEP, lo)
        elif d["instruction"] == "reduce":
            exposure = max(exposure - EXPOSURE_STEP, hi)

        r = WeekResult(
            week=wk[-1].d, close=wk[-1].c,
            instruction=d["instruction"], state=d["state"],
            regime=d["regime"]["score"],
            exhaustion=d["exhaustion"]["score"],
            selling_exhaustion=d["selling_exhaustion"]["score"],
            accum_action=d["accumulation_gate"]["action"],
            trim_action=d["trim_gate"]["action"],
            accum_blocked=list(d["accumulation_gate"].get("blocked_by") or []),
            confluence=d["index"].get("confluence") or 0,
            extension_atr=d["index"].get("extension_atr"),
            correction_atr=d["index"].get("correction_atr"),
            exposure=exposure,
        )
        for h in HORIZONS:
            r.fwd[h] = ((weekly[t + h].c / wk[-1].c - 1.0)
                        if t + h < len(weekly) and wk[-1].c else None)
        results.append(r)
    return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _edge(sel: list[float], base: list[float]) -> dict[str, Any]:
    if not sel:
        return {"n": 0}
    return {
        "n": len(sel),
        "mean": round(mean(sel), 4),
        "median": round(median(sel), 4),
        "baseline_mean": round(mean(base), 4) if base else None,
        "edge": round(mean(sel) - mean(base), 4) if base else None,
        "win_rate": round(sum(1 for x in sel if x > 0) / len(sel), 3),
        "baseline_win_rate": (round(sum(1 for x in base if x > 0) / len(base), 3)
                              if base else None),
    }


def score_signal(
    results: list[WeekResult], *, predicate, horizons: tuple[int, ...] = HORIZONS,
) -> dict[str, Any]:
    """Forward returns when a signal fired, against the unconditional baseline.

    The baseline is every week in the same sample, so the comparison is against
    "hold the complex" rather than against cash. A gate on a complex that rose
    for a decade will show a positive mean return while adding nothing; only
    the difference from the baseline says whether the rule contributed.
    """
    out: dict[str, Any] = {}
    for h in horizons:
        sel = [r.fwd[h] for r in results if predicate(r) and r.fwd.get(h) is not None]
        base = [r.fwd[h] for r in results if r.fwd.get(h) is not None]
        out[f"{h}w"] = _edge(sel, base)
    return out


def simulate_curve(results: list[WeekResult]) -> dict[str, Any]:
    """Gated exposure against always-100%, on the same weeks.

    The decision-relevant comparison for a fund whose thesis is a multi-year
    supercycle is not "did the gates make money" — the complex made money — but
    whether they reduced drawdown by more than they gave up in return. A
    machine that halves the drawdown and halves the return has done nothing;
    one that costs 10% of the return to remove 30% of the drawdown has earned
    its place.
    """
    if len(results) < 2:
        return {"weeks": len(results)}

    def _curve(exposures: list[float]) -> tuple[float, float]:
        eq, peak, mdd = 1.0, 1.0, 0.0
        for i in range(1, len(results)):
            prev, cur = results[i - 1].close, results[i].close
            ret = (cur / prev - 1.0) if prev else 0.0
            eq *= (1.0 + exposures[i - 1] * ret)
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak if peak else 0.0)
        return eq, mdd

    gated_eq, gated_dd = _curve([r.exposure for r in results])
    hold_eq, hold_dd = _curve([1.0] * len(results))
    years = len(results) / 52.0

    def _cagr(eq: float) -> Optional[float]:
        return round(eq ** (1 / years) - 1.0, 4) if years > 0 and eq > 0 else None

    return {
        "weeks": len(results), "years": round(years, 1),
        "gated": {"total_return": round(gated_eq - 1, 4), "cagr": _cagr(gated_eq),
                  "max_drawdown": round(gated_dd, 4),
                  "avg_exposure": round(mean(r.exposure for r in results), 3)},
        "hold": {"total_return": round(hold_eq - 1, 4), "cagr": _cagr(hold_eq),
                 "max_drawdown": round(hold_dd, 4)},
        "return_given_up": round((hold_eq - 1) - (gated_eq - 1), 4),
        "drawdown_avoided": round(hold_dd - gated_dd, 4),
    }


def score_basket(results: list[WeekResult]) -> dict[str, Any]:
    """Every gate's edge on one basket, plus the curve comparison."""
    if not results:
        return {"weeks": 0, "note": "insufficient history"}

    blockers: dict[str, int] = {}
    for r in results:
        for b in r.accum_blocked:
            blockers[b] = blockers.get(b, 0) + 1

    return {
        "weeks": len(results),
        "from": results[0].week.isoformat(), "to": results[-1].week.isoformat(),
        "accumulate": score_signal(results, predicate=lambda r: r.accum_action == "accumulate"),
        "trim": score_signal(results, predicate=lambda r: r.trim_action == "trim"),
        "stop_adding": score_signal(results, predicate=lambda r: r.trim_action == "stop_adding"),
        "regime_ge3": score_signal(results, predicate=lambda r: r.regime >= 3),
        "selling_exhaustion_ge2": score_signal(
            results, predicate=lambda r: r.selling_exhaustion >= 2),
        "curve": simulate_curve(results),
        "state_weeks": _counts(r.state for r in results),
        "instruction_weeks": _counts(r.instruction for r in results),
        "accumulation_blockers": dict(sorted(blockers.items(), key=lambda kv: -kv[1])),
    }


def _counts(it) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in it:
        out[v] = out.get(v, 0) + 1
    return out


async def _basket_symbols(b: Basket) -> list[str]:
    """The live theme universe for ai_infra, the declared list otherwise."""
    if b.symbols:
        return b.symbols
    from sqlalchemy import select

    from ..db import ThemeSymbol, get_session as db_session
    async with db_session() as s:
        return sorted({(r[0] or "").upper()
                       for r in (await s.execute(select(ThemeSymbol.symbol))).all() if r[0]})


async def replay_one(b: Basket, *, lookback_days: int) -> tuple[list[WeekResult], dict[str, Any]]:
    """Fetch, assemble and replay one basket."""
    from ..portfolio.basket_index import _fetch_daily, build_index_series, to_weekly

    symbols = await _basket_symbols(b)
    meta: dict[str, Any] = {"key": b.key, "label": b.label, "note": b.note,
                            "benchmark": b.benchmark, "requested": len(symbols)}
    if not symbols:
        meta["skipped"] = "no symbols"
        return [], meta

    per_symbol = await _fetch_daily(symbols, lookback_days)
    meta["with_bars"] = len(per_symbol)
    if len(per_symbol) < 5:
        meta["skipped"] = f"only {len(per_symbol)} constituents returned bars"
        return [], meta

    daily = build_index_series(per_symbol)
    weekly = to_weekly(daily)
    meta["weekly_bars"] = len(weekly)

    bench = await _fetch_daily([b.benchmark], lookback_days)
    bench_weekly = to_weekly(bench[b.benchmark]) if b.benchmark in bench else []
    if not bench_weekly:
        meta["warning"] = f"{b.benchmark} unavailable — relative strength blind"

    weekly_by_symbol = {s: to_weekly(bars) for s, bars in per_symbol.items()}
    results = replay_basket(weekly=weekly, benchmark_weekly=bench_weekly,
                            weekly_by_symbol=weekly_by_symbol)
    meta["evaluated_weeks"] = len(results)
    return results, meta


async def run_gate_backtest(
    *, keys: Optional[list[str]] = None, lookback_days: int = 9200,
    persist: bool = True,
) -> dict[str, Any]:
    """Replay every basket, score every gate, persist the report.

    ``lookback_days`` defaults to ~25 years so the sample reaches back through
    the dot-com break. Baskets whose constituents did not exist then simply
    start later — chained index construction lets a name join when it begins
    trading rather than truncating the series to the shortest history.
    """
    # Force settings to load before any provider is constructed. Providers read
    # their keys from the process environment at construction, and .env is only
    # applied by get_settings(); a stale OS-level key otherwise wins silently
    # and every request fails auth against a key nobody has configured. That
    # cost an hour of misdiagnosis as a revoked production key.
    from ..config import get_settings
    get_settings()

    chosen = [b for b in BASKETS if keys is None or b.key in keys]
    all_results: dict[str, list[WeekResult]] = {}
    per_basket: dict[str, Any] = {}

    for b in chosen:
        try:
            results, meta = await replay_one(b, lookback_days=lookback_days)
        except Exception as e:  # noqa: BLE001 — one basket must not kill the run
            logger.warning("backtest: basket %s failed: %s", b.key, e)
            per_basket[b.key] = {"error": str(e)}
            continue
        if results:
            all_results[b.key] = results
        per_basket[b.key] = {**meta, **score_basket(results)}
        logger.info("backtest %s: %d weeks evaluated", b.key, len(results))

    pooled = {
        "accumulate": pool_signal(
            all_results, predicate=lambda r: r.accum_action == "accumulate"),
        "trim": pool_signal(all_results, predicate=lambda r: r.trim_action == "trim"),
        "stop_adding": pool_signal(
            all_results, predicate=lambda r: r.trim_action == "stop_adding"),
        "regime_ge3": pool_signal(all_results, predicate=lambda r: r.regime >= 3),
        "selling_exhaustion_ge2": pool_signal(
            all_results, predicate=lambda r: r.selling_exhaustion >= 2),
    }

    report = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "baskets": per_basket,
        "pooled": pooled,
        "totals": {
            "baskets_replayed": len(all_results),
            "weeks_evaluated": sum(len(v) for v in all_results.values()),
        },
        "limits": ["survivorship: constituents are today's known names",
                   "no cap-weighted twin: exhaustion scored out of 4, not 5",
                   "exposure model, not option deltas"],
    }

    if persist:
        try:
            from ..autotrade.auto_gate import AutoGateResult, record_auto_action
            from ..db import get_session as db_session
            async with db_session() as s:
                await record_auto_action(
                    s, loop="research", action_type=BACKTEST_ACTION_TYPE,
                    gate_result=AutoGateResult(passed=True, failures=[]),
                    payload=report, outcome=f"{report['totals']['weeks_evaluated']} weeks")
        except Exception as e:  # noqa: BLE001
            logger.warning("backtest: could not persist report: %s", e)

    return report


def sweep_confluence(
    all_results: dict[str, list[WeekResult]],
    thresholds: tuple[int, ...] = (1, 2, 3, 4, 5), horizon: int = 13,
) -> dict[str, Any]:
    """What the accumulation gate would have done at each confluence threshold.

    Reconstructed from ``accum_blocked`` rather than by re-running: a week
    satisfied every non-confluence condition exactly when no other blocker was
    recorded, so the confluence requirement can be varied after the fact
    without another pass over twenty years of data.

    The purpose is not to find the threshold with the best number. It is to see
    whether the gate has any edge at ALL once it is loose enough to fire — a
    rule that only works at a setting it never reaches has not been shown to
    work, and a rule whose edge vanishes as soon as it fires never had one.
    """
    out: dict[str, Any] = {}
    for c in thresholds:
        def pred(r: WeekResult, c: int = c) -> bool:
            others = [b for b in r.accum_blocked if not b.startswith("confluence")]
            return not others and r.confluence >= c
        out[f"confluence>={c}"] = pool_signal(all_results, predicate=pred,
                                              horizon=horizon)
    return out


def sweep_trim_location(
    all_results: dict[str, list[WeekResult]],
    thresholds: tuple[int, ...] = (2, 3, 4, 5), horizon: int = 13,
) -> dict[str, Any]:
    """The trim gate's location condition at each confluence threshold.

    Exhaustion and persistence are read from the recorded week rather than
    reconstructed, so this varies only the half that was found to be binding.
    A trim signal EARNS its place by being followed by returns BELOW the
    baseline — if forward returns after a trim match the baseline, the gate is
    selling for no reason.
    """
    out: dict[str, Any] = {}
    for c in thresholds:
        def pred(r: WeekResult, c: int = c) -> bool:
            location = r.confluence >= c or (r.extension_atr or 0) > 2.5
            return location and r.exhaustion >= 3
        out[f"confluence>={c}"] = pool_signal(all_results, predicate=pred,
                                              horizon=horizon)
    return out


def pool_signal(all_results: dict[str, list[WeekResult]], *, predicate,
                horizon: int = 13) -> dict[str, Any]:
    """One signal pooled across every basket.

    Pooling is the point of the exercise: a rule that helps on one complex has
    been fitted to it, while a rule that helps across eight independent
    complexes spanning three decades is describing something about how growth
    complexes behave. Per-basket numbers are reported too, so a result driven
    entirely by one era is visible rather than averaged away.
    """
    sel: list[float] = []
    base: list[float] = []
    per: dict[str, int] = {}
    for key, rs in all_results.items():
        s = [r.fwd[horizon] for r in rs if predicate(r) and r.fwd.get(horizon) is not None]
        sel.extend(s)
        base.extend(r.fwd[horizon] for r in rs if r.fwd.get(horizon) is not None)
        per[key] = len(s)
    out = _edge(sel, base)
    out["per_basket_fires"] = per
    out["horizon_weeks"] = horizon
    return out
