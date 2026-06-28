"""Pooled cross-sectional ML ranker — rank the universe by predicted return.

The small-N fix made real: we do NOT fit per-theme (4 names = guaranteed
overfit). We pool EVERY theme symbol across EVERY snapshot date into one
cross-section, with theme membership already encoded as features, and learn a
single model that ranks names by expected forward return.

Offline + read-only, same discipline as the other harnesses. Two model paths:

  * ridge   — pure-numpy ridge regression (default). No overfitting knobs, no
              dependency surprises; the right default on a thin early sample.
  * gbm     — sklearn GradientBoostingRegressor when explicitly requested and
              there is enough data to justify it.

Cold start (no labelled rows yet) degrades to a transparent HEURISTIC ranking
(chokepoint centrality + the Aschenbrenner buildout lens), clearly flagged — so
the endpoint is useful from day one and sharpens into a trained model as labels
accumulate. Research-only: nothing here sizes or places a trade.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from .feature_research import feature_columns, load_feature_panel, resolve_db_path

logger = logging.getLogger(__name__)

DEFAULT_HORIZON = 20
RIDGE_ALPHA = 1.0
# Below this many labelled training rows we don't trust a fit — fall back to the
# transparent heuristic rather than pretend a model learned something.
MIN_TRAIN_ROWS = 40
# Heuristic cold-start blend (z-features, all same-day, no lookahead).
_HEURISTIC_WEIGHTS = {"z_theme_centrality": 1.0, "persona_aschenbrenner": 0.03,
                      "z_smartmoney_theme_confirm": 0.5, "z_momentum_60d": 0.4}


# ---------------------------------------------------------------------------
# Pure numeric core (unit-tested)
# ---------------------------------------------------------------------------


def standardize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Column-standardise; return (Xs, mean, std). Zero-variance cols → std 1."""
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std = np.where(std == 0.0, 1.0, std)
    return (X - mean) / std, mean, std


def ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float = RIDGE_ALPHA) -> tuple[np.ndarray, float]:
    """Closed-form ridge on standardised X. Returns (coef, intercept).

    Intercept is the mean of y (X is centred by standardisation), and the
    penalty is not applied to it. Robust on wide/thin matrices.
    """
    n, p = X.shape
    Xs, _, _ = standardize(X)
    yc = y - y.mean()
    A = Xs.T @ Xs + alpha * np.eye(p)
    coef = np.linalg.solve(A, Xs.T @ yc)
    return coef, float(y.mean())


