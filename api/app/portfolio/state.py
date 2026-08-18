"""Exposure state machine — the band the book should be operating in.

The gates say what just happened (a dislocation, an exhaustion). This says
where aggregate exposure should therefore SIT, and it is the piece that turns
signals into a position size for the whole fund.

Five states, each with a target band for delta-adjusted exposure:

    accumulation          100-110%   deploy the reserve aggressively
    full_participation     90-100%   stay invested, normal additions
    mature_advance         80-90%    stop chasing, hold what you have
    exhaustion_rotation    60-75%    trim the tactical sleeve, optionally hedge
    theme_break            below 40% structural exit, not a trading decision

This stays aggressive by design. Even in a normal risk-off state the fund
retains 60-75% effective exposure, because the thesis is a multi-year
supercycle and being flat during it is the larger error.

Two rules protect the fund from its own signals:

    ONE STEP PER DAY. Exposure moves 100 -> 90 -> 80 -> 70, never 100 -> 60.
    A signal that is right stays right tomorrow; a signal that is wrong does
    less damage when it only moved one step. The same applies on the way back
    up. Overridden only for a genuine theme break, where the structural thesis
    itself has changed.

    THE BAND IS A TARGET, NOT A TRIGGER. Exposure inside the band means HOLD.
    Acting on every drift produces trim-and-rebuy oscillation, which costs
    spread on every crossing and is the most reliable way to bleed a book that
    is directionally right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

STATE_ACCUMULATION = "accumulation"
STATE_FULL = "full_participation"
STATE_MATURE = "mature_advance"
STATE_EXHAUSTION = "exhaustion_rotation"
STATE_THEME_BREAK = "theme_break"

# Ordered from most to least invested — adjacency is what "one step" means.
STATE_ORDER = [STATE_ACCUMULATION, STATE_FULL, STATE_MATURE,
               STATE_EXHAUSTION, STATE_THEME_BREAK]

# Bands are on DELTA-ADJUSTED exposure, and that unit is why these numbers are
# not the 90-100% a cash-equity book would use. A long-dated call carries the
# delta of far more stock than the premium paid for it, so a fully-invested
# LEAPS book measures well above 100% by construction: on 2026-08-18 the book
# read 76.9% premium at 1.60x leverage — 122.9% delta-adjusted.
#
# The original bands topped out at 110%, which put a normally-invested book
# permanently above every band. The consequence was not conservatism but
# paralysis: the state machine returned reduce or hold on every tick, so the
# accumulation gate could fire on a genuine dislocation — thesis intact, no
# rotation — and never be allowed to act on it. That is the opposite of the
# intended behaviour, which is to keep buying dips while the thesis holds.
#
# Calibrated so a fully-invested book sits INSIDE full_participation rather
# than above it, leaving accumulation genuine headroom to deploy into a
# dislocation. Overridable in settings because this is a risk limit, and a risk
# limit that requires a code change to adjust is one nobody adjusts.
DEFAULT_TARGET_BANDS: dict[str, tuple[float, float]] = {
    STATE_ACCUMULATION: (1.40, 1.60),
    STATE_FULL: (1.15, 1.40),
    STATE_MATURE: (1.00, 1.15),
    STATE_EXHAUSTION: (0.70, 1.00),
    STATE_THEME_BREAK: (0.0, 0.50),
}


def target_bands() -> dict[str, tuple[float, float]]:
    """Bands in force, from settings when configured. Never raises."""
    try:
        from ..config import get_settings
        cfg = getattr(get_settings(), "PORTFOLIO_TARGET_BANDS", None)
        if isinstance(cfg, dict) and cfg:
            out = dict(DEFAULT_TARGET_BANDS)
            for k, v in cfg.items():
                if k in out and isinstance(v, (list, tuple)) and len(v) == 2:
                    out[k] = (float(v[0]), float(v[1]))
            return out
    except Exception:  # noqa: BLE001 — a bad override must not break the decision
        pass
    return dict(DEFAULT_TARGET_BANDS)


# Retained for callers that read the module attribute directly.
TARGET_BANDS = DEFAULT_TARGET_BANDS


@dataclass
class PortfolioState:
    state: str
    target_low: float
    target_high: float
    current_exposure: float
    action: str                       # add | hold | reduce
    gap: float = 0.0                  # signed distance to the nearest band edge
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def in_band(self) -> bool:
        return self.target_low <= self.current_exposure <= self.target_high

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "target_band": [self.target_low, self.target_high],
            "current_exposure": round(self.current_exposure, 4),
            "in_band": self.in_band,
            "gap": round(self.gap, 4),
            "action": self.action,
            "reasons": self.reasons,
            "detail": self.detail,
        }


def classify_state(
    *,
    theme_broken: bool,
    regime_score: int,
    exhaustion: int,
    selling_exhaustion: int,
    accumulation_ready: bool,
    trim_ready: bool,
) -> tuple[str, list[str]]:
    """Which state the complex is in, from the weekly evidence. Pure.

    Checked most-severe first, because a theme break outranks everything and
    an exhaustion call outranks a healthy-looking regime score.
    """
    reasons: list[str] = []

    if theme_broken:
        return STATE_THEME_BREAK, ["structural thesis broken — not a trading decision"]

    if trim_ready or exhaustion >= 3:
        reasons.append(f"exhaustion {exhaustion} with deterioration")
        return STATE_EXHAUSTION, reasons

    if accumulation_ready:
        reasons.append("dislocation confirmed — all accumulation conditions aligned")
        return STATE_ACCUMULATION, reasons

    # No dislocation and no exhaustion: position by how intact the trend is.
    if regime_score >= 4:
        return STATE_FULL, ["regime 4/4 — full participation"]
    if regime_score == 3:
        return STATE_FULL, ["regime 3/4 — trend intact"]
    if regime_score == 2:
        return STATE_MATURE, ["regime 2/4 — advance maturing, stop chasing"]

    # regime 0-1 with the theme intact. Weak, but a weak regime inside an
    # intact supercycle is a pullback, not an exit — hold the exhaustion band
    # rather than dropping toward theme-break levels.
    reasons.append(f"regime {regime_score}/4 weak but thesis intact — defensive, not out")
    if selling_exhaustion >= 2:
        reasons.append(f"selling exhaustion {selling_exhaustion} building")
    return STATE_EXHAUSTION, reasons


def step_toward(current_state: Optional[str], target_state: str,
                *, immediate: Optional[set[str]] = None) -> str:
    """Move at most ONE state toward the target. Pure.

    A signal that is right will still be right tomorrow, and a signal that is
    wrong does less damage when it only moved the book one step. Applies in
    both directions, so re-accumulation is as gradual as reduction.

    A theme break is the sole exception: the structural thesis has changed, so
    the move is immediate rather than staged.
    """
    if target_state == STATE_THEME_BREAK or current_state is None:
        return target_state
    if immediate and target_state in immediate:
        # A confirmed accumulation signal is a discrete event, not drift, and
        # it already carries its own damping: the gate deploys 25/25/50 across
        # three stages with confirmation required between them. Making the
        # STATE crawl toward it as well damps the same signal twice, and a
        # dislocation is usually over before a two-step crawl arrives. The
        # one-step rule still governs every reduction and all ordinary drift,
        # which is where whipsaw actually costs money.
        return target_state
    if current_state == target_state:
        return target_state
    try:
        ci, ti = STATE_ORDER.index(current_state), STATE_ORDER.index(target_state)
    except ValueError:
        return target_state
    return STATE_ORDER[ci + (1 if ti > ci else -1)]


def resolve(
    *,
    current_exposure: float,
    target_state: str,
    previous_state: Optional[str] = None,
    tolerance: float = 0.02,
    accumulation_confirmed: bool = False,
) -> PortfolioState:
    """Target band and the resulting instruction. Pure.

    ``tolerance`` keeps the book still when it is marginally outside the band.
    Without it, exposure drifting a point past an edge triggers a trade, the
    trade overshoots, and the book oscillates — paying spread on every
    crossing. Trim-and-rebuy churn is the most reliable way to bleed a
    portfolio that is directionally correct.
    """
    state = step_toward(
        previous_state, target_state,
        immediate=({STATE_ACCUMULATION} if accumulation_confirmed else None))
    lo, hi = target_bands()[state]
    reasons: list[str] = []
    if state != target_state:
        reasons.append(f"stepping {previous_state} -> {state} (target {target_state}, "
                       f"one step per day)")

    if current_exposure < lo - tolerance:
        action, gap = "add", lo - current_exposure
        reasons.append(f"exposure {current_exposure:.1%} below band {lo:.0%}-{hi:.0%}")
    elif current_exposure > hi + tolerance:
        action, gap = "reduce", current_exposure - hi
        reasons.append(f"exposure {current_exposure:.1%} above band {lo:.0%}-{hi:.0%}")
    else:
        action, gap = "hold", 0.0
        reasons.append(f"exposure {current_exposure:.1%} within band "
                       f"{lo:.0%}-{hi:.0%} — no action")

    return PortfolioState(
        state=state, target_low=lo, target_high=hi,
        current_exposure=current_exposure, action=action, gap=gap, reasons=reasons,
        detail={"target_state": target_state, "previous_state": previous_state,
                "tolerance": tolerance},
    )


# ---------------------------------------------------------------------------
# Sleeves
# ---------------------------------------------------------------------------

# Proportional split. Every position carries the same core/tactical ratio, so a
# trim reduces the whole basket evenly and no single name is ever fully exited
# by a portfolio-level decision. That is what the spec asks for — "trim the
# tactical sleeve proportionally across the basket... avoid disturbing the
# permanent core" — and it is why the alternative (oldest positions become
# core, newest become tactical) was rejected: it would concentrate every trim
# into the most recent entries, which are exactly the ones with the least
# information about whether they work.
SLEEVE_CORE = 0.60
SLEEVE_TACTICAL = 0.30
SLEEVE_RESERVE = 0.10


def tactical_trim_quantity(
    *, held_qty: float, reduce_fraction: float,
    tactical_share: float = SLEEVE_TACTICAL,
) -> int:
    """Contracts to sell from one position for a portfolio-level reduction. Pure.

    ``reduce_fraction`` is of TOTAL exposure, and only the tactical sleeve may
    be touched, so the per-position cut is scaled by how much of the book that
    sleeve represents. Rounded DOWN and floored at zero: a portfolio decision
    must never fully close a position — that is a thesis decision, taken per
    name by the exit logic, not by an exposure band.
    """
    if held_qty <= 1 or reduce_fraction <= 0 or tactical_share <= 0:
        return 0
    # Fraction of the position represented by the reduction, expressed within
    # the tactical sleeve.
    per_position = reduce_fraction / tactical_share
    qty = int(held_qty * min(per_position, 1.0) * tactical_share)
    # Never take the last contract at portfolio level.
    return max(0, min(qty, int(held_qty) - 1))
