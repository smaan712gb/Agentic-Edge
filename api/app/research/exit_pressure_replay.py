"""Exit-Pressure Replay Harness — autoresearch-style offline tuner.

WHAT THIS IS
------------
Every maintenance tick, the live system computes an Exit Pressure Score for
each open position and writes the full breakdown to ``auto_actions``
(action_type ``position_pressure_hold`` and the trim/close variants). The
score is a weighted sum of five sub-scores:

    theme_deterioration 0.30 · profit_preservation 0.20 · tech_exhaustion 0.20
    · options_risk 0.15 · rotation_pressure 0.15            (production weights)

Those weights and the band thresholds (hold <40, trim_light <60, trim_heavy
<75, aggressive ≥75) are *hand-set*. Nobody has ever checked whether they
actually separate positions that went on to lose money from positions that
went on to make money — in *this* book.

This harness closes that loop. It is the trading analog of Karpathy's
``autoresearch``:

    autoresearch                     this harness
    ------------------------------   --------------------------------------
    train.py (the one mutable file)  the 5 weights + 3 band thresholds
    5-minute training run            replay over auto_actions + positions
    val_bpb (fixed metric)           forward-outcome separation (below)
    keep-if-better, discard-else     report best vs incumbent; HUMAN promotes

It is strictly offline and read-only. It recommends; it never writes config
and never trades.

THE METRIC (fixed, deterministic — the discipline that keeps it honest)
-----------------------------------------------------------------------
For each logged pressure observation at time T on symbol S we measure the
*forward* behaviour of S from the ``positions`` price snapshots:

  * fwd_ret_H    — return over the next H calendar days
  * fwd_maxdd_H  — worst peak-to-trough drawdown over the next H days

A *good* exit-pressure score is one where a HIGH score predicts a BAD forward
outcome (deep drawdown / negative return) and a LOW score predicts a good
one. We quantify that two ways:

  1. rank_ic   — Spearman rank corr between score and fwd_maxdd_H, sign-
                 flipped so "higher is better" (high pressure → deep dd → +).
  2. band_edge — mean fwd_ret_H of positions the config says HOLD minus the
                 mean fwd_ret_H of positions it says EXIT (heavy/aggressive).
                 Positive = the config would have held the winners and shed
                 the losers.

Fitness = rank_ic + band_edge (both unit-light, comparable scale). The
optimiser maximises fitness on a TRAIN window and we report the lift on a
held-out VALIDATION window so we don't reward overfitting.

DATA CAVEAT (read before trusting any number this prints)
---------------------------------------------------------
Pressure logging in the current DB spans only a ~2-day window (~1.3k rows /
24 symbols), so the *effective* independent sample is small and confidence
intervals are wide. The harness is correct and the framework is the point:
it grows sharper automatically as the live loop accumulates more pressure
logs. Treat early output as a smoke test of the machinery, not a mandate to
re-weight the live book.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# The five sub-scores, in canonical order. Must match the keys written by
# tradingagents.strategies.maintenance.exit_pressure.compute_exit_pressure.
SUBSCORE_KEYS: tuple[str, ...] = (
    "theme_deterioration",
    "profit_preservation",
    "tech_exhaustion",
    "options_risk",
    "rotation_pressure",
)

# Production incumbent — pulled from the live exit_pressure module if it is
# importable, else the documented constants. Keeping this in sync with prod
# is what makes "best vs incumbent" an honest comparison.
def _load_incumbent() -> "PressureConfig":
    try:
        from tradingagents.strategies.maintenance import exit_pressure as ep  # type: ignore
        return PressureConfig(
            weights={
                "theme_deterioration": ep.WEIGHT_THEME_DETERIORATION,
                "profit_preservation": ep.WEIGHT_PROFIT_PRESERVATION,
                "tech_exhaustion":     ep.WEIGHT_TECH_EXHAUSTION,
                "options_risk":        ep.WEIGHT_OPTIONS_RISK,
                "rotation_pressure":   ep.WEIGHT_ROTATION_PRESSURE,
            },
            band_hold_max=ep.BAND_HOLD_MAX,
            band_light_max=ep.BAND_TRIM_LIGHT_MAX,
            band_heavy_max=ep.BAND_TRIM_HEAVY_MAX,
            name="incumbent(prod)",
        )
    except Exception:  # pragma: no cover - fallback when package not on path
        return PressureConfig(
            weights={
                "theme_deterioration": 0.30,
                "profit_preservation": 0.20,
                "tech_exhaustion":     0.20,
                "options_risk":        0.15,
                "rotation_pressure":   0.15,
            },
            band_hold_max=40.0, band_light_max=60.0, band_heavy_max=75.0,
            name="incumbent(doc)",
        )


def named_configs(incumbent: "PressureConfig") -> dict[str, "PressureConfig"]:
    """Hand-named candidate configs the harness always evaluates, alongside
    the random search. These let us track *specific* hypotheses (the LEAPS
    recast and a couple of pillar tilts) against forward outcomes over time,
    rather than only reporting whatever the search happened to find.

    `leaps_recast` is the production change: drop options_risk, renormalise the
    surviving four proportionally — a neutral de-biasing, no pillar preference.
    The tilt variants are the *evidence-seeking* part: do we do better by
    leaning harder on technical exhaustion (the only component that fired much
    in practice) or on rotation/better-opportunity?
    """
    base = {
        "theme_deterioration": 0.30, "profit_preservation": 0.20,
        "tech_exhaustion": 0.20, "options_risk": 0.0, "rotation_pressure": 0.15,
    }
    cfgs = {
        "incumbent": incumbent,
        "leaps_recast": PressureConfig(dict(base), 40.0, 60.0, 75.0, name="leaps_recast"),
        "leaps_tech_tilt": PressureConfig(
            {"theme_deterioration": 0.25, "profit_preservation": 0.10,
             "tech_exhaustion": 0.40, "options_risk": 0.0, "rotation_pressure": 0.25},
            40.0, 60.0, 75.0, name="leaps_tech_tilt"),
        "leaps_rotation_tilt": PressureConfig(
            {"theme_deterioration": 0.35, "profit_preservation": 0.10,
             "tech_exhaustion": 0.20, "options_risk": 0.0, "rotation_pressure": 0.35},
            40.0, 60.0, 75.0, name="leaps_rotation_tilt"),
    }
    return cfgs


@dataclass
class PressureConfig:
    """A candidate set of exit-pressure parameters — the 'train.py' analog.

    This is the *only* thing the optimiser mutates. Weights are renormalised
    to sum to 1 on use, so the optimiser may search an unnormalised simplex.
    """

    weights: dict[str, float]
    band_hold_max: float = 40.0
    band_light_max: float = 60.0
    band_heavy_max: float = 75.0
    name: str = "candidate"

    def norm_weights(self) -> dict[str, float]:
        total = sum(max(0.0, self.weights.get(k, 0.0)) for k in SUBSCORE_KEYS)
        if total <= 0:
            # Degenerate; fall back to uniform so scoring stays defined.
            return {k: 1.0 / len(SUBSCORE_KEYS) for k in SUBSCORE_KEYS}
        return {k: max(0.0, self.weights.get(k, 0.0)) / total for k in SUBSCORE_KEYS}

    def score(self, sub: pd.DataFrame) -> pd.Series:
        """Vectorised score for a frame whose columns are the sub-score keys."""
        w = self.norm_weights()
        return sum(sub[k] * w[k] for k in SUBSCORE_KEYS)

    def band(self, score: pd.Series) -> pd.Series:
        bins = [-np.inf, self.band_hold_max, self.band_light_max,
                self.band_heavy_max, np.inf]
        labels = ["hold", "trim_light", "trim_heavy", "aggressive"]
        return pd.cut(score, bins=bins, labels=labels, right=False)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "weights": self.norm_weights(),
            "bands": {
                "hold_max": self.band_hold_max,
                "light_max": self.band_light_max,
                "heavy_max": self.band_heavy_max,
            },
        }


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

PRESSURE_ACTION_TYPES = ("position_pressure_hold",)


def _db_path_from_url(url: str) -> str:
    """Extract a filesystem path from a sqlite SQLAlchemy URL."""
    # sqlite+aiosqlite:///./agentic_edge.db  ->  ./agentic_edge.db
    if ":///" in url:
        return url.split(":///", 1)[1]
    return url


def resolve_db_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)
    try:
        from app.config import get_settings  # type: ignore
        return Path(_db_path_from_url(get_settings().DATABASE_URL))
    except Exception:
        return Path("agentic_edge.db")


def load_pressure_observations(conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per logged exit-pressure observation, sub-scores exploded out.

    Reads the JSON ``payload`` of every pressure-bearing ``auto_actions`` row
    and pulls the five ``sub_scores``. Rows without a sub_scores block are
    skipped (they are not pressure computations).
    """
    placeholders = ",".join("?" for _ in PRESSURE_ACTION_TYPES)
    rows = conn.execute(
        f"SELECT timestamp, symbol, payload FROM auto_actions "
        f"WHERE action_type IN ({placeholders}) AND symbol IS NOT NULL "
        f"ORDER BY timestamp",
        PRESSURE_ACTION_TYPES,
    ).fetchall()

    recs: list[dict] = []
    for ts, symbol, payload in rows:
        if not payload:
            continue
        try:
            d = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        sub = d.get("sub_scores")
        if not isinstance(sub, dict):
            continue
        rec = {
            "ts": pd.to_datetime(ts, utc=True),
            "symbol": symbol,
            "logged_score": d.get("score"),
            "logged_band": d.get("band"),
        }
        for k in SUBSCORE_KEYS:
            rec[k] = float(sub.get(k) or 0.0)
        recs.append(rec)

    df = pd.DataFrame.from_records(recs)
    if not df.empty:
        df = df.sort_values("ts").reset_index(drop=True)
    return df


