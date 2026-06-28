"""Manager-archetype personas — score a symbol through a famous investor's lens.

NOT filing-tracking (that's the Hedge Fund Signal Tracker). This is *playbook
emulation*: each archetype is a weighting over the same cross-sectional z-scored
features the factory already computes, encoding HOW that investor picks. A name
that lights up the Aschenbrenner lens is one his published thesis (the AI
compute / power / semiconductor buildout, concentrated, early) would favour —
regardless of whether his (sparsely-filing) fund actually holds it.

Pure + deterministic: ``score_symbol`` is a function of a single symbol's
feature dict, so it's unit-testable and carries no lookahead. The persona
scores are folded into the feature snapshot as their own feature family
(``persona_*``) by features.py.

Archetypes (each a public, well-documented style):
  * aschenbrenner — Situational Awareness: AI compute/power/semis buildout,
    concentrated, early, holds through volatility. Rewards chokepoint
    centrality + momentum + smart-money confirmation.
  * growth_momentum — Coatue/Tiger style: ride the strongest flow + momentum.
  * activist_value  — Elliott/Starboard style: dislocation + a stake catalyst
    (below-MA + smart-money present).
  * macro_thematic  — Druckenmiller style: broad thematic breadth + positive
    options/gamma positioning, less single-name idiosyncrasy.
"""

from __future__ import annotations

from typing import Any, Mapping


# Each persona is a weighting over z_* (and a couple of raw) features. Positive
# weight = the archetype likes a high value; negative = likes a low value. The
# raw score is a weighted sum of available z-features; missing features are
# skipped and the weights renormalised over what's present, so a name with thin
# market/flow data still gets a graph-driven score.
PERSONAS: dict[str, dict[str, Any]] = {
    "aschenbrenner": {
        "label": "Aschenbrenner (compute buildout)",
        "description": "Concentrated, early, thesis-driven on the AI compute/power/"
                       "semiconductor buildout. Favours names at the chokepoints.",
        "weights": {
            "z_theme_centrality": 1.4,
            "z_co_membership_degree": 1.0,
            "z_smartmoney_theme_confirm": 0.8,
            "z_momentum_60d": 0.6,
            "z_dark_pool_notional": 0.4,
        },
    },
    "growth_momentum": {
        "label": "Growth momentum (Coatue/Tiger)",
        "description": "Rides the strongest flow and price momentum.",
        "weights": {
            "z_momentum_20d": 1.2,
            "z_momentum_60d": 0.8,
            "z_flow_imbalance": 1.0,
            "z_rvol": 0.5,
            "z_gamma_sign_num": 0.4,
        },
    },
    "activist_value": {
        "label": "Activist value (Elliott/Starboard)",
        "description": "Dislocation plus a stake catalyst — below trend with "
                       "smart money already present.",
        "weights": {
            "z_dist_50dma": -1.0,            # likes names BELOW their 50dma
            "z_smartmoney_theme_confirm": 1.2,
            "z_momentum_60d": -0.4,
            "z_theme_centrality": 0.6,
        },
    },
    "macro_thematic": {
        "label": "Macro thematic (Druckenmiller)",
        "description": "Broad thematic breadth and positive positioning over "
                       "single-name idiosyncrasy.",
        "weights": {
            "z_theme_count": 1.0,
            "z_avg_theme_size": 0.6,
            "z_gamma_sign_num": 0.8,
            "z_flow_imbalance": 0.6,
        },
    },
}


def _squash(x: float) -> float:
    """Map a z-scale weighted sum to a 0..100 score via a logistic curve.

    A weighted sum of ~0 (universe-average name) → 50. Each unit of net
    z-advantage moves it ~20 points, saturating smoothly at the tails.
    """
    import math
    return round(100.0 / (1.0 + math.exp(-x)), 1)


def score_symbol(features: Mapping[str, Any], persona: str) -> float:
    """0..100 archetype score for one symbol's feature dict.

    Renormalises weights over the features actually present (non-null numeric),
    so a name missing flow/market data is scored on its graph features alone
    rather than penalised to the floor. Returns 50.0 (neutral) when no weighted
    feature is available.
    """
    spec = PERSONAS.get(persona)
    if spec is None:
        raise KeyError(f"unknown persona: {persona}")
    weights: dict[str, float] = spec["weights"]

    num = 0.0
    wabs = 0.0
    for feat, w in weights.items():
        v = features.get(feat)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        num += w * float(v)
        wabs += abs(w)
    if wabs == 0.0:
        return 50.0
    # Normalise by total |weight| so the squash input is on a comparable z-scale
    # whether or not every feature was present.
    return _squash(num / wabs)


def score_all(features: Mapping[str, Any]) -> dict[str, float]:
    """{persona: score} across every archetype for one symbol."""
    return {name: score_symbol(features, name) for name in PERSONAS}


def persona_meta() -> list[dict[str, str]]:
    """Archetype identity for the UI (label + description)."""
    return [{"key": k, "label": v["label"], "description": v["description"]}
            for k, v in PERSONAS.items()]
