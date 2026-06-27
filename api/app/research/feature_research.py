"""Feature Research Harness — information coefficient + alpha-decay profiler.

The quant analog of ``exit_pressure_replay.py``: strictly offline, read-only.
It answers the two questions that turn a pile of features into research:

  1. Which features actually predict forward returns?  (rank IC)
  2. How fast does each feature's edge decay?           (IC across horizons)

For every numeric feature in ``symbol_feature_snapshots`` it computes the
Spearman rank correlation (IC) against the realised forward return at each
horizon {5, 20, 60} days, using only rows whose label is known. The IC across
horizons IS the alpha-decay curve — a feature whose IC peaks at 5d and fades by
60d is a *fast* signal (act now); one that builds toward 60d is a slow,
positional signal.

This directly serves the operator's recurring worry — *timely decisions*: the
decay profile tells you, per signal, whether you are early or late.

DISCIPLINE (identical to the exit-pressure harness): it recommends, it never
writes config and never trades. Early output, when snapshots are few, is a
smoke test of the machinery — the honesty warnings say so — and sharpens
automatically as the nightly snapshot job accumulates history.
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

logger = logging.getLogger(__name__)

HORIZONS: tuple[int, ...] = (5, 20, 60)
# Features we never rank (identifiers / list-valued / the labels themselves).
_NON_FEATURE_KEYS = {"themes"}


def spearman_ic(xs: Iterable[float], ys: Iterable[float]) -> float:
    """Spearman rank correlation between two equal-length numeric sequences.

    Pure + dependency-light (pandas). Returns NaN when undefined (fewer than 2
    pairs, or no variation on either side). Exposed for unit testing.
    """
    x = pd.Series(list(xs), dtype="float64")
    y = pd.Series(list(ys), dtype="float64")
    mask = x.notna() & y.notna()
    x, y = x[mask], y[mask]
    if len(x) < 2 or x.nunique() < 2 or y.nunique() < 2:
        return float("nan")
    return float(x.corr(y, method="spearman"))


def _db_path_from_url(url: str) -> str:
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


def load_feature_panel(conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per snapshot: symbol, as_of, label_status, exploded features +
    fwd_ret_{h}d label columns. Non-numeric / list features are dropped."""
    rows = conn.execute(
        "SELECT symbol, as_of, features, labels, label_status "
        "FROM symbol_feature_snapshots ORDER BY as_of, symbol"
    ).fetchall()

    recs: list[dict] = []
    for symbol, as_of, features, labels, status in rows:
        try:
            feat = json.loads(features) if features else {}
        except (TypeError, json.JSONDecodeError):
            feat = {}
        try:
            lab = json.loads(labels) if labels else {}
        except (TypeError, json.JSONDecodeError):
            lab = {}
        rec = {"symbol": symbol, "as_of": pd.to_datetime(as_of, utc=True), "label_status": status}
        for k, v in feat.items():
            if k in _NON_FEATURE_KEYS:
                continue
            if isinstance(v, (int, float)) and not (isinstance(v, bool)):
                rec[k] = float(v)
        for h in HORIZONS:
            key = f"fwd_ret_{h}d"
            v = lab.get(key)
            rec[key] = float(v) if isinstance(v, (int, float)) else np.nan
        recs.append(rec)

    df = pd.DataFrame.from_records(recs)
    return df


def feature_columns(df: pd.DataFrame) -> list[str]:
    """Numeric feature columns present in the panel (excludes labels/ids)."""
    label_cols = {f"fwd_ret_{h}d" for h in HORIZONS}
    skip = {"symbol", "as_of", "label_status"} | label_cols
    return [c for c in df.columns if c not in skip]


def compute_ic_table(df: pd.DataFrame) -> dict[str, dict[int, dict[str, float]]]:
    """{feature: {horizon: {ic, n}}} — pooled Spearman IC vs forward return."""
    out: dict[str, dict[int, dict[str, float]]] = {}
    for feat in feature_columns(df):
        out[feat] = {}
        for h in HORIZONS:
            label = f"fwd_ret_{h}d"
            if label not in df.columns:
                out[feat][h] = {"ic": float("nan"), "n": 0}
                continue
            pair = df[[feat, label]].dropna()
            ic = spearman_ic(pair[feat], pair[label]) if len(pair) >= 2 else float("nan")
            out[feat][h] = {"ic": ic, "n": int(len(pair))}
    return out