def build_price_panel(conn: sqlite3.Connection) -> dict[str, pd.Series]:
    """Per-symbol daily ``last_price`` series from the positions snapshots.

    The positions table is snapshotted many times intraday. We resample to one
    price per (symbol, calendar day) using the last snapshot of the day.

    Reset guard: a book-wide re-seed writes ``pnl=0`` with ``avg_price ==
    last_price`` for *every* row at one ``captured_at``. Those timestamps carry
    no real quote, so we drop any snapshot timestamp where the whole book is in
    that reset state before resampling.
    """
    df = pd.read_sql_query(
        "SELECT symbol, last_price, avg_price, pnl, captured_at FROM positions",
        conn,
    )
    if df.empty:
        return {}
    df["captured_at"] = pd.to_datetime(df["captured_at"], utc=True)

    # Identify book-wide reset timestamps and drop them.
    df["_is_reset_row"] = (df["pnl"] == 0.0) & np.isclose(df["avg_price"], df["last_price"])
    per_ts = df.groupby("captured_at")["_is_reset_row"].mean()
    reset_ts = set(per_ts[per_ts >= 0.999].index)
    if reset_ts:
        df = df[~df["captured_at"].isin(reset_ts)]

    df = df[df["last_price"] > 0]
    df["day"] = df["captured_at"].dt.floor("D")

    panel: dict[str, pd.Series] = {}
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("captured_at")
        daily = g.groupby("day")["last_price"].last()
        if len(daily) >= 2:
            panel[sym] = daily
    return panel


