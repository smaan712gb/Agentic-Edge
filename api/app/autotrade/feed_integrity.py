"""Feed integrity — detect signals that have silently stopped being real.

The existing health monitor answers "is the machine running": broker up,
positions matched to intents, margin above the floor, jobs not wedged. Every
one of those checks passed continuously through six production defects, each
of which had been live for weeks before anyone noticed:

    options flow      every symbol $0      (right parsed from the OCC strike)
    dark pool         every call 404       (wrong provider route)
    macro gate        permanently 'calm'   (VIX/SPX null, no exception raised)
    rotation flags    9 days stale, read as current
    bullish_etfs      0 on every theme     (downstream of the flow defect)
    z_flow_imbalance  dropped as zero-variance

Not one raised. Every run completed successfully. The runs computed garbage,
and "successful run" is precisely what a dead feed looks like from outside.

The shared signature is that a quantity which should MOVE stopped moving, or a
source which should return rows started returning none. That is detectable
without knowing the defect — which is the whole point, because the next one
will be something nobody has thought of yet.

Four detectors, every one judged against the feed's OWN history rather than an
absolute threshold, so a new feed needs no configuration and no existing feed
needs re-tuning when the regime changes:

    empty       coverage 0 on every observation in the window
    flatline    a numeric that should move has zero variance across sessions
    degraded    coverage far below this feed's own trailing median
    silent      nothing recorded within this feed's own learned cadence

Coverage and variance are deliberately separate. The macro gate failed with
full variance and no coverage (both inputs null); the options flow failed with
full coverage and no variance (alerts arrived, every premium summed to zero).
Either detector alone would have caught only one of them.

A fifth detector covers the opposite failure, which is just as quiet: a gate
whose conditions are so rarely satisfiable that it never fires at all. A rule
that never opens and a market that never qualifies are indistinguishable from
outside; the difference shows up only in WHICH condition does the blocking, so
that is what gets reported.

Observation is a fire-and-forget call from wherever a signal is computed. This
module never trades, never mutates a position, and never raises into a caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Optional

from sqlalchemy import select

from ..config import get_settings
from ..db import AutoAction, get_session as db_session

logger = logging.getLogger("agentic_edge.feeds")

FEED_OBSERVATION = "feed_observation"
FEED_ANOMALY = "feed_anomaly"

# Structural defaults. These bound how much evidence is required before the
# module is willing to call something broken — they are not thresholds on any
# market quantity, which is why they can be constants at all. Every judgement
# about what a NORMAL value looks like comes from the feed's own history.
DEFAULT_WINDOW_HOURS = 96.0     # four days: spans a weekend without going blind
MIN_OBSERVATIONS = 4            # never accuse a feed on one or two readings
MIN_SPAN_HOURS = 20.0           # a flatline must cross a session boundary
COVERAGE_FLOOR_RATIO = 0.5      # "far below" = under half its own median
CADENCE_MULTIPLE = 3.0          # late by 3x its own median gap = silent
NEVER_FIRES_MIN_DAYS = 21.0     # a gate needs a real sample before it is judged

LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """One reading of one feed.

    ``coverage`` is the fraction of the inputs the feed NEEDED that it actually
    obtained — not the fraction that came back non-zero. A dark-pool call
    returning a legitimately empty print list has coverage 1.0 and numeric 0.0;
    a 404 has coverage 0.0. Conflating those two is what let the 404 hide.
    """
    feed: str
    ts: datetime
    numeric: Optional[float] = None
    categorical: Optional[str] = None
    coverage: Optional[float] = None
    subjects: int = 0

    @classmethod
    def from_payload(cls, p: dict[str, Any], ts: datetime) -> Optional["Observation"]:
        feed = p.get("feed")
        if not feed:
            return None
        cat = p.get("categorical")
        return cls(
            feed=str(feed), ts=ts,
            numeric=_as_float(p.get("numeric")),
            categorical=(str(cat) if cat is not None else None),
            coverage=_as_float(p.get("coverage")),
            subjects=int(p.get("subjects") or 0),
        )


@dataclass
class Anomaly:
    feed: str
    kind: str            # empty | flatline | degraded | silent | never_fires
    level: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"feed": self.feed, "kind": self.kind, "level": self.level,
                "detail": self.detail, "evidence": self.evidence}


def _as_float(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _span_hours(obs: list[Observation]) -> float:
    if len(obs) < 2:
        return 0.0
    return (obs[-1].ts - obs[0].ts).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Detectors — all PURE, so each is testable without a broker or a provider
# ---------------------------------------------------------------------------


def detect_empty(
    obs: list[Observation], *, min_obs: int = MIN_OBSERVATIONS,
) -> Optional[Anomaly]:
    """Coverage zero on every observation — the source is returning nothing.

    This is the dark-pool 404 and the null macro gate. Both kept running and
    both produced a confident downstream answer built on no data at all.
    """
    known = [o for o in obs if o.coverage is not None]
    if len(known) < min_obs:
        return None
    if any((o.coverage or 0) > 0 for o in known):
        return None
    return Anomaly(
        feed=obs[0].feed, kind="empty", level=LEVEL_WARNING,
        detail=(f"returned no usable data on all {len(known)} readings over "
                f"{_span_hours(known):.0f}h — downstream logic is running on a "
                f"default, not a measurement"),
        evidence={"observations": len(known),
                  "span_hours": round(_span_hours(known), 1),
                  "subjects": known[-1].subjects})


def detect_flatline(
    obs: list[Observation], *,
    min_obs: int = MIN_OBSERVATIONS, min_span_hours: float = MIN_SPAN_HOURS,
) -> Optional[Anomaly]:
    """A numeric that should move has not moved at all.

    Restricted to numerics on purpose. Categorical feeds are legitimately
    constant for long stretches — a macro regime really can read 'calm' for a
    month, and alerting on that is the noise that teaches an operator to ignore
    the monitor. Market numerics do not repeat to the digit across sessions, so
    a span requirement plus zero variance is close to proof.

    All-zero is called out separately from merely-constant because it is the
    signature of a parse or aggregation defect rather than a stalled provider.
    """
    known = [o for o in obs if o.numeric is not None]
    if len(known) < min_obs:
        return None
    span = _span_hours(known)
    if span < min_span_hours:
        return None
    values = {o.numeric for o in known}
    if len(values) > 1:
        return None
    v = next(iter(values))
    all_zero = (v == 0.0)
    why = (" — a market quantity summing to exactly zero every time is a parse "
           "or aggregation defect, not a quiet tape") if all_zero else (
          " — the provider is likely serving a cached or default response")
    return Anomaly(
        feed=obs[0].feed, kind="flatline", level=LEVEL_WARNING,
        detail=(f"identical value {v} on all {len(known)} readings across "
                f"{span:.0f}h{why}"),
        evidence={"value": v, "observations": len(known),
                  "span_hours": round(span, 1), "all_zero": all_zero})


def detect_degraded(
    obs: list[Observation], *,
    min_history: int = MIN_OBSERVATIONS * 2,
    floor_ratio: float = COVERAGE_FLOOR_RATIO,
) -> Optional[Anomaly]:
    """Coverage has fallen well below what this feed normally achieves.

    Compared against the feed's own trailing median rather than any fixed
    number, because acceptable coverage is a property of the feed: a 13F
    overlap lookup resolving 70% of CUSIPs is healthy, while a quote feed at
    70% is broken. Only the feed's own history knows which it is.
    """
    known = [o for o in obs if o.coverage is not None]
    if len(known) < min_history:
        return None
    history, latest = known[:-1], known[-1]
    med = median(o.coverage or 0.0 for o in history)
    if med <= 0 or latest.coverage is None:
        return None            # no baseline to regress from; detect_empty owns this
    if latest.coverage >= med * floor_ratio:
        return None
    return Anomaly(
        feed=obs[0].feed, kind="degraded", level=LEVEL_WARNING,
        detail=(f"coverage {latest.coverage:.0%} against a normal {med:.0%} — the "
                f"feed is answering, but for a fraction of what it used to cover"),
        evidence={"coverage": round(latest.coverage, 3),
                  "median_coverage": round(med, 3), "history": len(history)})


def detect_silence(
    obs: list[Observation], now: datetime, *,
    cadence_multiple: float = CADENCE_MULTIPLE, min_history: int = MIN_OBSERVATIONS,
) -> Optional[Anomaly]:
    """Nothing recorded for far longer than this feed's own normal gap.

    The cadence is learned rather than declared so that changing a cron does
    not silently invalidate the check — the failure being guarded against is a
    job that stopped running, and a hand-maintained expected interval is one
    more thing that can quietly fall out of step with reality.
    """
    if len(obs) < min_history:
        return None
    gaps = [(obs[i].ts - obs[i - 1].ts).total_seconds() / 60.0
            for i in range(1, len(obs))]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    med_gap = median(gaps)
    since = (now - obs[-1].ts).total_seconds() / 60.0
    if since <= med_gap * cadence_multiple:
        return None
    return Anomaly(
        feed=obs[0].feed, kind="silent", level=LEVEL_WARNING,
        detail=(f"last reading {since:.0f} min ago against a normal {med_gap:.0f} "
                f"min cadence — whatever produces this feed has stopped"),
        evidence={"minutes_since": round(since), "median_gap_min": round(med_gap, 1),
                  "observations": len(obs)})


def detect_never_fires(
    decisions: list[tuple[datetime, str, list[str]]], *,
    firing_actions: set[str], feed: str,
    min_days: float = NEVER_FIRES_MIN_DAYS,
) -> Optional[Anomaly]:
    """A gate that has never once opened, and the condition doing the blocking.

    Each entry is (timestamp, action, blocked_by). A gate sitting closed is not
    itself a defect — that is what a gate is for — but a gate that has NEVER
    opened across a meaningful sample is indistinguishable from one wired shut,
    and the distinction matters because the two have opposite remedies.

    Reporting the most frequent blocker turns an unanswerable question ("why
    does it never buy?") into a specific one ("regime>=3 blocked 58 of 60
    evaluations"), which is the difference between a rule an operator can judge
    and a black box they end up overriding by hand.
    """
    if not decisions:
        return None
    decisions = sorted(decisions, key=lambda d: d[0])
    span_days = (decisions[-1][0] - decisions[0][0]).total_seconds() / 86400.0
    if span_days < min_days:
        return None
    if any(a in firing_actions for _, a, _ in decisions):
        return None
    counts: dict[str, int] = {}
    for _, _, blocked in decisions:
        for b in (blocked or []):
            counts[b] = counts.get(b, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    top = ", ".join(f"{k} ({v}/{len(decisions)})" for k, v in ranked[:3])
    return Anomaly(
        feed=feed, kind="never_fires", level=LEVEL_INFO,
        detail=(f"never once fired in {len(decisions)} evaluations over "
                f"{span_days:.0f} days. Binding conditions: "
                f"{top or 'none recorded'}"),
        evidence={"evaluations": len(decisions), "span_days": round(span_days, 1),
                  "blockers": dict(ranked)})


def analyze_feed(obs: list[Observation], now: datetime) -> list[Anomaly]:
    """Every detector for one feed, most-diagnostic first.

    ``empty`` short-circuits the rest: a feed returning nothing will also look
    flatlined and degraded, and three alerts describing one dead provider is
    how a monitor becomes background noise.
    """
    if not obs:
        return []
    obs = sorted(obs, key=lambda o: o.ts)
    empty = detect_empty(obs)
    if empty:
        return [empty]
    return [d for d in (detect_flatline(obs), detect_degraded(obs),
                        detect_silence(obs, now)) if d]


# ---------------------------------------------------------------------------
# Recording and reading
# ---------------------------------------------------------------------------


async def observe(
    feed: str, *,
    numeric: Optional[float] = None,
    categorical: Optional[str] = None,
    coverage: Optional[float] = None,
    subjects: int = 0,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Record one reading. Fire-and-forget: never raises into the caller.

    Swallowing every exception is normally the defect this module exists to
    find, and it is correct here for the one case where it is safe: this
    function only writes an audit row. A monitor that can break the thing it
    monitors is worse than no monitor.
    """
    if not getattr(get_settings(), "FEED_INTEGRITY_ENABLED", True):
        return
    try:
        from .auto_gate import AutoGateResult, record_auto_action
        payload: dict[str, Any] = {"feed": feed, "numeric": numeric,
                                   "categorical": categorical, "coverage": coverage,
                                   "subjects": subjects}
        if extra:
            payload["extra"] = extra
        async with db_session() as s:
            await record_auto_action(
                s, loop="feeds", action_type=FEED_OBSERVATION,
                gate_result=AutoGateResult(passed=True, failures=[]),
                payload=payload, outcome=feed)
    except Exception as e:  # noqa: BLE001 — observation must never break a caller
        logger.debug("feed observe(%s) failed: %s", feed, e)


async def observe_many(readings: list[dict[str, Any]]) -> None:
    """Record many readings in one transaction.

    The feature store observes several dozen feeds in a single pass, and one
    session per feed would make the monitor the most expensive part of the
    snapshot it is supposed to be quietly watching.
    """
    if not readings or not getattr(get_settings(), "FEED_INTEGRITY_ENABLED", True):
        return
    try:
        from .auto_gate import AutoGateResult, record_auto_action
        ok = AutoGateResult(passed=True, failures=[])
        async with db_session() as s:
            for r in readings:
                feed = r.get("feed")
                if not feed:
                    continue
                await record_auto_action(
                    s, loop="feeds", action_type=FEED_OBSERVATION, gate_result=ok,
                    payload={"feed": feed, "numeric": r.get("numeric"),
                             "categorical": r.get("categorical"),
                             "coverage": r.get("coverage"),
                             "subjects": int(r.get("subjects") or 0)},
                    outcome=str(feed)[:48])
    except Exception as e:  # noqa: BLE001 — observation must never break a caller
        logger.debug("feed observe_many failed: %s", e)


async def load_observations(window_hours: float = DEFAULT_WINDOW_HOURS
                            ) -> dict[str, list[Observation]]:
    """Every observation in the window, grouped by feed name."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    out: dict[str, list[Observation]] = {}
    async with db_session() as s:
        rows = (await s.execute(
            select(AutoAction.payload, AutoAction.timestamp)
            .where(AutoAction.action_type == FEED_OBSERVATION)
            .where(AutoAction.timestamp >= cutoff)
            .order_by(AutoAction.timestamp.asc())
        )).all()
    for payload, ts in rows:
        if not isinstance(payload, dict) or ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        o = Observation.from_payload(payload, ts)
        if o is not None:
            out.setdefault(o.feed, []).append(o)
    return out


async def _gate_history(days: float) -> dict[str, list[tuple[datetime, str, list[str]]]]:
    """Accumulation and trim gate outcomes from the persisted daily decisions."""
    from ..portfolio.daily import DECISION_ACTION_TYPE

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    hist: dict[str, list[tuple[datetime, str, list[str]]]] = {}
    async with db_session() as s:
        rows = (await s.execute(
            select(AutoAction.payload, AutoAction.timestamp)
            .where(AutoAction.action_type == DECISION_ACTION_TYPE)
            .where(AutoAction.timestamp >= cutoff)
            .order_by(AutoAction.timestamp.asc())
        )).all()
    for payload, ts in rows:
        if not isinstance(payload, dict) or ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        for key, gate in (("accumulation_gate", payload.get("accumulation_gate")),
                          ("trim_gate", payload.get("trim_gate"))):
            if isinstance(gate, dict) and gate.get("action"):
                hist.setdefault(key, []).append(
                    (ts, str(gate["action"]), list(gate.get("blocked_by") or [])))
    return hist


async def run_feed_integrity_check() -> dict[str, Any]:
    """Scan every feed and gate, alert on anomalies, return a summary.

    Called by the 15-minute health monitor. Never raises, never trades.
    """
    s = get_settings()
    if not getattr(s, "FEED_INTEGRITY_ENABLED", True):
        return {"enabled": False, "anomalies": []}

    window = float(getattr(s, "FEED_INTEGRITY_WINDOW_HOURS", DEFAULT_WINDOW_HOURS))
    now = datetime.now(timezone.utc)
    anomalies: list[Anomaly] = []
    feeds_seen = 0

    try:
        grouped = await load_observations(window)
        feeds_seen = len(grouped)
        for _feed, obs in sorted(grouped.items()):
            anomalies.extend(analyze_feed(obs, now))
    except Exception as e:  # noqa: BLE001
        logger.warning("feed integrity: observation scan failed: %s", e)

    try:
        gate_days = float(getattr(s, "FEED_INTEGRITY_GATE_DAYS", 90.0))
        for name, decisions in (await _gate_history(gate_days)).items():
            firing = ({"accumulate"} if name == "accumulation_gate"
                      else {"trim", "stop_adding"})
            a = detect_never_fires(decisions, firing_actions=firing, feed=name)
            if a:
                anomalies.append(a)
    except Exception as e:  # noqa: BLE001
        logger.warning("feed integrity: gate scan failed: %s", e)

    return {"enabled": True, "feeds_observed": feeds_seen,
            "anomalies": [a.to_dict() for a in anomalies]}
