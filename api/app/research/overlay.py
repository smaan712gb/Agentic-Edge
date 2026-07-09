"""Quant overlay — autonomously folds the research factory into the decision.

This is the wiring that makes the Quant Research Factory *improve trades* rather
than sit beside them. It is fully autonomous: no human approves anything. The
overlay distils each symbol's point-in-time features into a compact signal block
that is injected into the scorecard's per-ticker LLM context, so the AI's own
Buy/Hold/Avoid + conviction (which then drive entry ranking AND sizing) factor in
centrality, smart-money, dark-pool accumulation, momentum, flow, and the manager
personas.

SELF-VALIDATING WEIGHTS (the key to "autonomous, not reckless"):
Each signal's weight is a Bayesian blend of a theory PRIOR and its MEASURED
information coefficient (IC) from feature_research.py:

    w = prior * k/(k+n)  +  ic_weight * n/(k+n)

where n = labelled rows behind that feature's IC and k = prior strength. At cold
start (n=0) the weight is the theory prior, so the overlay is useful from day
one; as forward-return labels accrue, weight shifts to what actually predicts
returns in THIS book. A daily recalibration job recomputes the weights with no
human in the loop — the IC harness is the arbiter, not a person.

Pure cores (`quant_edge`, `blended_weights`) are unit-tested. Reading the latest
snapshot + formatting the block is the only I/O.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger("agentic_edge.research")

# Signals the overlay scores on, with a theory prior and a human label. Prior
# magnitudes encode conviction in the *mechanism* before any data: chokepoint
# centrality and smart-money confirmation are the strongest structural priors;
# personas are weak until their IC is proven.
PRIOR_WEIGHTS: dict[str, float] = {
    "z_theme_centrality":          1.0,
    "z_smartmoney_theme_confirm":  0.8,
    "z_dark_pool_notional":        0.5,
    "z_momentum_60d":              0.4,
    "z_flow_imbalance":            0.4,
    "z_momentum_20d":              0.3,
    "persona_aschenbrenner":       0.3,
    # Technical engines (2026-07-09). Deliberately small priors: blended_weights
    # only scores features present HERE, so absence meant the TA engines
    # contributed zero to quant edge no matter what their IC said. Small prior
    # = they participate immediately but IC evidence decides their real weight.
    "z_ema_stack_score":           0.3,
    "z_pattern_bull_count":        0.2,
    "z_accum_dist_balance":        0.2,
    "z_cba_confidence_num":        0.2,
}

SIGNAL_LABELS: dict[str, str] = {
    "z_theme_centrality":          "chokepoint centrality",
    "z_smartmoney_theme_confirm":  "smart-money confirmation",
    "z_dark_pool_notional":        "dark-pool accumulation",
    "z_momentum_60d":              "60d momentum",
    "z_flow_imbalance":            "options flow tilt",
    "z_momentum_20d":              "20d momentum",
    "persona_aschenbrenner":       "buildout-thesis (Aschenbrenner) lens",
    "z_ema_stack_score":           "EMA trend-stack alignment",
    "z_pattern_bull_count":        "bullish chart patterns",
    "z_accum_dist_balance":        "accumulation vs distribution days",
    "z_cba_confidence_num":        "closing-bell accumulation",
}

# Bayesian shrinkage strength: ~k labelled rows of IC evidence to move a weight
# halfway off its prior. Higher = trust the prior longer.
SHRINKAGE_K = 60.0
# Maps a raw IC (Spearman, ~[-0.3, 0.3]) onto the prior weight scale (~[0, 1]).
IC_TO_WEIGHT = 3.0
PRIMARY_HORIZON = 20


# ---------------------------------------------------------------------------
# Pure cores (unit-tested)
# ---------------------------------------------------------------------------


def _normalize(feat: str, value: float) -> float:
    """Put a feature on a roughly-unit z scale. persona_* are 0..100 → centre
    on 50, scale by 25 so a top-decile persona reads ~+2."""
    if feat.startswith("persona_"):
        return (value - 50.0) / 25.0
    return value


def blended_weights(
    prior: Mapping[str, float], ic: Mapping[str, float], n_by_feat: Mapping[str, int],
    k: float = SHRINKAGE_K,
) -> dict[str, float]:
    """Per-feature Bayesian blend of prior and measured-IC weight.

    ic[feat] is the signed Spearman IC at the primary horizon (NaN/missing →
    treated as no evidence). n_by_feat[feat] is the labelled-row count behind it.
    """
    out: dict[str, float] = {}
    for feat, p in prior.items():
        ic_v = ic.get(feat, float("nan"))
        n = float(n_by_feat.get(feat, 0) or 0)
        if ic_v != ic_v or n <= 0:      # NaN or no labels → pure prior
            out[feat] = p
            continue
        ic_weight = max(0.0, ic_v * IC_TO_WEIGHT)   # only reward positive predictive power
        shrink = n / (k + n)
        out[feat] = round(p * (1.0 - shrink) + ic_weight * shrink, 4)
    return out


def effective_weights(
    weights: Mapping[str, float], universe_rows: list[Mapping[str, Any]],
) -> dict[str, float]:
    """Drop weights for signals carrying NO information across the universe.

    A signal whose value is constant (zero variance) over every symbol — e.g.
    ``z_dark_pool_notional`` / ``z_flow_imbalance`` when the UW feed is empty —
    contributes nothing to any name's edge yet still sits in the denominator,
    diluting the live signals toward neutral. We drop such dead signals so the
    surviving weights actually shape the edge. Falls back to the full set if
    everything looks dead (defensive — centrality always varies on a real
    multi-theme universe)."""
    active: dict[str, float] = {}
    for feat, w in weights.items():
        vals = [r.get(feat) for r in universe_rows
                if isinstance(r.get(feat), (int, float)) and not isinstance(r.get(feat), bool)]
        if len(vals) >= 2 and (max(vals) - min(vals)) > 1e-9:
            active[feat] = w
    return active or dict(weights)


def quant_edge(features: Mapping[str, Any], weights: Mapping[str, float],
               shrink: float = 1.0) -> dict[str, Any]:
    """0..100 edge for one symbol from its feature dict + signal weights.

    Weighted average of the present, normalised signals through a logistic so a
    universe-average name scores ~50. ``shrink`` (0..1) scales the z-advantage
    before the logistic — < 1 pulls the edge toward 50, used to down-weight the
    cold-start prior. Returns score, label, and per-signal contributions.
    """
    num = 0.0
    wabs = 0.0
    contribs: list[tuple[str, float]] = []
    for feat, w in weights.items():
        v = features.get(feat)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
            continue
        nv = _normalize(feat, float(v))
        num += w * nv
        wabs += abs(w)
        contribs.append((feat, round(w * nv, 3)))
    if wabs == 0.0:
        return {"score": 50.0, "label": "NEUTRAL", "contributions": []}
    score = round(100.0 / (1.0 + math.exp(-(num / wabs) * shrink)), 1)
    label = "STRONG" if score >= 64 else ("WEAK" if score <= 36 else "NEUTRAL")
    contribs.sort(key=lambda kv: abs(kv[1]), reverse=True)
    return {"score": score, "label": label, "contributions": contribs}


# ---------------------------------------------------------------------------
# Weight persistence + autonomous recalibration
# ---------------------------------------------------------------------------


def _weights_path() -> Path:
    from .feature_research import resolve_db_path
    return resolve_db_path().parent / "overlay_weights.json"


def load_weights() -> dict[str, Any]:
    """Persisted IC-blended weights, or the theory prior if none yet.

    Returns {weights, source, calibrated_at, n_labelled}. ``source`` is
    'prior' (cold start) or 'ic_blended' (self-tuned)."""
    path = _weights_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data.get("weights"), dict):
                return data
        except Exception as e:  # pragma: no cover - corrupt file falls back to prior
            logger.warning("overlay: weights file unreadable (%s) — using prior", e)
    return {"weights": dict(PRIOR_WEIGHTS), "source": "prior",
            "calibrated_at": None, "n_labelled": 0}


def recalibrate_weights(db_path: Optional[str] = None) -> dict[str, Any]:
    """Run the IC harness, blend each signal's weight off its prior by measured
    predictive power, and persist. Fully autonomous (the scheduler calls it).

    Returns the new weights record. At cold start (no labels) this writes the
    prior with source='prior' — a harmless no-op that becomes self-tuning the
    moment forward returns exist.
    """
    from .feature_research import run_research
    rep = run_research(db_path)
    ic = {feat: by_h.get(PRIMARY_HORIZON, {}).get("ic", float("nan"))
          for feat, by_h in rep.ic_table.items()}
    n_by_feat = {feat: by_h.get(PRIMARY_HORIZON, {}).get("n", 0)
                 for feat, by_h in rep.ic_table.items()}
    weights = blended_weights(PRIOR_WEIGHTS, ic, n_by_feat)
    n_labelled = rep.n_labelled.get(f"{PRIMARY_HORIZON}d", 0)
    source = "ic_blended" if n_labelled > 0 else "prior"
    record = {"weights": weights, "source": source,
              "calibrated_at": None, "n_labelled": n_labelled,
              "ic_primary_horizon": PRIMARY_HORIZON}
    try:
        _weights_path().write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception as e:  # pragma: no cover
        logger.warning("overlay: could not persist weights (%s)", e)
    logger.info("overlay: recalibrated weights source=%s n_labelled=%d", source, n_labelled)
    return record


# ---------------------------------------------------------------------------
# Scorecard injection (the autonomous wiring point)
# ---------------------------------------------------------------------------


def format_block(symbol: str, features: Mapping[str, Any], weights: Mapping[str, float],
                 source: str, shrink: float = 1.0) -> str:
    """The compact QUANT SIGNALS text injected into the scorer's prompt."""
    edge = quant_edge(features, weights, shrink=shrink)
    lines = [f"QUANT EDGE: {edge['label']} ({edge['score']}/100)"]
    # Top contributing signals in plain language.
    for feat, contrib in edge["contributions"][:5]:
        raw = features.get(feat)
        if raw is None:
            continue
        direction = "+" if contrib >= 0 else "-"
        lines.append(f"  {direction} {SIGNAL_LABELS.get(feat, feat)}: "
                     f"{raw:.2f} (contribution {contrib:+.2f})")
    if source == "prior":
        conf = (f"weights are theory-priors (no forward-return history yet); "
                f"influence shrunk to {shrink:.0%} until measured IC exists")
    else:
        conf = "weights are auto-calibrated to measured forward-return predictiveness"
    lines.append(f"  [{conf}]")
    return "\n".join(lines)