# ---------------------------------------------------------------------------
# Outcome labelling
# ---------------------------------------------------------------------------


def _forward_metrics(
    series: pd.Series, t0: pd.Timestamp, horizon_days: int
) -> tuple[Optional[float], Optional[float]]:
    """(forward_return, forward_max_drawdown) over (t0, t0+horizon].

    forward_return: last available price in window / price at-or-before t0 - 1.
    forward_max_drawdown: most negative (trough/running_peak - 1) in window
    (<= 0; deeper loss is more negative).
    """
    day0 = t0.floor("D")
    # Anchor price = last price at or before the observation day.
    at_or_before = series[series.index <= day0]
    if at_or_before.empty:
        return None, None
    p0 = float(at_or_before.iloc[-1])
    if p0 <= 0:
        return None, None

    window = series[(series.index > day0) & (series.index <= day0 + timedelta(days=horizon_days))]
    if window.empty:
        return None, None

    fwd_ret = float(window.iloc[-1]) / p0 - 1.0

    # Max drawdown across the path including the anchor.
    path = np.concatenate([[p0], window.to_numpy(dtype=float)])
    running_peak = np.maximum.accumulate(path)
    dd = path / running_peak - 1.0
    fwd_maxdd = float(dd.min())
    return fwd_ret, fwd_maxdd


