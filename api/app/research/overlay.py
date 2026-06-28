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
}

SIGNAL_LABELS: dict[str, str] = {
    "z_theme_centrality":          "chokepoint centrality",
    "z_smartmoney_theme_confirm":  "smart-money confirmation",
    "z_dark_pool_notional":        "dark-pool accumulation",
    "z_momentum_60d":              "60d momentum",
    "z_flow_imbalance":            "options flow tilt",
    "z_momentum_20d":              "20d momentum",
    "persona_aschenbrenner":       "buildout-thesis (Aschenbrenner) lens",
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


def quant_edge(features: Mapping[str, Any], weights: Mapping[str, float]) -> dict[str, Any]:
    """0..100 edge for one symbol from its feature dict + signal weights.

    Weighted average of the present, normalised signals through a logistic so a
    universe-average name scores ~50. Returns score, label, and per-signal
    contributions (sorted, for the LLM block).
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
    score = round(100.0 / (1.0 + math.exp(-(num / wabs))), 1)
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
                 source: str) -> str:
    """The compact QUANT SIGNALS text injected into the scorer's prompt."""
    edge = quant_edge(features, weights)
    lines = [f"QUANT EDGE: {edge['label']} ({edge['score']}/100)"]
    # Top contributing signals in plain language.
    for feat, contrib in edge["contributions"][:5]:
        raw = features.get(feat)
        if raw is None:
            continue
        direction = "+" if contrib >= 0 else "-"
        lines.append(f"  {direction} {SIGNAL_LABELS.get(feat, feat)}: "
                     f"{raw:.2f} (contribution {contrib:+.2f})")
    conf = ("weights are theory-priors (no forward-return history yet - weigh "
            "modestly)" if source == "prior" else
            "weights are auto-calibrated to measured forward-return predictiveness")
    lines.append(f"  [{conf}]")
    return "\n".join(lines)


async def build_extra_context(symbols: list[str]) -> dict[str, str]:
    """{ticker: QUANT-SIGNALS block} for the scorer, from each symbol's latest
    feature snapshot. Symbols without a snapshot are omitted (the scorer simply
    gets no quant block for them)."""
    from sqlalchemy import select
    from ..db import SymbolFeatureSnapshot, get_session as db_session

    wrec = load_weights()
    weights, source = wrec["weights"], wrec.get("source", "prior")
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
        out[r.symbol] = format_block(r.symbol, r.features or {}, weights, source)
    return out
