"""Chart-pattern recognition — the eight setups from the technical spec.

    Cup & Handle · Flat Base · VCP · Ascending Triangle ·
    Wyckoff Accumulation · Wyckoff Spring · Stage 2 breakout · Failed breakout

Deterministic heuristics over daily OHLCV (oldest first). Each detector is
deliberately CONSERVATIVE — a pattern only fires when the classic textbook
shape is clearly present, because a false "Cup & Handle" chip is worse than
a missed one.

These are LABELS, not signals: decision-support on the dashboard and a
``pattern_bull_count`` column in the point-in-time feature store so the IC
harness can measure whether detected patterns actually predict forward
returns. No gate or score reads them until that validation says they earn it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("agentic_edge.research")

_MIN_BARS = 120


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _slope_norm(vals: list[float]) -> float:
    """Least-squares slope normalised by the mean level — per-bar fractional
    drift, so thresholds mean the same thing for a $5 and a $500 stock."""
    n = len(vals)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2.0
    my = _mean(vals)
    num = sum((i - mx) * (v - my) for i, v in enumerate(vals))
    den = sum((i - mx) ** 2 for i in range(n))
    if den == 0 or my == 0:
        return 0.0
    return (num / den) / abs(my)


def _swing_points(highs: list[float], lows: list[float], k: int = 4) -> tuple[list[int], list[int]]:
    """Indices of local swing highs/lows: the bar is the extreme of ±k bars."""
    sh: list[int] = []
    sl: list[int] = []
    for i in range(k, len(highs) - k):
        window_h = highs[i - k:i + k + 1]
        window_l = lows[i - k:i + k + 1]
        if highs[i] >= max(window_h):
            sh.append(i)
        if lows[i] <= min(window_l):
            sl.append(i)
    return sh, sl


def _pat(key: str, name: str, direction: str, confidence: str, detail: str) -> dict[str, Any]:
    return {"key": key, "name": name, "direction": direction,
            "confidence": confidence, "detail": detail}


# ---------------------------------------------------------------------------
# Detectors — each returns a pattern dict or None
# ---------------------------------------------------------------------------


def _stage2_breakout(highs, lows, closes, volumes) -> Optional[dict[str, Any]]:
    """Weinstein Stage 2: price above a RISING long-term average and breaking
    to new medium-term highs, ideally on expanding volume."""
    n = len(closes)
    if n < 220:
        return None
    ma200_now = _mean(closes[-200:])
    ma200_prev = _mean(closes[-220:-20])
    ma50 = _mean(closes[-50:])
    if not (ma200_now > ma200_prev and closes[-1] > ma200_now and closes[-1] > ma50):
        return None
    prior_high = max(highs[-145:-15])
    recent_high = max(highs[-15:])
    if not (recent_high > prior_high * 1.005 and closes[-1] > prior_high * 0.98):
        return None
    vol_expanding = (_mean(volumes[-5:]) > 1.3 * _mean(volumes[-40:-5])
                     if _mean(volumes[-40:-5]) > 0 else False)
    return _pat(
        "stage2_breakout", "Stage 2 breakout", "bullish",
        "high" if vol_expanding else "medium",
        f"new ~6-month high above a rising 200-day average"
        + (" on expanding volume" if vol_expanding else " (volume not yet confirming)"),
    )


def _failed_breakout(highs, lows, closes, volumes) -> Optional[dict[str, Any]]:
    """Broke above prior resistance within the last ~3 weeks, then fell back
    below it — the classic bull trap."""
    n = len(closes)
    if n < 90:
        return None
    resistance = max(highs[-75:-15])
    broke_out = any(c > resistance * 1.01 for c in closes[-15:-1])
    fell_back = closes[-1] < resistance * 0.99
    if broke_out and fell_back:
        return _pat(
            "failed_breakout", "Failed breakout", "bearish", "high",
            "cleared prior resistance in the last 3 weeks, then closed back below it",
        )
    return None


def _flat_base(highs, lows, closes, volumes) -> Optional[dict[str, Any]]:
    """Tight sideways range near the highs after a meaningful advance."""
    n = len(closes)
    if n < _MIN_BARS:
        return None
    w = 30
    hh = max(highs[-w:])
    ll = min(lows[-w:])
    if hh <= 0 or (hh - ll) / hh > 0.15:
        return None
    year_high = max(highs[-min(n, 250):])
    if hh < 0.90 * year_high:
        return None
    base_start = closes[-w]
    lookback = closes[-min(n, 130)]
    if lookback <= 0 or base_start / lookback - 1.0 < 0.25:
        return None
    return _pat(
        "flat_base", "Flat base", "bullish", "medium",
        f"~{w}-day tight range (≤15%) near the highs after a 25%+ advance",
    )


def _vcp(highs, lows, closes, volumes) -> Optional[dict[str, Any]]:
    """Volatility Contraction Pattern: successive pullbacks each SHALLOWER
    than the last, volume drying up, price holding near the base high."""
    n = len(closes)
    if n < _MIN_BARS:
        return None
    win_h, win_l = highs[-120:], lows[-120:]
    sh, _sl = _swing_points(win_h, win_l, k=4)
    if len(sh) < 3:
        return None
    # Depth of the correction after each of the last 3 swing highs.
    depths: list[float] = []
    for a, b in zip(sh[-3:], sh[-2:] + [len(win_h) - 1]):
        seg_low = min(win_l[a:b + 1])
        if win_h[a] > 0:
            depths.append(1.0 - seg_low / win_h[a])
    if len(depths) < 3:
        return None
    contracting = depths[0] > depths[1] > depths[2] and depths[2] <= 0.12
    vol_drying = _slope_norm(volumes[-40:]) < 0
    near_high = closes[-1] >= 0.90 * max(win_h)
    if contracting and near_high:
        return _pat(
            "vcp", "VCP (volatility contraction)", "bullish",
            "high" if vol_drying else "medium",
            f"pullbacks tightening {depths[0]*100:.0f}% → {depths[1]*100:.0f}% → "
            f"{depths[2]*100:.0f}%" + (", volume drying up" if vol_drying else ""),
        )
    return None


def _cup_and_handle(highs, lows, closes, volumes) -> Optional[dict[str, Any]]:
    """Rounded 12-35% base back to the rim, then a small handle pullback in
    the upper half of the cup."""
    n = len(closes)
    if n < 160:
        return None
    # Left rim: the high 60-160 bars ago.
    rim_window = highs[-160:-60]
    rim = max(rim_window)
    rim_idx = n - 160 + rim_window.index(rim)
    cup_lows = lows[rim_idx:n - 15]
    if not cup_lows or rim <= 0:
        return None
    cup_low = min(cup_lows)
    depth = 1.0 - cup_low / rim
    if not (0.12 <= depth <= 0.35):
        return None
    recovery_high = max(highs[-20:])
    if recovery_high < 0.95 * rim:
        return None
    handle_pullback = 1.0 - closes[-1] / recovery_high
    cup_mid = cup_low + 0.5 * (rim - cup_low)
    handle_holds = min(lows[-10:]) > cup_mid
    if 0.0 <= handle_pullback <= 0.12 and handle_holds:
        return _pat(
            "cup_handle", "Cup & Handle", "bullish", "medium",
            f"{depth*100:.0f}%-deep rounded base recovered to the rim, "
            f"handle pulling back {handle_pullback*100:.0f}% in the upper half",
        )
    return None


def _ascending_triangle(highs, lows, closes, volumes) -> Optional[dict[str, Any]]:
    """Flat resistance tested 2+ times with rising swing lows pressing into it."""
    n = len(closes)
    if n < _MIN_BARS:
        return None
    w = 55
    win_h, win_l = highs[-w:], lows[-w:]
    sh, sl = _swing_points(win_h, win_l, k=3)
    if len(sh) < 2 or len(sl) < 2:
        return None
    top = max(win_h)
    flat_tests = [i for i in sh if win_h[i] >= 0.97 * top]
    if len(flat_tests) < 2:
        return None
    lows_vals = [win_l[i] for i in sl]
    rising = all(b > a * 1.005 for a, b in zip(lows_vals, lows_vals[1:]))
    near_top = closes[-1] >= 0.95 * top
    if rising and near_top:
        return _pat(
            "ascending_triangle", "Ascending triangle", "bullish", "medium",
            f"flat ceiling tested {len(flat_tests)}× with buyers stepping up at higher lows",
        )
    return None


def _wyckoff_accumulation(highs, lows, closes, volumes) -> Optional[dict[str, Any]]:
    """Sideways range after a markdown, with volume favouring the up-days —
    the composite operator absorbing supply."""
    n = len(closes)
    if n < 160:
        return None
    w = 45
    hh = max(highs[-w:])
    ll = min(lows[-w:])
    if hh <= 0 or (hh - ll) / hh > 0.18:
        return None
    prior_peak = max(closes[-160:-w])
    if prior_peak <= 0 or closes[-w] > 0.80 * prior_peak:
        return None  # no markdown before the range → not accumulation
    up_vol = sum(v for c0, c1, v in zip(closes[-w:-1], closes[-w + 1:], volumes[-w + 1:]) if c1 > c0)
    down_vol = sum(v for c0, c1, v in zip(closes[-w:-1], closes[-w + 1:], volumes[-w + 1:]) if c1 < c0)
    if down_vol <= 0 or up_vol < 1.15 * down_vol:
        return None
    return _pat(
        "wyckoff_accumulation", "Wyckoff accumulation", "bullish", "medium",
        f"sideways range after a {100 * (1 - closes[-w] / prior_peak):.0f}% markdown "
        "with up-day volume dominating (absorption)",
    )


def _wyckoff_spring(highs, lows, closes, volumes) -> Optional[dict[str, Any]]:
    """A brief undercut of range support that snaps back — the shakeout that
    often precedes markup."""
    n = len(closes)
    if n < 90:
        return None
    support = min(lows[-40:-8])
    recent_low = min(lows[-8:])
    undercut = support * 0.94 <= recent_low < support * 0.995
    recovered = closes[-1] > support * 1.005
    if undercut and recovered:
        undercut_idx = lows[-8:].index(recent_low)
        vol_spike = (volumes[-8:][undercut_idx] > 1.5 * _mean(volumes[-40:-8])
                     if _mean(volumes[-40:-8]) > 0 else False)
        return _pat(
            "wyckoff_spring", "Wyckoff spring", "bullish",
            "high" if vol_spike else "medium",
            "briefly undercut range support then snapped back above it"
            + (" on a volume spike (shakeout)" if vol_spike else ""),
        )
    return None


_DETECTORS = (
    _stage2_breakout,
    _vcp,
    _cup_and_handle,
    _flat_base,
    _ascending_triangle,
    _wyckoff_accumulation,
    _wyckoff_spring,
    _failed_breakout,
)


def detect_patterns(
    *, highs: list[float], lows: list[float],
    closes: list[float], volumes: list[float],
) -> list[dict[str, Any]]:
    """Run every detector over daily OHLCV (oldest first). Returns the
    patterns that fired, bullish first. Series shorter than each detector's
    requirement simply skip that detector."""
    series = [highs, lows, closes, volumes]
    n = min(len(s) for s in series)
    if n < 90:
        return []
    highs, lows, closes, volumes = (list(map(float, s[-n:])) for s in series)
    out: list[dict[str, Any]] = []
    for det in _DETECTORS:
        try:
            hit = det(highs, lows, closes, volumes)
            if hit:
                out.append(hit)
        except Exception as e:  # a single detector bug must not kill the sweep
            logger.debug("patterns: %s failed: %s", det.__name__, e)
    out.sort(key=lambda p: (p["direction"] != "bullish", p["name"]))
    return out


def bull_pattern_count(patterns: list[dict[str, Any]]) -> int:
    """Feature-store scalar: bullish patterns minus failed breakouts."""
    bulls = sum(1 for p in patterns if p["direction"] == "bullish")
    bears = sum(1 for p in patterns if p["direction"] == "bearish")
    return bulls - bears


# ---------------------------------------------------------------------------
# Market structure — HH/HL, accumulation vs distribution days, liquidity
# sweeps / stop hunts, supply & demand zones. Same deterministic OHLCV
# discipline as the pattern detectors: labels for the operator, and an
# ``accum_dist_balance`` scalar for the feature store.
# ---------------------------------------------------------------------------


def market_structure(
    *, highs: list[float], lows: list[float],
    closes: list[float], volumes: list[float],
) -> Optional[dict[str, Any]]:
    series = [highs, lows, closes, volumes]
    n = min(len(s) for s in series)
    if n < 90:
        return None
    highs, lows, closes, volumes = (list(map(float, s[-n:])) for s in series)
    price = closes[-1]

    # --- Higher highs / higher lows off swing points (last ~120 bars) ------
    win = min(n, 120)
    sh, sl = _swing_points(highs[-win:], lows[-win:], k=4)
    sh_vals = [highs[-win:][i] for i in sh][-3:]
    sl_vals = [lows[-win:][i] for i in sl][-3:]
    higher_highs = len(sh_vals) >= 2 and all(b > a for a, b in zip(sh_vals, sh_vals[1:]))
    higher_lows = len(sl_vals) >= 2 and all(b > a for a, b in zip(sl_vals, sl_vals[1:]))
    lower_highs = len(sh_vals) >= 2 and all(b < a for a, b in zip(sh_vals, sh_vals[1:]))
    lower_lows = len(sl_vals) >= 2 and all(b < a for a, b in zip(sl_vals, sl_vals[1:]))
    if higher_highs and higher_lows:
        structure = "uptrend"
    elif lower_highs and lower_lows:
        structure = "downtrend"
    else:
        structure = "range"

    # --- Accumulation vs distribution days (last 25 sessions, IBD-style) ---
    accum = distrib = 0
    for c0, c1, v0, v1 in zip(closes[-26:-1], closes[-25:], volumes[-26:-1], volumes[-25:]):
        if c0 <= 0 or v1 <= v0:
            continue
        chg = c1 / c0 - 1.0
        if chg >= 0.002:
            accum += 1
        elif chg <= -0.002:
            distrib += 1

    # --- Liquidity sweep / stop hunt (last 8 bars) --------------------------
    sweep: Optional[dict[str, Any]] = None
    support = min(lows[-40:-8])
    resistance = max(highs[-40:-8])
    for i in range(n - 8, n):
        if lows[i] < support * 0.997 and closes[i] > support:
            sweep = {"side": "lows", "detail":
                     "price dipped under a well-watched support level and closed back "
                     "above it — stops below were taken out (bullish stop hunt)"}
        elif highs[i] > resistance * 1.003 and closes[i] < resistance:
            sweep = {"side": "highs", "detail":
                     "price poked above a well-watched high and closed back under it — "
                     "buyers' stops were swept (bearish stop hunt)"}

    # --- Supply / demand zones ----------------------------------------------
    supply = _nearest_zone(highs, lows, closes, kind="supply", price=price)
    demand = _nearest_zone(highs, lows, closes, kind="demand", price=price)

    return {
        "structure": structure,
        "higher_highs": higher_highs,
        "higher_lows": higher_lows,
        "accumulation_days_25": accum,
        "distribution_days_25": distrib,
        # 4+ distribution days in 25 sessions is the classic institutional-
        # selling tell; flag it only when sellers also outnumber buyers.
        "distribution_flag": distrib >= 4 and distrib > accum,
        "liquidity_sweep": sweep,
        "supply_zone": supply,
        "demand_zone": demand,
    }


def _nearest_zone(
    highs: list[float], lows: list[float], closes: list[float],
    *, kind: str, price: float,
) -> Optional[dict[str, Any]]:
    """Nearest untested supply zone above price (a swing high a ≥5% drop
    launched from, never reclaimed) or demand zone below price (a swing low a
    ≥5% rally launched from, never broken)."""
    n = len(closes)
    win = min(n, 200)
    sh, sl = _swing_points(highs[-win:], lows[-win:], k=4)
    base = n - win
    best: Optional[dict[str, Any]] = None
    if kind == "supply":
        for i in sh:
            gi = base + i
            zone_hi = highs[gi]
            zone_lo = min(lows[max(gi - 1, 0):gi + 2])
            after = closes[gi:min(gi + 16, n)]
            if not after or min(after) > zone_hi * 0.95:
                continue                    # no ≥5% rejection — not supply
            if any(c > zone_hi for c in closes[gi + 1:]):
                continue                    # reclaimed since — zone spent
            if zone_lo <= price:
                continue                    # not above us
            dist = zone_lo / price - 1.0
            if best is None or dist < best["dist_pct"]:
                best = {"low": round(zone_lo, 2), "high": round(zone_hi, 2),
                        "dist_pct": round(dist, 4)}
    else:
        for i in sl:
            gi = base + i
            zone_lo = lows[gi]
            zone_hi = max(highs[max(gi - 1, 0):gi + 2])
            after = closes[gi:min(gi + 16, n)]
            if not after or max(after) < zone_lo * 1.05:
                continue                    # no ≥5% launch — not demand
            if any(c < zone_lo for c in closes[gi + 1:]):
                continue                    # broken since — zone spent
            if zone_hi >= price:
                continue                    # not below us
            dist = zone_hi / price - 1.0
            if best is None or dist > best["dist_pct"]:
                best = {"low": round(zone_lo, 2), "high": round(zone_hi, 2),
                        "dist_pct": round(dist, 4)}
    return best
