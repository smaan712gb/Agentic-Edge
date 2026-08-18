"""Portfolio-level accumulation and trim gates.

These decide the only three things that matter at the book level: when to stop
adding, when to reduce aggregate exposure, and when to redeploy into the next
leg. They are deliberately NOT ticker decisions — the thesis is a multi-year
AI-infrastructure supercycle, so the objective is to stay structurally invested
while it is intact and convert portfolio-level euphoria into dry powder for
portfolio-level dislocations.

Everything here is weekly-close based. A high-beta complex produces a daily
signal on every ordinary pullback, and 2026-08-18 is the case in point: a
-4.8% session that halted all buying while the index sat 0.13 ATR above its
20-week mean — mid-range, neither extended nor washed out. Daily data answers
"is today red"; the weekly close answers "has the trend changed".

Scores are COUNTS of discrete conditions, one point each, not normalised
indices. That keeps every reading auditable — a score of 3 means three named
things are true, and the caller can see which — and it is why the thresholds
differ between the two scores (selling-exhaustion >= 2, exhaustion >= 3): they
count different numbers of conditions, so a shared scale would be misleading.

All functions here are PURE. They take measurements and return decisions, so
every threshold can be tested against a synthetic book without a broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Weekly regime — is the structural uptrend still in force?
# ---------------------------------------------------------------------------


@dataclass
class RegimeScore:
    score: int
    max_score: int
    components: dict[str, bool] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "max_score": self.max_score,
                "components": self.components, "unknown": self.unknown}


def weekly_regime_score(
    *,
    close: Optional[float],
    ma_w20: Optional[float],
    ma_w20_rising: Optional[bool],
    rs_vs_benchmark: Optional[float],
    breadth_above_w20: Optional[float],
    structure_intact: Optional[bool],
    breadth_floor: float = 0.50,
) -> RegimeScore:
    """0-4: how intact the weekly uptrend is. Pure.

    Four independent questions, one point each:

      1. trend      — index above a RISING 20-week average. Above a falling
                      average is a bounce inside a downtrend, not an uptrend,
                      which is why the slope is required as well as the level.
      2. relative   — the complex outperforming its benchmark. This separates
                      "everything is down" (beta) from "this is being left
                      behind" (rotation).
      3. breadth    — a majority of constituents individually above their own
                      20-week average, so the index is not being carried by a
                      handful of names.
      4. structure  — higher highs AND higher lows on the weekly pivots.

    An input that cannot be computed scores 0 and is recorded in ``unknown``
    rather than being assumed true. Missing evidence is not evidence.
    """
    comp: dict[str, bool] = {}
    unknown: list[str] = []

    if close is None or ma_w20 is None or ma_w20_rising is None:
        comp["trend"] = False
        unknown.append("trend")
    else:
        comp["trend"] = bool(close > ma_w20 and ma_w20_rising)

    if rs_vs_benchmark is None:
        comp["relative_strength"] = False
        unknown.append("relative_strength")
    else:
        comp["relative_strength"] = bool(rs_vs_benchmark > 0)

    if breadth_above_w20 is None:
        comp["breadth"] = False
        unknown.append("breadth")
    else:
        comp["breadth"] = bool(breadth_above_w20 > breadth_floor)

    if structure_intact is None:
        comp["structure"] = False
        unknown.append("structure")
    else:
        comp["structure"] = bool(structure_intact)

    return RegimeScore(score=sum(1 for v in comp.values() if v),
                       max_score=4, components=comp, unknown=unknown)


# ---------------------------------------------------------------------------
# Exhaustion — is a strong run running out of participation?
# ---------------------------------------------------------------------------


@dataclass
class ConditionScore:
    score: int
    conditions: dict[str, bool] = field(default_factory=dict)

    @property
    def met(self) -> list[str]:
        return sorted(k for k, v in self.conditions.items() if v)

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "conditions": self.conditions, "met": self.met}


def exhaustion_score(
    *,
    extension_atr: Optional[float],
    breadth_divergence: Optional[bool],
    rs_vs_benchmark: Optional[float],
    rs_was_positive: Optional[bool],
    distribution_weeks: Optional[int],
    equal_lagging_cap: Optional[bool],
    extension_warn: float = 2.0,
    distribution_min: int = 3,
) -> ConditionScore:
    """Count of exhaustion conditions. Trim gate requires >= 3. Pure.

    Deliberately none of these is "the portfolio made money". Trimming a winner
    for rising is how a structural position gets given away; these count the
    ways a strong run stops being supported:

      extended            — >= 2.0 weekly ATR above the mean. Volatility-scaled,
                            because "+15%" means nothing without knowing how far
                            this index normally travels.
      breadth_divergence  — index near its high, participation not. The single
                            most reliable warning in the framework.
      rs_breaking         — relative strength was positive and has turned. A
                            complex that stops leading is being rotated out of.
      distribution        — a cluster of heavy-volume down weeks.
      narrowing           — equal-weighted lagging cap-weighted: the advance is
                            concentrating into the largest names.
    """
    c: dict[str, bool] = {
        "extended": bool(extension_atr is not None and extension_atr >= extension_warn),
        "breadth_divergence": bool(breadth_divergence),
        "rs_breaking": bool(rs_was_positive and rs_vs_benchmark is not None
                            and rs_vs_benchmark <= 0),
        "distribution": bool(distribution_weeks is not None
                             and distribution_weeks >= distribution_min),
        "narrowing": bool(equal_lagging_cap),
    }
    return ConditionScore(score=sum(1 for v in c.values() if v), conditions=c)


def selling_exhaustion_score(
    *,
    breadth_washout: Optional[bool],
    down_volume_spike_fading: Optional[bool],
    correlation_spike: Optional[bool],
    declines_shrinking: Optional[bool],
    stopped_making_lows: Optional[bool],
) -> ConditionScore:
    """Count of selling-exhaustion conditions. Accumulation requires >= 2. Pure.

    These are not "price fell", which is the trap the whole design exists to
    avoid. They are the signatures of sellers finishing:

      breadth_washout     — participation collapsed; almost nothing is holding
                            its trend, which is what a flush looks like.
      volume_fading       — down-volume spiked and is now contracting: the
                            urgent selling has been done.
      correlation_spike   — everything moving together. Correlation goes to one
                            in capitulation, not in orderly rotation.
      declines_shrinking  — successive down weeks getting smaller despite
                            continued bad news; supply is being absorbed.
      stopped_making_lows — the index is no longer extending its low.
    """
    c: dict[str, bool] = {
        "breadth_washout": bool(breadth_washout),
        "volume_fading": bool(down_volume_spike_fading),
        "correlation_spike": bool(correlation_spike),
        "declines_shrinking": bool(declines_shrinking),
        "stopped_making_lows": bool(stopped_making_lows),
    }
    return ConditionScore(score=sum(1 for v in c.values() if v), conditions=c)


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


@dataclass
class GateDecision:
    """A portfolio instruction, with the reasoning that produced it."""
    action: str                     # see ACTIONS below
    stage: int = 0                  # staging step within accumulate/trim
    deploy_fraction: float = 0.0    # of RESERVED tactical capital
    trim_fraction: float = 0.0      # of delta-adjusted exposure
    reasons: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "stage": self.stage,
            "deploy_fraction": self.deploy_fraction,
            "trim_fraction": self.trim_fraction,
            "reasons": self.reasons, "blocked_by": self.blocked_by,
            "detail": self.detail,
        }


ACTION_ACCUMULATE = "accumulate"
ACTION_HOLD = "hold"
ACTION_STOP_ADDING = "stop_adding"
ACTION_TRIM = "trim"
ACTION_THEME_REVIEW = "theme_review"


def accumulation_gate(
    *,
    theme_score_positive: bool,
    regime: RegimeScore,
    volatility_pct: Optional[float],
    confluence: int,
    correction_atr: Optional[float],
    breadth_deterioration_stopped: bool,
    selling_exhaustion: ConditionScore,
    stage_completed: int = 0,
    strong_weekly_reversal: bool = False,
    closed_above_reversal_high: bool = False,
    rs_restored_positive: bool = False,
    min_volatility_pct: float = 0.084,
    min_selling_exhaustion: int = 2,
) -> GateDecision:
    """Deploy reserved tactical capital into a portfolio-level dislocation.

    Two conditions must hold: the complex is in a genuinely high-volatility
    state, and there is evidence sellers are finishing. Both were selected by
    measurement rather than intuition, replacing a six-condition conjunction
    that was verified never to fire.

    HOW THIS WAS CHOSEN. Candidate conditions were screened by point-in-time
    replay over 6,863 weeks and eight complexes (``research.gate_backtest``),
    fitted on five baskets and validated on three never used for selection —
    hypergrowth, china_tech, and ai_infra, the live book. Forward 13-week
    return against the unconditional baseline:

        condition                       dev      holdout   baskets +ve
        volatility + selling exhaustion +5.8%    +5.0%        3/3
        volatility alone                +6.4%    +5.6%        3/3
        + correction>=1.5ATR            +4.2%    +3.3%        2/3
        + breadth_deterioration_stopped +3.1%    +1.8%        2/3
        + regime>=3                     fires ONCE in 4,892 weeks

    Dev and holdout agreeing to within a point is what distinguishes this from
    the alternative that was rejected. A drawdown-based condition scored +2.7%
    on dev but decayed to nothing on holdout, went NEGATIVE at 52 weeks, and
    its entire remaining edge came from one basket — the survivorship-biased
    live book — while the two holdout complexes that genuinely broke showed
    +0.3% and +0.2%. Buying a deep drawdown pays only if the thesis holds,
    which is the one thing the rule cannot know.

    WHY THE OLD GATE COULD NOT WORK. ``regime>=3`` requires an intact weekly
    uptrend while the other conditions require a dislocation, and a dislocation
    destroys the regime score by construction — so the conjunction was close to
    self-contradictory. That, not the confluence threshold, was the deeper
    fault. Separately, ``min_confluence=5`` was the CEILING, not a high bar:
    ``compute_levels`` emits exactly five families and ``confluence_at`` counts
    distinct families, so it demanded all five align inside half a weekly ATR.

    WHY AN ABSOLUTE VOLATILITY THRESHOLD. A self-calibrating version — top
    quintile of the complex's own trailing three years — was tested and is
    WORSE (holdout +1.0%, 2/3 baskets), because a relative threshold also fires
    in a calm complex having a mildly active week. What carries the edge is
    absolute stress, so this is a deliberate exception to preferring
    self-referential thresholds, made because the measurement says so.

    Robustness: 77 distinct firing episodes in the holdout, 62% with positive
    13-week returns, against 61% across 170 dev episodes. Not one crash
    inflating a week count.

    Staging deliberately buys some exposure BEFORE confirmation and more after,
    which avoids both waiting until the recovery is obvious and committing
    everything to the first bounce:

        stage 1   25%   both conditions align
        stage 2   25%   a strong weekly reversal follows
        stage 3   50%   close above the reversal week's high, OR weekly
                        relative strength turns positive again

    ``stage_completed`` is what has already been deployed this cycle, so the
    caller advances one step at a time and never re-deploys a stage.

    """
    checks = {
        "theme_score_positive": theme_score_positive,
        f"volatility>={min_volatility_pct:.3f}": (
            volatility_pct is not None and volatility_pct >= min_volatility_pct),
        f"selling_exhaustion>={min_selling_exhaustion}": (
            selling_exhaustion.score >= min_selling_exhaustion),
    }
    blocked = [k for k, v in checks.items() if not v]
    # Regime, confluence, correction and breadth are still MEASURED and
    # recorded — they remain useful for reading a decision after the fact —
    # but they are no longer required. Each was tested and each cost holdout
    # edge; see the docstring.
    detail = {"checks": checks, "regime": regime.to_dict(),
              "selling_exhaustion": selling_exhaustion.to_dict(),
              "volatility_pct": volatility_pct,
              "observed_not_required": {
                  "regime_score": regime.score, "confluence": confluence,
                  "correction_atr": correction_atr,
                  "breadth_deterioration_stopped": breadth_deterioration_stopped}}

    if blocked:
        return GateDecision(action=ACTION_HOLD, blocked_by=blocked, detail=detail,
                            reasons=["accumulation conditions not aligned"])

    # Conditions hold — advance ONE stage from whatever has been done.
    if stage_completed < 1:
        return GateDecision(action=ACTION_ACCUMULATE, stage=1, deploy_fraction=0.25,
                            reasons=["dislocation confirmed by all six conditions"],
                            detail=detail)
    if stage_completed < 2:
        if not strong_weekly_reversal:
            return GateDecision(action=ACTION_HOLD, stage=1,
                                blocked_by=["awaiting strong weekly reversal"],
                                detail=detail)
        return GateDecision(action=ACTION_ACCUMULATE, stage=2, deploy_fraction=0.25,
                            reasons=["strong weekly reversal confirmed"], detail=detail)
    if stage_completed < 3:
        if not (closed_above_reversal_high or rs_restored_positive):
            return GateDecision(
                action=ACTION_HOLD, stage=2,
                blocked_by=["awaiting close above reversal-week high or RS recovery"],
                detail=detail)
        return GateDecision(action=ACTION_ACCUMULATE, stage=3, deploy_fraction=0.50,
                            reasons=["next leg confirmed"], detail=detail)
    return GateDecision(action=ACTION_HOLD, stage=3,
                        reasons=["tactical reserve fully deployed"], detail=detail)


def trim_gate(
    *,
    confluence_at_resistance: int,
    extension_atr: Optional[float],
    exhaustion: ConditionScore,
    deterioration_persists: bool,
    stage_completed: int = 0,
    confirmed_weekly_reversal: bool = False,
    min_confluence: int = 5,
    max_extension_atr: float = 2.5,
    min_exhaustion: int = 3,
) -> GateDecision:
    """Reduce aggregate exposure after a run stops being supported.

    Structure follows the spec exactly: a LOCATION condition (at a resistance
    zone with confluence >= 5, OR extended beyond 2.5 weekly ATR) AND an
    exhaustion score >= 3 AND persistent breadth/relative-strength
    deterioration. Location alone is not enough — an index can sit at
    resistance for weeks while participation stays healthy, and trimming that
    is just selling a winner.

    Staged, because the first trim is taken into strength and the second only
    after the market confirms:

        stage 1   10%    initial exhaustion
        stage 2   12.5%  only after a confirmed weekly reversal

    When the exhaustion evidence is present but the location condition is not,
    the answer is STOP ADDING rather than trim — raise no new risk, but do not
    sell a position that has not yet reached a level.
    """
    location = (confluence_at_resistance >= min_confluence
                or (extension_atr is not None and extension_atr > max_extension_atr))
    detail = {
        "location": location, "confluence": confluence_at_resistance,
        "extension_atr": extension_atr, "exhaustion": exhaustion.to_dict(),
        "deterioration_persists": deterioration_persists,
    }

    exhausted = exhaustion.score >= min_exhaustion
    if exhausted and deterioration_persists and not location:
        return GateDecision(
            action=ACTION_STOP_ADDING, detail=detail,
            reasons=["exhaustion and deterioration present, but not at a level — "
                     "raise no new risk rather than sell"])

    blocked = []
    if not location:
        blocked.append(f"no resistance zone (confluence {confluence_at_resistance}) "
                       f"and extension {extension_atr} <= {max_extension_atr} ATR")
    if not exhausted:
        blocked.append(f"exhaustion {exhaustion.score} < {min_exhaustion}")
    if not deterioration_persists:
        blocked.append("breadth/RS deterioration not persistent")
    if blocked:
        return GateDecision(action=ACTION_HOLD, blocked_by=blocked, detail=detail)

    if stage_completed < 1:
        return GateDecision(action=ACTION_TRIM, stage=1, trim_fraction=0.10,
                            reasons=["confirmed exhaustion at a level"], detail=detail)
    if stage_completed < 2:
        if not confirmed_weekly_reversal:
            return GateDecision(action=ACTION_STOP_ADDING, stage=1,
                                blocked_by=["awaiting confirmed weekly reversal"],
                                detail=detail)
        return GateDecision(action=ACTION_TRIM, stage=2, trim_fraction=0.125,
                            reasons=["weekly reversal confirmed"], detail=detail)
    return GateDecision(action=ACTION_STOP_ADDING, stage=2,
                        reasons=["tactical reduction complete for this cycle"],
                        detail=detail)