def dedupe_to_symbol(obs: pd.DataFrame) -> pd.DataFrame:
    """Collapse to one observation per symbol — the LAST pressure reading.

    The raw log samples the same ~two dozen symbols many times over a short
    window. Treating each intraday sample as an independent observation is
    pseudo-replication: it inflates any correlation by repeating the same
    symbol→outcome pair dozens of times. For a clean (if small) cross-section
    we keep only each symbol's most recent reading before its forward window.
    """
    if obs.empty:
        return obs
    return (obs.sort_values("ts")
               .groupby("symbol", as_index=False)
               .tail(1)
               .reset_index(drop=True))


def attach_outcomes(
    obs: pd.DataFrame, panel: dict[str, pd.Series], horizon_days: int
) -> pd.DataFrame:
    """Add fwd_ret / fwd_maxdd columns for the given horizon; drop unlabelable rows."""
    if obs.empty:
        return obs.assign(fwd_ret=[], fwd_maxdd=[])
    rets: list[Optional[float]] = []
    dds: list[Optional[float]] = []
    for _, r in obs.iterrows():
        series = panel.get(r["symbol"])
        if series is None:
            rets.append(None); dds.append(None); continue
        ret, dd = _forward_metrics(series, r["ts"], horizon_days)
        rets.append(ret); dds.append(dd)
    out = obs.copy()
    out["fwd_ret"] = rets
    out["fwd_maxdd"] = dds
    out = out.dropna(subset=["fwd_ret", "fwd_maxdd"]).reset_index(drop=True)
    return out