def alpha_decay(ic_table: dict[str, dict[int, dict[str, float]]]) -> dict[str, dict]:
    """Per feature: peak-IC horizon and a coarse decay verdict.

    fast    — |IC| peaks at the shortest horizon (act immediately)
    slow    — |IC| peaks at the longest horizon (positional, patient)
    mid     — peaks in between
    flat    — no usable IC anywhere
    """
    out: dict[str, dict] = {}
    for feat, by_h in ic_table.items():
        usable = {h: d["ic"] for h, d in by_h.items() if d["ic"] == d["ic"]}
        if not usable:
            out[feat] = {"peak_horizon": None, "peak_ic": float("nan"), "verdict": "flat"}
            continue
        peak_h = max(usable, key=lambda h: abs(usable[h]))
        horizons_sorted = sorted(usable.keys())
        if peak_h == horizons_sorted[0]:
            verdict = "fast"
        elif peak_h == horizons_sorted[-1]:
            verdict = "slow"
        else:
            verdict = "mid"
        out[feat] = {"peak_horizon": peak_h, "peak_ic": round(usable[peak_h], 4), "verdict": verdict}
    return out


@dataclass
class ResearchReport:
    n_snapshots: int
    n_labelled: dict[str, int]          # per-horizon count of usable label rows
    n_symbols: int
    n_dates: int
    features: list[str]
    ic_table: dict
    alpha_decay: dict
    warnings: list[str] = field(default_factory=list)


def run_research(db_path: Optional[str] = None) -> ResearchReport:
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
        return ResearchReport(0, {f"{h}d": 0 for h in HORIZONS}, 0, 0, [], {}, {},
                              ["no feature snapshots yet — run the snapshot job first"])

    feats = feature_columns(df)
    n_labelled = {f"{h}d": int(df[f"fwd_ret_{h}d"].notna().sum()) for h in HORIZONS}
    n_symbols = int(df["symbol"].nunique())
    n_dates = int(df["as_of"].dt.floor("D").nunique())

    ic_table = compute_ic_table(df)
    decay = alpha_decay(ic_table)

    if max(n_labelled.values()) == 0:
        warnings.append(
            "NO LABELLED ROWS yet — every snapshot is still inside its forward "
            "window. IC is undefined until the labeler backfills returns (run it "
            "once snapshots are >5 calendar days old)."
        )
    if n_dates < 5 or max(n_labelled.values()) < 50:
        warnings.append(
            f"LOW STATISTICAL POWER: {n_dates} distinct snapshot date(s), "
            f"max {max(n_labelled.values())} labelled rows at any horizon over "
            f"{n_symbols} symbols. Pooled IC here is a SMOKE TEST of the "
            f"machinery, not a tradeable edge. Sharpens as nightly snapshots "
            f"accumulate."
        )

    return ResearchReport(
        n_snapshots=int(len(df)), n_labelled=n_labelled, n_symbols=n_symbols,
        n_dates=n_dates, features=feats, ic_table=ic_table, alpha_decay=decay,
        warnings=warnings,
    )


def _fmt(x: float) -> str:
    return "  n/a" if x != x else f"{x:+.3f}"


def print_report(rep: ResearchReport) -> None:
    line = "=" * 78
    print(line)
    print("FEATURE RESEARCH  —  information coefficient + alpha-decay profiler")
    print(line)
    print(f"snapshots          : {rep.n_snapshots}")
    print(f"symbols / dates    : {rep.n_symbols} symbols over {rep.n_dates} snapshot date(s)")
    print(f"labelled rows      : " + ", ".join(f"{h}={n}" for h, n in rep.n_labelled.items()))
    print()

    if rep.warnings:
        print("WARNINGS")
        for w in rep.warnings:
            print(f"  ! {w}")
        print()

    if rep.ic_table:
        print("RANK IC  (Spearman feature vs forward return; higher |IC| = more predictive)")
        print(f"  {'feature':24s} {'IC@5d':>8s} {'IC@20d':>8s} {'IC@60d':>8s}  {'peak':>5s}  decay")
        # Sort by best absolute IC at any horizon, most predictive first.
        def _best(feat: str) -> float:
            vals = [rep.ic_table[feat][h]["ic"] for h in HORIZONS]
            vals = [abs(v) for v in vals if v == v]
            return max(vals) if vals else -1.0
        for feat in sorted(rep.ic_table, key=_best, reverse=True):
            by_h = rep.ic_table[feat]
            dec = rep.alpha_decay.get(feat, {})
            peak = dec.get("peak_horizon")
            print(f"  {feat:24s} "
                  f"{_fmt(by_h[5]['ic']):>8s} {_fmt(by_h[20]['ic']):>8s} {_fmt(by_h[60]['ic']):>8s}  "
                  f"{(str(peak)+'d' if peak else '  -'):>5s}  {dec.get('verdict','')}")
        print()

    print("READING IT: 'fast' features peak at the short horizon — act immediately;")
    print("'slow' features peak at 60d — positional. This is the alpha-decay answer")
    print("to 'timely decisions'. PROMOTION GATE: research only — no signal moves to")
    print("a live entry/exit gate until its IC is credible on a larger sample.")
    print(line)


def main(argv: Optional[Iterable[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    try:  # pragma: no cover - environment dependent
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="path to sqlite db (default: from settings)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(list(argv) if argv is not None else None)

    rep = run_research(args.db)
    if args.json:
        from dataclasses import asdict
        print(json.dumps(asdict(rep), default=str, indent=2))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