# Cache the de-diluted, shrink-adjusted weights per (snapshot-day, source) so the
# exit path doesn't reload the universe on every held position every tick.
_ACTIVE_WEIGHTS_CACHE: dict[tuple, tuple] = {}


async def _resolve_active_weights() -> tuple[dict[str, float], str, float]:
    """(effective_weights, source, shrink) for the current universe.

    Loads the latest snapshot day once (cached), drops dead/zero-variance
    signals, and computes the cold-start shrink (< 1 while the weights are still
    the un-calibrated prior). Shared by the entry block and the exit delta so
    both use one honest, de-diluted weight set."""
    from sqlalchemy import select
    from ..config import get_settings
    from ..db import SymbolFeatureSnapshot, get_session as db_session

    wrec = load_weights()
    weights, source = wrec["weights"], wrec.get("source", "prior")
    async with db_session() as s:
        day = (await s.execute(
            select(SymbolFeatureSnapshot.as_of)
            .order_by(SymbolFeatureSnapshot.as_of.desc()).limit(1)
        )).scalar()
        key = (str(day), source)
        if key in _ACTIVE_WEIGHTS_CACHE:
            return _ACTIVE_WEIGHTS_CACHE[key]
        rows: list[dict] = []
        if day is not None:
            rows = [r[0] or {} for r in (await s.execute(
                select(SymbolFeatureSnapshot.features)
                .where(SymbolFeatureSnapshot.as_of == day)
            )).all()]

    eff = effective_weights(weights, rows) if rows else dict(weights)
    shrink = float(get_settings().QUANT_PRIOR_SHRINK) if source == "prior" else 1.0
    result = (eff, source, shrink)
    _ACTIVE_WEIGHTS_CACHE[key] = result
    return result