def ridge_predict(X: np.ndarray, coef: np.ndarray, intercept: float,
                  mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    """Predict with a fit produced against a TRAIN standardisation (mean/std)."""
    std = np.where(std == 0.0, 1.0, std)
    Xs = (X - mean) / std
    return Xs @ coef + intercept


def rank_percentiles(scores: np.ndarray) -> np.ndarray:
    """0..1 percentile rank of each score (ties share the average rank)."""
    s = pd.Series(scores)
    return (s.rank(method="average") - 1).to_numpy() / max(len(s) - 1, 1)


# ---------------------------------------------------------------------------
# Matrix assembly
# ---------------------------------------------------------------------------


def _impute(df: pd.DataFrame, cols: list[str], medians: pd.Series) -> np.ndarray:
    """Fill NaN with provided (train) medians, then 0 for still-empty cols."""
    return df[cols].fillna(medians).fillna(0.0).to_numpy(dtype="float64")


def _usable_feature_cols(df: pd.DataFrame, feats: list[str]) -> list[str]:
    """Drop columns that are entirely NaN over the panel (no information)."""
    return [c for c in feats if df[c].notna().any()]


# ---------------------------------------------------------------------------
# Train + rank
# ---------------------------------------------------------------------------


@dataclass
class RankRow:
    symbol: str
    predicted_return: float
    rank: int
    percentile: float


@dataclass
class RankReport:
    model: str                       # ridge | gbm | heuristic
    horizon_days: int
    n_train_rows: int
    n_ranked: int
    as_of: Optional[str]
    features_used: list[str]
    top_drivers: list[str]
    ranking: list[dict]
    warnings: list[str] = field(default_factory=list)


def _heuristic_scores(latest: pd.DataFrame) -> np.ndarray:
    out = np.zeros(len(latest), dtype="float64")
    for feat, w in _HEURISTIC_WEIGHTS.items():
        if feat in latest.columns:
            out = out + w * latest[feat].fillna(0.0).to_numpy(dtype="float64")
    return out


def train_and_rank(
    db_path: Optional[str] = None, horizon_days: int = DEFAULT_HORIZON,
    model: str = "ridge", alpha: float = RIDGE_ALPHA,
) -> RankReport:
    path = resolve_db_path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    conn = sqlite3.connect(str(path))
    try:
        df = load_feature_panel(conn)
    finally:
        conn.close()

    warnings: list[str] = []
    if df.empty:
        return RankReport("heuristic", horizon_days, 0, 0, None, [], [], [],
                          ["no feature snapshots yet — run the snapshot job first"])

    label = f"fwd_ret_{horizon_days}d"
    feats = _usable_feature_cols(df, feature_columns(df))
    # Latest snapshot date = what we rank.
    latest_day = df["as_of"].max()
    latest = df[df["as_of"] == latest_day].reset_index(drop=True)

    # Training rows: labelled at this horizon.
    train = df[df[label].notna()] if label in df.columns else df.iloc[0:0]
    n_train = len(train)

    if n_train < MIN_TRAIN_ROWS or not feats:
        scores = _heuristic_scores(latest)
        warnings.append(
            f"COLD START: only {n_train} labelled rows at {horizon_days}d "
            f"(need >={MIN_TRAIN_ROWS}). Ranking by transparent heuristic "
            f"(centrality + buildout lens), NOT a trained model. Sharpens as the "
            f"labeler accumulates forward returns."
        )
        return _assemble(latest, scores, "heuristic", horizon_days, n_train,
                         feats, {}, warnings)

    medians = train[feats].median(numeric_only=True)
    Xtr = _impute(train, feats, medians)
    ytr = train[label].to_numpy(dtype="float64")
    Xlt = _impute(latest, feats, medians)

    drivers: dict[str, float] = {}
    if model == "gbm":
        try:
            from sklearn.ensemble import GradientBoostingRegressor
            gbm = GradientBoostingRegressor(random_state=0, n_estimators=200,
                                            max_depth=2, learning_rate=0.05)
            gbm.fit(Xtr, ytr)
            scores = gbm.predict(Xlt)
            drivers = dict(zip(feats, gbm.feature_importances_))
        except Exception as e:  # pragma: no cover - optional dependency path
            warnings.append(f"gbm unavailable ({e}); fell back to ridge")
            model = "ridge"
    if model == "ridge":
        _, mean, std = standardize(Xtr)
        coef, intercept = ridge_fit(Xtr, ytr, alpha)
        scores = ridge_predict(Xlt, coef, intercept, mean, std)
        drivers = dict(zip(feats, np.abs(coef)))

    top_drivers = [k for k, _ in sorted(drivers.items(), key=lambda kv: kv[1], reverse=True)[:6]]
    rep = _assemble(latest, scores, model, horizon_days, n_train, feats, {}, warnings)
    rep.top_drivers = top_drivers
    return rep


def _assemble(latest: pd.DataFrame, scores: np.ndarray, model: str, horizon: int,
              n_train: int, feats: list[str], _drivers: dict, warnings: list[str]) -> RankReport:
    pct = rank_percentiles(scores)
    order = np.argsort(-scores)
    ranking: list[dict] = []
    for rank, idx in enumerate(order, start=1):
        ranking.append({
            "symbol": latest.iloc[idx]["symbol"],
            "predicted_return": round(float(scores[idx]), 5),
            "rank": rank,
            "percentile": round(float(pct[idx]), 4),
        })
    as_of = latest["as_of"].max()
    return RankReport(
        model=model, horizon_days=horizon, n_train_rows=int(n_train),
        n_ranked=len(ranking), as_of=as_of.isoformat() if as_of is not None else None,
        features_used=feats, top_drivers=[], ranking=ranking, warnings=warnings,
    )


async def rank_universe(horizon_days: int = DEFAULT_HORIZON, model: str = "ridge") -> dict:
    """Async wrapper for the API — runs the sync core off the event loop."""
    import asyncio
    from dataclasses import asdict
    rep = await asyncio.to_thread(train_and_rank, None, horizon_days, model)
    return asdict(rep)


def print_report(rep: RankReport) -> None:
    line = "=" * 74
    print(line)
    print("ML RANKER  —  pooled cross-sectional forward-return ranking")
    print(line)
    print(f"model / horizon    : {rep.model}  /  {rep.horizon_days}d")
    print(f"train rows         : {rep.n_train_rows}")
    print(f"ranked (as of)     : {rep.n_ranked}  ({rep.as_of})")
    if rep.top_drivers:
        print(f"top drivers        : {', '.join(rep.top_drivers)}")
    print()
    if rep.warnings:
        print("WARNINGS")
        for w in rep.warnings:
            print(f"  ! {w}")
        print()
    print(f"  {'rank':>4s}  {'symbol':8s} {'pred_ret':>10s} {'pctile':>8s}")
    for r in rep.ranking[:25]:
        print(f"  {r['rank']:>4d}  {r['symbol']:8s} {r['predicted_return']:>+10.4f} {r['percentile']:>8.2f}")
    print()
    print("PROMOTION GATE: research only — predictions never size or place a trade.")
    print(line)


def main(argv: Optional[Iterable[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    try:  # pragma: no cover
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    ap.add_argument("--model", choices=["ridge", "gbm"], default="ridge")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    rep = train_and_rank(args.db, args.horizon, args.model)
    if args.json:
        from dataclasses import asdict
        print(json.dumps(asdict(rep), default=str, indent=2))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