def component_variance(obs: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Per-sub-score spread diagnostic — flags 'dead' components (always 0).

    A component with zero variance cannot influence ranking no matter its
    weight, so any weight it carries is dead allocation. This is one of the
    more actionable things the harness surfaces about the live formula.
    """
    out: dict[str, dict[str, float]] = {}
    for k in SUBSCORE_KEYS:
        if k in obs:
            col = obs[k]
            out[k] = {
                "mean": float(col.mean()),
                "std": float(col.std(ddof=0)),
                "nonzero_frac": float((col != 0).mean()),
                "max": float(col.max()),
            }
    return out


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    n: int = 0
    rank_ic: float = float("nan")     # sign-flipped Spearman(score, maxdd)
    band_edge: float = float("nan")   # mean fwd_ret(hold) - mean fwd_ret(exit)
    fitness: float = float("nan")
    band_counts: dict[str, int] = field(default_factory=dict)
    band_mean_ret: dict[str, float] = field(default_factory=dict)


EXIT_BANDS = ("trim_heavy", "aggressive")


def evaluate(obs: pd.DataFrame, cfg: PressureConfig) -> EvalResult:
    """Score every observation under ``cfg`` and measure forward separation."""
    if obs.empty:
        return EvalResult()
    score = cfg.score(obs)
    band = cfg.band(score).astype("object")

    # rank_ic: high pressure should predict deep (more negative) drawdown.
    # Spearman(score, maxdd) is negative when that holds, so flip the sign.
    if score.nunique() > 1 and obs["fwd_maxdd"].nunique() > 1:
        spear = pd.Series(score).corr(obs["fwd_maxdd"], method="spearman")
        rank_ic = -float(spear) if spear == spear else float("nan")
    else:
        rank_ic = float("nan")

    hold_ret = obs.loc[band == "hold", "fwd_ret"]
    exit_ret = obs.loc[band.isin(EXIT_BANDS), "fwd_ret"]
    if len(hold_ret) and len(exit_ret):
        band_edge = float(hold_ret.mean() - exit_ret.mean())
    else:
        band_edge = float("nan")

    parts = [v for v in (rank_ic, band_edge) if v == v]
    fitness = float(sum(parts)) if parts else float("nan")

    counts = band.value_counts().to_dict()
    mean_ret = obs.assign(_b=band).groupby("_b")["fwd_ret"].mean().to_dict()
    return EvalResult(
        n=len(obs), rank_ic=rank_ic, band_edge=band_edge, fitness=fitness,
        band_counts={k: int(v) for k, v in counts.items()},
        band_mean_ret={str(k): float(v) for k, v in mean_ret.items()},
    )


# ---------------------------------------------------------------------------
# The autoresearch search loop
# ---------------------------------------------------------------------------


def _sample_config(rng: np.random.Generator, incumbent: PressureConfig) -> PressureConfig:
    """Draw a candidate near a plausible region of weight/band space.

    Weights: Dirichlet draw (a point on the simplex) so they always sum to 1.
    Bands: jitter the incumbent thresholds, keeping them ordered and in (0,100).
    """
    w = rng.dirichlet(np.ones(len(SUBSCORE_KEYS)) * 2.0)  # alpha=2 → mild central pull
    weights = {k: float(w[i]) for i, k in enumerate(SUBSCORE_KEYS)}

    hold = float(np.clip(incumbent.band_hold_max + rng.normal(0, 8), 10, 70))
    light = float(np.clip(hold + abs(rng.normal(15, 6)), hold + 5, 90))
    heavy = float(np.clip(light + abs(rng.normal(15, 6)), light + 5, 98))
    return PressureConfig(weights, hold, light, heavy, name="candidate")


def random_search(
    train: pd.DataFrame, n_samples: int, seed: int, incumbent: PressureConfig
) -> tuple[PressureConfig, EvalResult]:
    rng = np.random.default_rng(seed)
    best_cfg, best_eval = incumbent, evaluate(train, incumbent)
    for _ in range(n_samples):
        cand = _sample_config(rng, incumbent)
        ev = evaluate(train, cand)
        if ev.fitness == ev.fitness and (best_eval.fitness != best_eval.fitness or ev.fitness > best_eval.fitness):
            best_cfg, best_eval = cand, ev
    return best_cfg, best_eval


def time_split(obs: pd.DataFrame, train_frac: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Walk-forward split: earliest ``train_frac`` of rows train, rest validate."""
    if obs.empty:
        return obs, obs
    obs = obs.sort_values("ts").reset_index(drop=True)
    cut = int(len(obs) * train_frac)
    return obs.iloc[:cut].reset_index(drop=True), obs.iloc[cut:].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Orchestration / reporting
# ---------------------------------------------------------------------------


@dataclass
class ReplayReport:
    horizon_days: int
    n_observations: int
    n_labelled: int
    n_effective: int
    symbols: int
    component_variance: dict
    incumbent: dict
    incumbent_eval_full: EvalResult
    named_evals: dict[str, dict]
    best: dict
    best_eval_train: EvalResult
    best_eval_val: EvalResult
    incumbent_eval_val: EvalResult
    warnings: list[str] = field(default_factory=list)


def run_replay(
    db_path: Optional[str] = None,
    horizon_days: int = 21,
    n_samples: int = 2000,
    train_frac: float = 0.6,
    seed: int = 7,
    dedupe_symbol: bool = True,
) -> ReplayReport:
    path = resolve_db_path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    conn = sqlite3.connect(str(path))
    try:
        obs_raw = load_pressure_observations(conn)
        panel = build_price_panel(conn)
    finally:
        conn.close()

    warnings: list[str] = []
    n_obs = len(obs_raw)
    obs_full = attach_outcomes(obs_raw, panel, horizon_days)
    incumbent = _load_incumbent()
    comp_var = component_variance(obs_full) if not obs_full.empty else {}

    # Dead-component diagnostic — a component that never varied carries dead weight.
    dead = [k for k, v in comp_var.items() if v["std"] == 0.0]
    if dead:
        dead_wt = sum(incumbent.norm_weights()[k] for k in dead)
        warnings.append(
            f"DEAD COMPONENTS (zero variance over this window): {', '.join(dead)} "
            f"— they carry {dead_wt:.0%} of incumbent weight but cannot affect ranking here."
        )

    # Collapse pseudo-replication: one obs per symbol unless explicitly disabled.
    obs = dedupe_to_symbol(obs_full) if dedupe_symbol else obs_full

    if obs.empty:
        warnings.append("no labelled observations — cannot evaluate (need pressure logs with a forward price window)")
        empty = EvalResult()
        return ReplayReport(horizon_days, n_obs, 0, 0, len(panel), comp_var,
                            incumbent.to_dict(), empty, {}, incumbent.to_dict(),
                            empty, empty, empty, warnings)

    # Degenerate-outcome warning: forward window fell inside a price-data gap.
    if obs["fwd_ret"].nunique() <= 1 or float(obs["fwd_maxdd"].min()) == 0.0:
        warnings.append(
            "DEGENERATE OUTCOMES: forward returns show ~no variation at this horizon "
            "— the forward window likely falls inside a position-snapshot gap. "
            "Try a larger --horizon so the window reaches the next price observations."
        )

    # Power warning — the honest caveat.
    span_days = (obs["ts"].max() - obs["ts"].min()).total_seconds() / 86400.0
    n_eff = obs["symbol"].nunique()
    if n_eff < 30 or len(obs) < 100:
        warnings.append(
            f"LOW STATISTICAL POWER: effective sample is {n_eff} independent symbols "
            f"(raw labelled obs {len(obs_full)} collapse to {len(obs)} after symbol-dedupe), "
            f"spanning {span_days:.1f} days. Treat results as a smoke test of the machinery, "
            f"not a re-weight mandate."
        )

    incumbent_full = evaluate(obs, incumbent)

    train, val = time_split(obs, train_frac)

    # Evaluate the hand-named hypotheses (LEAPS recast + tilts) on full sample
    # and on the validation slice, so specific candidates are tracked over time.
    from dataclasses import asdict as _asdict
    named_evals: dict[str, dict] = {}
    for nm, cfg in named_configs(incumbent).items():
        named_evals[nm] = {
            "weights": cfg.norm_weights(),
            "full": _asdict(evaluate(obs, cfg)),
            "val": _asdict(evaluate(val if len(val) >= 10 else obs, cfg)),
        }
    if len(val) < 10 or len(train) < 10:
        warnings.append(
            f"walk-forward split too small (train={len(train)}, val={len(val)}); "
            f"validation lift is unreliable — reporting in-sample only as indicative."
        )
    best_cfg, best_train_eval = random_search(train if len(train) >= 10 else obs,
                                              n_samples, seed, incumbent)
    best_val_eval = evaluate(val if len(val) >= 10 else obs, best_cfg)
    incumbent_val_eval = evaluate(val if len(val) >= 10 else obs, incumbent)

    return ReplayReport(
        horizon_days=horizon_days,
        n_observations=n_obs,
        n_labelled=len(obs_full),
        n_effective=len(obs),
        symbols=len(panel),
        component_variance=comp_var,
        incumbent=incumbent.to_dict(),
        incumbent_eval_full=incumbent_full,
        named_evals=named_evals,
        best=best_cfg.to_dict(),
        best_eval_train=best_train_eval,
        best_eval_val=best_val_eval,
        incumbent_eval_val=incumbent_val_eval,
        warnings=warnings,
    )


def _fmt(x: float) -> str:
    return "n/a" if x != x else f"{x:+.4f}"


def print_report(rep: ReplayReport) -> None:
    line = "=" * 72
    print(line)
    print("EXIT-PRESSURE REPLAY  —  autoresearch-style offline tuner")
    print(line)
    print(f"horizon            : {rep.horizon_days} calendar days forward")
    print(f"pressure obs (raw) : {rep.n_observations}")
    print(f"labelled obs       : {rep.n_labelled}   (had a forward price window)")
    print(f"effective sample   : {rep.n_effective}   (after symbol-dedupe)")
    print(f"symbols in panel   : {rep.symbols}")
    print()

    if rep.component_variance:
        print("SUB-SCORE VARIANCE (a zero-std component carries dead weight)")
        for k, v in rep.component_variance.items():
            tag = "  <- DEAD" if v["std"] == 0.0 else ""
            print(f"  {k:22s} mean={v['mean']:6.2f}  std={v['std']:6.2f}  "
                  f"nonzero={v['nonzero_frac']:.0%}  max={v['max']:5.1f}{tag}")
        print()

    if rep.warnings:
        print("WARNINGS")
        for w in rep.warnings:
            print(f"  ! {w}")
        print()

    inc, best = rep.incumbent_eval_full, rep.best_eval_train
    print("INCUMBENT (production weights)  — full sample")
    print(f"  weights : {json.dumps({k: round(v,3) for k,v in rep.incumbent['weights'].items()})}")
    print(f"  bands   : {rep.incumbent['bands']}")
    print(f"  n={inc.n}  rank_ic={_fmt(inc.rank_ic)}  band_edge={_fmt(inc.band_edge)}  fitness={_fmt(inc.fitness)}")
    if inc.band_mean_ret:
        print(f"  fwd_ret by band : { {k: round(v,4) for k,v in inc.band_mean_ret.items()} }")
    print()

    if rep.named_evals:
        print("NAMED CONFIGS (tracked hypotheses)  — full-sample fitness")
        print(f"  {'config':22s} {'rank_ic':>9s} {'band_edge':>10s} {'fitness':>9s}")
        for nm, ev in rep.named_evals.items():
            f = ev["full"]
            print(f"  {nm:22s} {_fmt(f['rank_ic']):>9s} {_fmt(f['band_edge']):>10s} {_fmt(f['fitness']):>9s}")
        print("  (leaps_recast = production change: options_risk dropped, four")
        print("   surviving weights renormalised proportionally — neutral de-bias)")
        print()

    print("BEST FOUND (search)  — train / validation")
    print(f"  weights : {json.dumps({k: round(v,3) for k,v in rep.best['weights'].items()})}")
    print(f"  bands   : { {k: round(v,1) for k,v in rep.best['bands'].items()} }")
    print(f"  TRAIN : n={best.n}  rank_ic={_fmt(best.rank_ic)}  band_edge={_fmt(best.band_edge)}  fitness={_fmt(best.fitness)}")
    bv, iv = rep.best_eval_val, rep.incumbent_eval_val
    print(f"  VAL   : best fitness={_fmt(bv.fitness)}   incumbent fitness={_fmt(iv.fitness)}")
    if bv.fitness == bv.fitness and iv.fitness == iv.fitness:
        lift = bv.fitness - iv.fitness
        verdict = "BEATS incumbent out-of-sample" if lift > 0 else "does NOT beat incumbent out-of-sample"
        print(f"  VAL lift (best - incumbent) = {_fmt(lift)}  →  {verdict}")
    print()
    print("PROMOTION GATE: this tool only recommends. Do not change live weights")
    print("until the validation lift is positive AND statistically credible on a")
    print("larger sample. Re-run nightly as pressure logs accumulate.")
    print(line)


def main(argv: Optional[Iterable[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    # Windows consoles default to cp1252; force UTF-8 so the report's symbols
    # never crash the run. Best-effort — older Pythons may lack reconfigure().
    try:  # pragma: no cover - environment dependent
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="path to sqlite db (default: from settings / ./agentic_edge.db)")
    ap.add_argument("--horizon", type=int, default=21, help="forward outcome horizon in calendar days")
    ap.add_argument("--samples", type=int, default=2000, help="random-search candidates")
    ap.add_argument("--train-frac", type=float, default=0.6, help="walk-forward train fraction")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-dedupe", action="store_true", help="keep every intraday sample (pseudo-replication; not recommended)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of the report")
    args = ap.parse_args(list(argv) if argv is not None else None)

    rep = run_replay(args.db, args.horizon, args.samples, args.train_frac, args.seed,
                     dedupe_symbol=not args.no_dedupe)
    if args.json:
        from dataclasses import asdict
        print(json.dumps(asdict(rep), default=str, indent=2))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