def edge_to_exit_delta(edge: float, max_delta: float) -> float:
    """Map a 0-100 quant edge to a bounded, bidirectional exit-pressure delta.

    edge 100 (structurally strong) -> -max_delta (hold winners longer);
    edge 0   (weak)                -> +max_delta (lean toward trimming);
    edge 50  (neutral)             ->  0. Pure + testable.
    """
    return round((50.0 - edge) / 50.0 * max_delta, 2)


async def symbol_exit_delta(symbol: str) -> float:
    """Quant exit-pressure delta for one held symbol from its latest snapshot.

    0.0 when there's no snapshot or the overlay is unusable — so the exit
    decision degrades to its non-quant behaviour, never erroring."""
    try:
        from sqlalchemy import select
        from ..config import get_settings
        from ..db import SymbolFeatureSnapshot, get_session as db_session
        max_delta = float(getattr(get_settings(), "QUANT_EXIT_MAX_DELTA", 10.0))
        eff_weights, _source, shrink = await _resolve_active_weights()
        async with db_session() as s:
            row = (await s.execute(
                select(SymbolFeatureSnapshot)
                .where(SymbolFeatureSnapshot.symbol == symbol.strip().upper())
                .order_by(SymbolFeatureSnapshot.as_of.desc())
                .limit(1)
            )).scalar_one_or_none()
        if row is None:
            return 0.0
        edge = quant_edge(row.features or {}, eff_weights, shrink=shrink)["score"]
        return edge_to_exit_delta(edge, max_delta)
    except Exception as e:  # pragma: no cover - never break the exit path
        logger.debug("overlay: exit-delta failed for %s: %s", symbol, e)
        return 0.0


