"""Perfect Entry Score — the 0-100 entry checklist with visible reasons.

The spec: every idea gets a score like "92/100" with the checklist that
produced it (✔ pullback into the 21 EMA, ✔ institutions buying, ✔ volume
contraction, ✔ breakout pending, ...). Pure function over the already-
enriched idea dict — deterministic, auditable, and cheap.

Weights (max 100 before penalties):
    25  Trend        — the 8/21/50/100/200 EMA stack alignment
    10  Entry point  — pullback into the 8/21 EMA zone (not extended)
    20  Institutions — cross-fund confirmation + bullish options flow
    15  Street       — upgrades vs downgrades + consensus-target upside
    10  Volume       — contraction near highs (VCP behaviour)
    10  Breakout     — a base/trigger pattern pending overhead
    10  Thesis       — the research team's composite conviction

Penalties: failed breakout −15 · distribution days −10 · theme rotation −10.

Decision-support only: the entry LOOP keeps its own gate stack; this score is
the operator's read on setup quality, not an order trigger.
"""

from __future__ import annotations

from typing import Any, Optional


def _reason(ok: bool, text: str) -> dict[str, Any]:
    return {"ok": ok, "text": text}


def compute_entry_score(idea: dict[str, Any]) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    score = 0.0

    # --- Trend (25) ---------------------------------------------------------
    trend = idea.get("trend") or {}
    stack_score = float(trend.get("score") or 0.0)
    pts = stack_score / 100.0 * 25.0
    score += pts
    if stack_score >= 100:
        reasons.append(_reason(True, "perfect EMA stack (8>21>50>100>200)"))
    elif stack_score >= 75:
        reasons.append(_reason(True, "EMA stack mostly aligned"))
    else:
        reasons.append(_reason(False, f"trend not aligned ({trend.get('label') or 'no data'})"))

    # --- Entry point (10): pullback into the 8/21 EMA zone ------------------
    emas = trend.get("emas") or {}
    price = trend.get("price")
    ema8, ema21 = emas.get("ema8"), emas.get("ema21")
    if price and ema8 and ema21 and stack_score >= 75:
        if price <= ema8 * 1.02 and price >= ema21 * 0.97:
            score += 10
            reasons.append(_reason(True, "pullback into the 8/21 EMA zone — buyable dip"))
        elif price > ema8 * 1.08:
            reasons.append(_reason(False, f"extended {100 * (price / ema8 - 1):.0f}% above the 8 EMA — chase risk"))
        else:
            score += 5
            reasons.append(_reason(True, "near the moving-average zone"))

    # --- Institutions (20) ---------------------------------------------------
    smart = idea.get("smart_money") or {}
    if smart.get("confirmation"):
        score += 12
        reasons.append(_reason(True, f"institutions buying — {smart.get('manager_count', 0)} tracked funds hold it"))
    elif smart.get("manager_count"):
        score += 6
        reasons.append(_reason(True, f"{smart['manager_count']} tracked fund holds it"))
    else:
        reasons.append(_reason(False, "no tracked-fund ownership yet"))
    flow = (idea.get("technical") or {}).get("flow_imbalance")
    if flow is not None and flow >= 0.2:
        score += 8
        reasons.append(_reason(True, "options flow tilting bullish"))
    elif flow is not None and flow <= -0.2:
        reasons.append(_reason(False, "options flow tilting bearish"))

    # --- Street (15) ---------------------------------------------------------
    analyst = idea.get("analyst") or {}
    up, down = analyst.get("n_up_30d", 0), analyst.get("n_down_30d", 0)
    if up > down and up > 0:
        score += 8
        reasons.append(_reason(True, f"analyst upgrades ({up} up vs {down} down in 30d)"))
    elif down > up and down > 0:
        reasons.append(_reason(False, f"analyst downgrades ({down} down vs {up} up in 30d)"))
    upside = analyst.get("upside_pct")
    if upside is not None:
        if upside >= 10:
            score += 7
            reasons.append(_reason(True, f"consensus target {upside:+.0f}% above price"))
        elif upside <= -5:
            reasons.append(_reason(False, f"price {abs(upside):.0f}% ABOVE the consensus target"))

    # --- Volume contraction (10) ---------------------------------------------
    patterns = {p.get("key") for p in (idea.get("patterns") or [])}
    rvol = (idea.get("technical") or {}).get("rvol")
    if "vcp" in patterns:
        score += 10
        reasons.append(_reason(True, "volume contraction pattern — supply drying up"))
    elif rvol is not None and rvol < 0.8 and stack_score >= 75:
        score += 7
        reasons.append(_reason(True, f"quiet volume ({rvol:.1f}× normal) while holding highs"))

    # --- Breakout pending (10) ------------------------------------------------
    trigger = patterns & {"flat_base", "ascending_triangle", "cup_handle"}
    if trigger:
        score += 10
        names = {"flat_base": "flat base", "ascending_triangle": "ascending triangle",
                 "cup_handle": "cup & handle"}
        reasons.append(_reason(True, f"breakout pending — {names[next(iter(trigger))]} formed"))
    elif "stage2_breakout" in patterns:
        score += 8
        reasons.append(_reason(True, "already breaking out (Stage 2)"))

    # --- Thesis (10) -----------------------------------------------------------
    composite = float(idea.get("composite") or 0.0)
    if composite >= 7.0:
        score += 10
        reasons.append(_reason(True, f"research team conviction high (composite {composite:.1f}/10)"))
    elif composite >= 6.0:
        score += 5
        reasons.append(_reason(True, f"research team constructive (composite {composite:.1f}/10)"))

    # --- Penalties --------------------------------------------------------------
    if "failed_breakout" in patterns:
        score -= 15
        reasons.append(_reason(False, "recent FAILED breakout — bull trap risk"))
    structure = idea.get("structure") or {}
    if structure.get("distribution_flag"):
        score -= 10
        reasons.append(_reason(
            False, f"distribution — {structure.get('distribution_days_25')} heavy-volume down days in 25 sessions"))
    if idea.get("rotation_flagged"):
        score -= 10
        reasons.append(_reason(False, "theme rotation flagged — institutions rotating out"))

    score = max(0.0, min(100.0, round(score, 1)))
    if score >= 85:
        label = "prime setup"
    elif score >= 70:
        label = "good setup"
    elif score >= 50:
        label = "developing"
    else:
        label = "not ready"
    return {"score": score, "label": label, "reasons": reasons}
