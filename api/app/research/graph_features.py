"""Universe-graph features — the 'one graph, not 17 themes' insight made numeric.

These are PURE functions over the theme→symbols membership map. No DB, no
network, no clock — so they are deterministic and unit-testable in isolation,
and they always produce signal even when every provider is down.

The membership matrix is itself the skeleton of the supply-chain graph: two
symbols that share a theme are adjacent nodes. From that we derive:

  * theme_count        — how many themes a name sits in. A name at 4 chokepoints
                         captures more of the buildout than a name at 1.
  * theme_centrality    — theme_count normalised to the busiest name (0..1).
  * co_membership_degree— how many DISTINCT other symbols share >=1 theme with
                         it (graph degree — how connected it is in the universe).
  * avg_theme_size      — mean size of the themes it belongs to (a name in only
                         tiny/concentrated themes scores differently from one in
                         broad themes).

These feed the feature store as the always-on, network-free feature family.
"""

from __future__ import annotations

from typing import Mapping


def build_membership(theme_to_symbols: Mapping[str, list[str]]) -> dict[str, set[str]]:
    """Invert {theme_id: [symbols]} into {symbol: {theme_ids}}.

    Symbols are upper-cased and de-spaced so 'msft' and 'MSFT' collapse. Empty
    / falsy symbols are dropped.
    """
    membership: dict[str, set[str]] = {}
    for theme_id, symbols in theme_to_symbols.items():
        for raw in symbols or []:
            sym = (raw or "").strip().upper()
            if not sym:
                continue
            membership.setdefault(sym, set()).add(theme_id)
    return membership


def theme_counts(membership: Mapping[str, set[str]]) -> dict[str, int]:
    """theme_count per symbol = chokepoint breadth."""
    return {sym: len(themes) for sym, themes in membership.items()}


def centrality(membership: Mapping[str, set[str]]) -> dict[str, float]:
    """theme_count normalised to the busiest symbol → 0..1 conviction proxy.

    Empty universe → empty. A universe where the max count is 1 maps every
    name to 1.0 (they are all equally central when nothing overlaps).
    """
    counts = theme_counts(membership)
    if not counts:
        return {}
    top = max(counts.values())
    if top <= 0:
        return {sym: 0.0 for sym in counts}
    return {sym: c / top for sym, c in counts.items()}


def co_membership_degree(membership: Mapping[str, set[str]]) -> dict[str, int]:
    """Graph degree: count of DISTINCT other symbols sharing >=1 theme.

    Built by expanding each theme into the symbols that carry it, then for each
    symbol unioning the co-members across all its themes (excluding itself).
    """
    # theme_id -> set of symbols in it (reconstructed from membership so we
    # depend on a single source of truth).
    theme_members: dict[str, set[str]] = {}
    for sym, themes in membership.items():
        for t in themes:
            theme_members.setdefault(t, set()).add(sym)

    degree: dict[str, int] = {}
    for sym, themes in membership.items():
        neighbours: set[str] = set()
        for t in themes:
            neighbours |= theme_members.get(t, set())
        neighbours.discard(sym)
        degree[sym] = len(neighbours)
    return degree


def avg_theme_size(membership: Mapping[str, set[str]]) -> dict[str, float]:
    """Mean number of symbols in the themes a name belongs to."""
    theme_size: dict[str, int] = {}
    for themes in membership.values():
        for t in themes:
            theme_size[t] = theme_size.get(t, 0) + 1

    out: dict[str, float] = {}
    for sym, themes in membership.items():
        if not themes:
            out[sym] = 0.0
            continue
        out[sym] = sum(theme_size[t] for t in themes) / len(themes)
    return out


def compute_graph_features(
    theme_to_symbols: Mapping[str, list[str]],
) -> dict[str, dict[str, object]]:
    """Per-symbol graph feature dict for the whole universe.

    Returns {symbol: {theme_count, theme_centrality, co_membership_degree,
                      avg_theme_size, themes}}.
    """
    membership = build_membership(theme_to_symbols)
    counts = theme_counts(membership)
    cent = centrality(membership)
    degree = co_membership_degree(membership)
    sizes = avg_theme_size(membership)

    out: dict[str, dict[str, object]] = {}
    for sym, themes in membership.items():
        out[sym] = {
            "theme_count": counts[sym],
            "theme_centrality": round(cent[sym], 4),
            "co_membership_degree": degree[sym],
            "avg_theme_size": round(sizes[sym], 3),
            "themes": sorted(themes),
        }
    return out