def edge_to_entry_factor(edge: float, max_tilt: float) -> float:
    """Map a 0-100 quant edge to a bounded, bidirectional entry SIZE multiplier.

    edge 100 (structurally strong) -> 1 + max_tilt (size the conviction buy up);
    edge 0   (weak)                -> 1 - max_tilt (size down);
    edge 50  (neutral)             -> 1.0. Clamped non-negative. The mirror of
    edge_to_exit_delta on the sizing axis."""
    return round(max(0.0, 1.0 + (edge - 50.0) / 50.0 * max_tilt), 3)


async def symbol_entry_factor(symbol: str) -> tuple[float, float]:
    """(size_factor, edge_score) for an entry candidate from its latest snapshot.

    Returns (1.0, 50.0) when there's no snapshot or the overlay is unusable — so
    entry sizing degrades to its non-quant behaviour, never erroring. The factor
    tilts position size; it is never a gate."""
    try:
        from sqlalchemy import select
        from ..config import get_settings
        from ..db import SymbolFeatureSnapshot, get_session as db_session
        if not get_settings().QUANT_OVERLAY_ENABLED:
            return 1.0, 50.0
        max_tilt = float(getattr(get_settings(), "QUANT_ENTRY_MAX_TILT", 0.15))
        eff_weights, _source, shrink = await _resolve_active_weights()
        async with db_session() as s:
            row = (await s.execute(
                select(SymbolFeatureSnapshot)
                .where(SymbolFeatureSnapshot.symbol == symbol.strip().upper())
                .order_by(SymbolFeatureSnapshot.as_of.desc())
                .limit(1)
            )).scalar_one_or_none()
        if row is None:
            return 1.0, 50.0
        edge = quant_edge(row.features or {}, eff_weights, shrink=shrink)["score"]
        return edge_to_entry_factor(edge, max_tilt), edge
    except Exception as e:  # pragma: no cover - never break the entry path
        logger.debug("overlay: entry-factor failed for %s: %s", symbol, e)
        return 1.0, 50.0


async def build_extra_context(symbols: list[str]) -> dict[str, str]:
    """{ticker: QUANT-SIGNALS block} for the scorer, from each symbol's latest
    feature snapshot. Symbols without a snapshot are omitted (the scorer simply
    gets no quant block for them)."""
    from sqlalchemy import select
    from ..db import SymbolFeatureSnapshot, get_session as db_session

    eff_weights, source, shrink = await _resolve_active_weights()
    wanted = {s.strip().upper() for s in symbols}

    async with db_session() as s:
        rows = (await s.execute(
            select(SymbolFeatureSnapshot)
            .order_by(SymbolFeatureSnapshot.symbol, SymbolFeatureSnapshot.as_of.desc())
        )).scalars().all()

    out: dict[str, str] = {}
    for r in rows:
        if r.symbol not in wanted or r.symbol in out:
            continue
        out[r.symbol] = format_block(r.symbol, r.features or {}, eff_weights, source, shrink)
    return out
