"""Application settings — the single source of truth.

Everything that reads ``os.environ`` directly should be migrated to read
from ``get_settings()`` instead. Settings are loaded once at import time
from ``.env`` (development) or the platform's secret store (production).

Naming convention follows ``.env.example``:

    IBKR_HOST / IBKR_PORT / IBKR_CLIENT_ID / IBKR_MODE
    UNUSUAL_WHALES_API_KEY
    DEEPSEEK_DEEP_MODEL / DEEPSEEK_QUICK_MODEL

Legacy names from the original ``.env`` are still accepted as aliases so
no env file edit is required when this module ships:

    IB_HOST           → IBKR_HOST
    IB_PORT           → IBKR_PORT
    IB_CLIENT_ID      → IBKR_CLIENT_ID
    IB_PAPER_TRADING  → IBKR_MODE  (mapped True/1 → "paper")
    UW_API_KEY        → UNUSUAL_WHALES_API_KEY
    DEEPSEEK_PRO_MODEL  → DEEPSEEK_DEEP_MODEL
    DEEPSEEK_FAST_MODEL → DEEPSEEK_QUICK_MODEL

Fail-fast: required keys missing raises during settings construction. Live mode
(``IBKR_MODE="live"``) additionally refuses to start unless
``I_UNDERSTAND_LIVE_AUTONOMOUS_TRADING=1`` is set — a deliberate second gate so
real-money autonomy is never one env flag away.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_ROOT = Path(__file__).resolve().parents[2]  # c:\Projects\Agentic Edge


def _populate_aliases() -> None:
    """Pre-populate canonical env names from legacy aliases, in-place.

    Pydantic Settings supports validation aliases but requires per-field
    declarations and doesn't normalise across the whole module. Doing the
    rewrite here means the rest of the code only ever sees canonical
    names.

    Done before BaseSettings reads os.environ so canonical names win.
    """
    aliases = {
        "IBKR_HOST":              "IB_HOST",
        "IBKR_PORT":              "IB_PORT",
        "IBKR_CLIENT_ID":         "IB_CLIENT_ID",
        "UNUSUAL_WHALES_API_KEY": "UW_API_KEY",
        "DEEPSEEK_DEEP_MODEL":    "DEEPSEEK_PRO_MODEL",
        "DEEPSEEK_QUICK_MODEL":   "DEEPSEEK_FAST_MODEL",
    }
    for canonical, legacy in aliases.items():
        if canonical not in os.environ and legacy in os.environ:
            os.environ[canonical] = os.environ[legacy]

    # IB_PAPER_TRADING is bool-like ("True"/"1"); IBKR_MODE is "paper" / "live".
    if "IBKR_MODE" not in os.environ and "IB_PAPER_TRADING" in os.environ:
        legacy = os.environ["IB_PAPER_TRADING"].strip().lower()
        os.environ["IBKR_MODE"] = "paper" if legacy in ("1", "true", "yes", "on") else "live"


# Best-effort .env load before BaseSettings reads. dotenv is preferred;
# fall back to the manual parser used by tradingagents/config_pro.
def _load_env_file() -> None:
    candidates = [
        _ROOT / ".env",
        Path.cwd() / ".env",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            from dotenv import load_dotenv  # type: ignore
            # override=True: the local .env is the source of truth. A stale
            # OS/Machine-scope var (e.g. a rotated-out API key) must NOT shadow
            # the freshly-edited .env — that footgun silently kept a burned FMP
            # key alive. .env wins.
            load_dotenv(path, override=True)
            return
        except ImportError:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip().upper()
                v = v.strip().strip('"').strip("'")
                if k:                       # .env authoritative — override OS env
                    os.environ[k] = v
            return


_load_env_file()
_populate_aliases()


IbkrMode = Literal["paper", "live"]


class Settings(BaseSettings):
    """The single Settings instance the rest of the app reads.

    Use ``get_settings()`` (cached) so multiple imports don't re-read
    the env. Tests can override individual fields via dependency
    injection.
    """

    model_config = SettingsConfigDict(
        env_file=None,          # we load it ourselves above
        case_sensitive=False,
        extra="ignore",
    )

    # ----- LLM ---------------------------------------------------------
    LLM_PROVIDER: str = "deepseek"
    DEEPSEEK_API_KEY: str = Field(..., min_length=10)
    DEEPSEEK_BASE_URL: Optional[str] = "https://api.deepseek.com/v1"
    DEEPSEEK_DEEP_MODEL: str = "deepseek-reasoner"
    DEEPSEEK_QUICK_MODEL: str = "deepseek-chat"

    # ----- Data providers ---------------------------------------------
    POLYGON_API_KEY: Optional[str] = None
    UNUSUAL_WHALES_API_KEY: Optional[str] = None
    FMP_API_KEY: Optional[str] = None
    ALPHA_VANTAGE_API_KEY: Optional[str] = None

    # ----- Strategy mode ----------------------------------------------
    # LEAPS_ONLY: long-dated CALL LEAPs only — long only, no short-call
    # underwriting, no PMCC combos, no stock fallback, no multi-leg. A
    # streamlined directional book. When False (default) the system runs the
    # full PMCC (long LEAP + short call) + stock-fallback strategy.
    LEAPS_ONLY: bool = False
    # LEAP entry execution (institutional): IBKR Adaptive algo working the
    # order toward mid, capped at mid + LEAP_ENTRY_CAP_PCT × half-spread
    # (near mid — won't pay through). Priority: Patient|Normal|Urgent.
    # Fraction of the half-spread the Adaptive algo may work above mid before
    # abandoning. 0.30 was too tight to ever fill: on 2026-08-17 all five orders
    # walked to their cap unfilled, because 30% of a half-spread is well under
    # 1% of premium on contracts trading at $90-$500 with wide LEAP spreads —
    # GLW got $0.58 of room on a $90.92 mid, STX $2.24 on $505.45. 0.50 matches
    # what the sell-to-close path already uses and is still under half the
    # spread; the walker still abandons rather than paying through it.
    #
    # 0.50 was STILL not enough: MU abandoned at cap $548.09 against a $544.17
    # mid — $3.92 of room, 0.72% of premium — because the cap is a fraction of
    # the HALF-spread, so on a ~2.9%-wide LEAP, half of half is under 1%. 13 of
    # 14 orders abandoned unfilled across the 2026-08-17 session.
    #
    # 1.0 = willing to pay up to the far touch (mid + a full half-spread = the
    # ask). This IS a real recurring cost — roughly half the spread per entry —
    # and it is the price of actually building the book instead of papering the
    # tape with orders that never fill. Two things still bound it: the Adaptive
    # algo works toward mid first and only pays up if it must, and
    # submit_single_leg_option passes fair_value_ceiling = the ask, so it can
    # never pay THROUGH the offer.
    #
    # Lower back toward 0.5 once fills are landing, to recover the spread.
    LEAP_ENTRY_CAP_PCT: float = 1.0
    # Once the system has DECIDED to enter, execution is the algo's job and the
    # spread is a COST OF THE POSITION, not a reason to walk away. This is a
    # multi-year book: half a spread paid once is amortised over the whole hold,
    # while an abandoned order means no position at all. 13 of 14 orders on
    # 2026-08-17 abandoned over spread width — the expensive outcome was not the
    # spread, it was the empty book.
    #
    # 'Urgent' lets IBKR's Adaptive algo cross promptly instead of working
    # patiently toward mid and expiring unfilled. Combined with a cap of 1.0
    # (mid + a full half-spread = the ask) and fair_value_ceiling = the ask, the
    # order pays the offer if it must but can never pay THROUGH it.
    LEAP_ENTRY_ADAPTIVE_PRIORITY: str = "Urgent"
    # Abandon if the mid drifts this far from where it sat at construction.
    # None = never — a decided entry should not be cancelled because the name
    # moved while we were queuing for it. (ExecutionConfig defaults this to
    # 0.05, a second abandon trigger the entry path never set explicitly.)
    LEAP_ENTRY_MID_DRIFT_ABANDON_PCT: Optional[float] = None

    # ----- Adaptive entry execution timing ----------------------------
    # Nothing here is a fixed constant applied to every contract: the values are
    # DERIVED per order from the live quote, because a 0.4%-wide megacap LEAP
    # and a 6%-wide small-cap LEAP need different patience and different step
    # sizes. A single hardcoded 180s / 5c pair suited neither.
    #
    # timeout = BASE + spread_pct x PER_SPREAD, clamped to MAX.
    #   0.5% spread -> 120 + 15  = 135s
    #   3.0% spread -> 120 + 90  = 210s
    #   8.0% spread -> 120 + 240 = 360s
    LEAP_ENTRY_TIMEOUT_BASE_SEC: float = 120.0
    LEAP_ENTRY_TIMEOUT_PER_SPREAD_SEC: float = 3000.0
    LEAP_ENTRY_TIMEOUT_MAX_SEC: float = 600.0
    # Walk step as a fraction of the spread rather than a fixed 5c: on a $2
    # spread 5c is a reasonable step, on a $16 spread it is 32 no-op crawls.
    # Floored at the 5c exchange minimum tick for options over $3.
    LEAP_ENTRY_STEP_PCT_OF_SPREAD: float = 0.10
    LEAP_ENTRY_MIN_STEP_CENTS: int = 5
    LEAP_ENTRY_WALK_INTERVAL_SEC: float = 5.0

    # ----- Manager conviction → entry sizing tilt ---------------------
    # Names that tracked legendary investors hold (cross-fund confirmed) get
    # a bounded size BOOST — never a gate, never a block (favor attempting).
    # Neutral (1.0) for untracked names, so behaviour is unchanged for them.
    # The boost lifts the %-of-NAV target but stays under the absolute $ cap.
    MANAGER_CONVICTION_ENABLED: bool = True
    MANAGER_CONVICTION_MAX_FACTOR: float = 1.30   # cap: 4+ confirming managers

    # Clustered OPPORTUNISTIC insider BUYING (>=2 officers/directors/10%-owners
    # making open-market 'P' purchases in 30d) is a high-conviction accumulation
    # signal. Like manager conviction it's a bounded BOOST only, never a gate.
    INSIDER_BUY_CONVICTION_ENABLED: bool = True
    INSIDER_BUY_CONVICTION_FACTOR: float = 1.15   # applied when a cluster is present

    # ----- Bearish institutional overlay (notable-short detection) ----
    # A notable short-seller's position is CONTEXT, not a standalone signal — a
    # single bear (e.g. Burry short SOXX) is often early/wrong, so it must not
    # gate entries or trigger exits by itself. It is logged on pullback-adds
    # (never a veto) and, on the exit side, only AMPLIFIES exit pressure when the
    # name ALREADY shows other bearish signals (theme deterioration / exhaustion
    # / rotation) — confirmation-only, never an independent guardrail pillar.
    # Sources: news/operator registry (bearish.NOTABLE_SHORTS) + tier="bear"
    # managers' 13F put positions.
    NOTABLE_SHORT_TRACKING_ENABLED: bool = True
    NOTABLE_SHORT_EXIT_DELTA: float = 10.0   # confirmation amplifier, not a driver
    NOTABLE_SHORT_NEWS_TTL_DAYS: int = 21    # how long a news-sourced short stays "live" context
    # Fresh institutional selling on a HELD name (recent tier-1/2 13F trim/exit
    # or a 13D/G stake reduction) — same confirmation-only discipline as the
    # notable-short overlay: amplifies existing weakness, never an independent
    # pillar, never fires on an otherwise-healthy name.
    INSTITUTIONAL_SELL_EXIT_DELTA: float = 8.0
    INSIDER_BUY_MIN_USD: float = 200_000          # 30d cluster $ floor

    # ----- SEC EDGAR (Hedge Fund Signal Tracker) ----------------------
    # SEC requires a descriptive User-Agent with a contact email or it 403s
    # every request. With this unset the EDGAR poller no-ops (logs a warning)
    # rather than hammering SEC anonymously. Free — no API key, just the UA.
    EDGAR_USER_AGENT_EMAIL: Optional[str] = None
    EDGAR_POLL_ENABLED: bool = True

    # ----- Phase 3 chokepoint + bearish news layer -------------------
    # Sweeps news for chokepoint AND short/bearish items across the whole theme
    # universe. Two sources: IBKR's free feed (best-effort) and FMP's news API
    # (real coverage, dynamic per-symbol). NEWS_FMP_ENABLED gates the FMP source
    # (needs FMP_API_KEY); without it the sweep falls back to IBKR only.
    NEWS_SWEEP_ENABLED: bool = True
    NEWS_FMP_ENABLED: bool = True

    # ----- Theme Rotation Detector ------------------------------------
    # Flags a theme as "rotating out" when ROTATION_MIN_SIGNALS of {RS
    # breakdown, options-flow distribution, breadth deterioration} agree.
    # On a flagged theme: halt new entries + take profit on winners +
    # tighten exit-pressure sensitivity. Low-regret — never dumps a loser.
    ROTATION_DETECTOR_ENABLED: bool = True
    ROTATION_MIN_SIGNALS: int = 2               # require confirmation
    # Freshness ceiling on a persisted rotation flag. Rotation is a read of
    # where money is moving NOW, so a stale flag is wrong evidence, not weak
    # evidence — after downtime it halts entries on conditions that already
    # reversed (2026-08-17: 9-day-old flags blocked 17 candidates on an
    # accumulation day). Older rows are ignored (fail-open); the sweep runs
    # every 30 min during RTH, so a real rotation is re-flagged within a tick.
    # Sized to cover an intraday gap but NOT an overnight/weekend one.
    ROTATION_MAX_AGE_HOURS: float = 6.0
    # A rotation call must rest on evidence of INSTITUTIONS MOVING, not on price
    # alone. rs_breakdown and breadth_deterioration are both pure price/trend
    # reads over overlapping names, so any ordinary 3-5% pullback trips both and
    # a "2-of-3" rule flags the theme. Requiring at least one institutional
    # signal (options-flow distribution / 13F-13D selling / bearish news) is what
    # separates "this dipped" from "money is leaving".
    ROTATION_REQUIRE_INSTITUTIONAL: bool = True
    # Rotation is a multi-day phenomenon. Demand the same call on N consecutive
    # sweeps before acting, so a single noisy reading can never halt entries.
    ROTATION_CONFIRM_SWEEPS: int = 2
    # Fraction of a theme's names showing fresh institutional selling (tracked
    # 13F trim/exit or a 13D/G reduction) for the signal to trip.
    ROTATION_INSTITUTIONAL_SELL_FRAC: float = 0.25
    # Fraction of a theme's names carrying bearish news in the lookback window.
    ROTATION_NEWS_BEARISH_FRAC: float = 0.25
    ROTATION_NEWS_LOOKBACK_DAYS: int = 7
    ROTATION_BREADTH_BELOW_MA_PCT: float = 0.60  # breadth-signal trip threshold
    # Exit-pressure delta injected for a held name in a rotating theme. Maps
    # via the rotation subscore (25 -> max subscore, weight 0.15 ≈ +15 pts of
    # composite) — tightens the existing signal, never forces a drawdown exit.
    ROTATION_EXIT_PRESSURE_DELTA: float = 25.0

    # ----- Morning brief -> trading decisions -------------------------
    # The brief was designed when execution was manual: the operator read it and
    # traded. Now that entries are automated, its judgment has to reach the
    # deciding agent or it is just a newsletter. Two things in it have no other
    # source in the trading path:
    #
    #   posture   — a 0-100 risk-appetite dial built from the system's OWN
    #               signals (theme health, rotation calm, buy breadth), capped
    #               at 20 when the entry breaker is latched. The entry loop has
    #               macro (VIX/SPX) and the intraday pulse, but nothing that
    #               reads the health of its own signal set.
    #   idea read — street consensus upside, 30-day analyst grade momentum, and
    #               a deterministic institutional lean per candidate. No analyst
    #               data reaches the trading path at all today.
    #
    # Both are bounded SIZING TILTS, never gates — consistent with the standing
    # policy that eligibility stays loose and the walker protects the price.
    # The brief is built once at 08:45 ET and PERSISTED; the loops read that
    # stored row all day rather than rebuilding it (it makes provider calls).
    MORNING_BRIEF_WIRED: bool = True
    # Posture 50 = neutral = 1.0x. 100 -> 1+tilt, 0 -> 1-tilt.
    MORNING_POSTURE_MAX_TILT: float = 0.25
    # Per-symbol tilt from analyst upside + grade momentum + institutional lean.
    MORNING_IDEA_MAX_TILT: float = 0.15
    # The brief also computes a 0-100 PERFECT ENTRY SCORE per idea — EMA-stack
    # alignment, pullback-vs-extended, volume contraction, pending breakout,
    # minus penalties for distribution days and failed breakouts. Nothing in the
    # trading path read it: the loop ranked purely on the research composite, so
    # on 2026-08-17 it bought SNDK at "Entry 46/100 - not ready, extended 20%
    # above the 8 EMA" while ETN sat unbought at "Entry 82/100 - pullback into
    # the 8/21 EMA zone, buyable dip".
    #
    # Composite and entry score answer different questions and both matter:
    # the composite says WHAT is worth owning, the entry score says WHETHER now
    # is a sane moment to buy it. For a multi-year builder, paying up 20% above
    # the 8 EMA is a worse sin than waiting a session.
    #
    # Wired two ways, neither a gate (a poor setup still gets bought, smaller
    # and later — consistent with favouring attempting):
    #   rank  — candidates ordered by a blend, so better setups are processed
    #           first while the day's capacity is still free.
    #   size  — bounded tilt: 50 neutral, 100 -> 1+tilt, 0 -> 1-tilt.
    MORNING_ENTRY_SCORE_WIRED: bool = True
    MORNING_ENTRY_SCORE_MAX_TILT: float = 0.20
    # Share of the ranking blend given to the entry score (rest to the
    # composite). 0.4 keeps thesis primary while letting timing break ties.
    MORNING_ENTRY_RANK_WEIGHT: float = 0.4
    # Freshness ceiling, same discipline as the rotation flags: a stale brief
    # describes a market that has moved on. Older than this -> neutral 1.0, so
    # a missed 08:45 run degrades to "no opinion" rather than yesterday's.
    # 30h covers a normal overnight gap but never a weekend.
    MORNING_BRIEF_MAX_AGE_HOURS: float = 30.0

    # ----- Quant Research Factory (decision-support, never a gate) -----
    # Point-in-time feature store + IC/alpha-decay research harness. The
    # nightly snapshot writes one feature row per theme symbol; the labeler
    # backfills forward returns; the harness measures which signals predict
    # returns and how fast their edge decays. Off-switch only — research-only,
    # no entry/exit gate reads it. See research/quant_factory.md.
    FEATURE_FACTORY_ENABLED: bool = True

    # Quant overlay: autonomously inject the research factory's per-symbol
    # signals into the scorecard's per-ticker LLM context so the AI's own
    # Buy/Hold/Avoid + conviction (which drive entry ranking and sizing) factor
    # in centrality, smart-money, dark-pool, momentum, flow, and personas. Signal
    # weights self-tune from a theory prior toward measured IC — no human gate.
    # Off-switch only.
    QUANT_OVERLAY_ENABLED: bool = True
    # Max magnitude (exit-pressure points) the quant overlay may shift an open
    # position's exit pressure: a strong name holds longer (−), a weak one
    # trims sooner (+). Kept small (< the rotation delta of 25) so the quant
    # signal informs but never single-handedly forces a drawdown exit.
    QUANT_EXIT_MAX_DELTA: float = 15.0   # modest bump 2026-06-30 — more say in trim/close
    # Max bidirectional entry SIZE tilt the quant overlay may apply: a
    # structurally strong candidate (high edge) sizes up to (1+tilt)×, a weak one
    # down to (1-tilt)×, neutral = 1.0. Applied before the absolute $ cap so a
    # boost can never breach PMCC_MAX_DOLLARS. Symmetric to manager-conviction;
    # never a gate — quant tilts size, it doesn't block an entry. This is the
    # entry-side wiring of the research factory's edge (2026-06-30).
    QUANT_ENTRY_MAX_TILT: float = 0.15
    # While the overlay weights are still the cold-start theory PRIOR (no
    # forward-return labels matured yet → IC un-measured), scale the quant
    # edge's influence by this factor so 100%-prior weights don't drive entry
    # ranking / exit nudges with the same authority as measured edge. Returns to
    # full strength (1.0) automatically once the weights become 'ic_blended'.
    QUANT_PRIOR_SHRINK: float = 0.5

    # Give the LEAP book the graded unified Exit Pressure Score (theme
    # deterioration + technical exhaustion + rotation + quant edge) the stock
    # path has — restoring per-tick position_pressure observability and putting
    # the quant signal into the EXIT decision. Records every tick; on the top
    # 'aggressive' band it FLAGS the LEAP for close (alert + operator-confirm,
    # never an auto-dump — kind 'exit_pressure' is not an auto-close kind).
    LEAP_GRADED_EXIT_ENABLED: bool = True

    # Autonomous execution of the graded LEAP exit-pressure (operator policy
    # 2026-06-30): the score now ACTS, not just flags. `aggressive` (>75) auto
    # full-closes the LEAP; `trim_heavy` (60-75) auto-trims LEAP_TRIM_HEAVY_PCT
    # of the contracts to de-risk. GUARDRAIL: only fires with multi-signal
    # agreement (>=2 of {theme deterioration, technical exhaustion incl. RSI,
    # rotation, quant edge}), so a single noisy pillar — and raw price drawdown,
    # which is not even a pillar here — can never auto-dump. Set False to revert
    # to flag-and-confirm instantly. Trims are deduped to at most once/name/day
    # and share the AUTO_MAX_CLOSES_PER_DAY cap with full closes.
    LEAP_AUTO_EXIT_ENABLED: bool = True
    LEAP_TRIM_HEAVY_PCT: float = 0.33   # fraction of contracts sold on a trim_heavy de-risk
    # Catastrophic slow-bleed STOP — the 100%-loss backstop for a long call.
    # A LEAP whose premium has fallen this far from entry AND whose underlying is
    # below its 200-day MA (structural downtrend) is auto-closed: that combination
    # is a broken thesis, not a one-day beta drop, so it does not violate the
    # no-drawdown-dumps rule. Set high enough to never fire on normal volatility.
    LEAP_CATASTROPHIC_STOP_PCT: float = 0.65

    # ----- Observability ----------------------------------------------
    # Persist logs to a rotating file (in addition to the console). Without
    # this, backend logs live only in the uvicorn console window and vanish on
    # restart — making post-hoc audits of what the autonomous loops did
    # impossible. Off by setting LOG_DIR="".
    LOG_DIR: str = "logs"
    LOG_LEVEL: str = "INFO"
    LOG_MAX_BYTES: int = 10_000_000      # 10 MB per file
    LOG_BACKUP_COUNT: int = 14           # ~2 weeks of rotated history

    # ----- Entry-loop signal freshness --------------------------------
    # The entry loop acts on the LATEST completed theme run per theme within
    # this lookback window (not strictly "today"), so a late or recovered daily
    # run still yields actionable candidates instead of a zero-entry day. Recent
    # per-symbol attempts/intents within the window still de-dup, so widening it
    # does not cause re-entry churn.
    ENTRY_RUN_LOOKBACK_HOURS: int = 30
    # Wait before re-attempting a name whose walking-limit ABANDONED (walked to
    # its price cap unfilled — price discipline, not a thesis rejection) or that
    # probed INELIGIBLE. Other outcomes (filled, error) never retry.
    #
    # 0 = no cooldown: retry on the very next tick the name still qualifies.
    # Set to 0 (2026-08-17) — a two-hour lockout on a name the scorer still
    # ranks Buy is a restriction, not a safeguard, and it was compounding the
    # entry drought: MU and SNDK probed ineligible at 09:32 on a threshold that
    # has since been lowered, and could not be re-tried for the rest of the
    # session even after the threshold moved.
    #
    # Retries are still naturally paced without it — the entry loop ticks every
    # 60s and a LEAP entry walks for up to 180s, so a name re-attempts roughly
    # every 4 minutes rather than continuously. The cost of a retry is one
    # option-chain probe; raise this if that probe volume ever pressures the
    # broker's market-data farm (the 10197 competing-session error is the
    # symptom to watch).
    ENTRY_ABANDON_RETRY_COOLDOWN_MIN: int = 0
    ENTRY_MAX_ORDER_ATTEMPTS_PER_DAY: Optional[int] = None   # None = retry a name as often as it re-qualifies
    # Correlation-aware sizing: a candidate highly correlated to the EXISTING
    # book adds concentration, not diversification — "you own 20 stocks but 5
    # bets". Sizes DOWN (0.5× / 0.75×), never blocks: a haircut, not a gate.
    # Uses 90d daily-return correlation vs currently-open names; fail-open 1.0.
    ENTRY_CORR_HAIRCUT_ENABLED: bool = True
    ENTRY_CORR_HIGH: float = 0.80    # avg corr ≥ this → 0.5× size
    ENTRY_CORR_MED: float = 0.65     # avg corr ≥ this → 0.75× size
    # Live tape gate: the intraday pulse (universe breadth vs the broad tape)
    # acts on NEW BUYING only — the complex-specific analogue of the macro
    # regime. distribution day → halt new entries this tick; money rotating
    # OUT of the complex → half size. NEVER touches exits: an intraday
    # "distribution" read must not become a drawdown-exit trigger.
    PULSE_ENTRY_GATE_ENABLED: bool = True
    PULSE_OUT_OF_SEMIS_SIZING: float = 0.5

    # ----- Execution (IBKR) -------------------------------------------
    IBKR_HOST: str = "127.0.0.1"
    IBKR_PORT: int = 7497
    IBKR_CLIENT_ID: int = 1
    IBKR_MODE: IbkrMode = "paper"

    # ----- Persistence ------------------------------------------------
    DATABASE_URL: str = "sqlite+aiosqlite:///./agentic_edge.db"
    REDIS_URL: Optional[str] = None
    PROVIDER_CACHE_DIR: Optional[str] = None

    # ----- Toggles ----------------------------------------------------
    USE_MOCK_RUN: bool = False
    MOCK_DATA: bool = False
    # MUST include the port start-all.ps1 actually serves the dashboard on.
    # It launches `npm run dev -- -p 3001`, while this defaulted to :3000 only —
    # so Next.js served the page fine and then EVERY browser call to the API was
    # rejected with "400 Disallowed CORS origin". The dashboard rendered as an
    # empty shell (no positions, no equity, no runs), which reads as a frontend
    # that won't start rather than a CORS mismatch.
    # 3000 is kept for a plain `npm run dev`, and the 127.0.0.1 forms because a
    # browser treats them as different origins from localhost.
    CORS_ALLOWED_ORIGINS: str = (
        "http://localhost:3001,http://127.0.0.1:3001,"
        "http://localhost:3000,http://127.0.0.1:3000"
    )

    # ----- Automation (deploy-time half of the dual kill switch) ------
    # Always defaults to False. Flipping requires both this env var AND
    # the runtime DB row ``system_state.autotrade_enabled``.
    AUTOTRADE_ENABLED: bool = False
    ADMIN_API_TOKEN: Optional[str] = None      # required for kill-switch endpoints

    # ----- Alerting ---------------------------------------------------
    SLACK_WEBHOOK_URL: Optional[str] = None
    ALERT_EMAIL_TO: Optional[str] = None

    # ----- Entry circuit breaker (account-level, halts NEW entries only) --
    # Never closes positions — high-beta exits stay signal-driven. The
    # breaker latches until manually re-armed via the admin endpoint.
    BREAKER_ENABLED: bool = True
    # Halt new entries if intraday NetLiq falls this fraction below the day's
    # opening NAV. Account-level capital discipline ("stop adding on a bad
    # day"), NOT a per-position stop. High-beta books swing hard, so keep
    # this generous — 0.12 = 12% account drawdown intraday.
    BREAKER_INTRADAY_NAV_DROP_PCT: float = 0.12
    # Halt new entries when the available-funds cushion (AvailableFunds /
    # NetLiquidation) drops below this — don't pile on risk when margin is
    # tight. 0.10 = require at least 10% free.
    BREAKER_MIN_MARGIN_CUSHION_PCT: float = 0.10

    # ----- Entry caps (capped momentum pyramiding) --------------------
    # Adds to a name that keeps confirming are ALLOWED, but bounded so the
    # book never over-concentrates or runs to the breaker's margin floor
    # (both happened on the first autonomous open). All three are pre-submit
    # checks in the LEAPS entry path; failing one records an `open_leap_capped`
    # audit row and skips the entry (it does NOT trip the breaker).
    #
    # Max total exposure to a single underlying as a fraction of NAV — caps
    # pyramiding (held + proposed premium). 0.15 ≈ allows ~one add on a
    # normal ~7%-of-NAV starter before it's capped.
    # ------------------------------------------------------------------
    # UNCAPPED DEPLOYMENT (operator decision, 2026-08-17)
    #
    # Every entry-side limit below is set to None = NO LIMIT, on the operator's
    # explicit instruction. Read this before changing any of them back.
    #
    # What this means concretely for a LEAPS-only (long-call) book: premium paid
    # IS the maximum loss. Long calls are fully paid, so there is no margin loan,
    # no margin call, and therefore NO broker-side mechanism that halts the book
    # at any level of loss. Buying power does not constrain this: IBKR reports
    # ~4x NAV of Reg-T buying power, but that is stock-margin capacity and can
    # never be spent on premium — the spendable figure is AvailableFunds.
    #
    # With these off, nothing bounds single-name concentration, per-theme
    # concentration, or total premium at risk. All themes in this universe are
    # one correlated AI/compute supply-chain graph, so a systemic drawdown is
    # not diversified away by holding many names within it.
    #
    # Still in force (deliberately NOT part of this change):
    #   * the dual kill switch (env + DB)
    #   * the entry circuit breaker — intraday NAV drop / margin cushion / blind
    #     broker; halts NEW entries only, never closes positions
    #   * macro regime sizing (panic -> sizing_factor 0 blocks entries)
    #   * rotation, tape and sector-regime entry gates
    #   * NAV fail-closed: an unreadable NAV still sizes to zero
    # ------------------------------------------------------------------
    ENTRY_MAX_NAME_PCT_OF_NAV: Optional[float] = None
    # Free-funds (dry-powder) cushion to REMAIN after an entry. For a fully-paid
    # LONG-options book this is a deployment/reserve policy, NOT a margin-safety
    # line — long calls can't be margin-called, and the real safety floor is the
    # circuit breaker's MAINTENANCE-margin cushion (BREAKER_MIN_MARGIN_CUSHION_PCT,
    # a different metric that stays ~100% here). Operator set 0.10 (max deploy,
    # 2026-07-01) — total premium-at-risk is still bounded by the aggregate
    # (AUTO_MAX_GROSS_PREMIUM_PCT_NAV), per-theme, and per-name caps.
    ENTRY_MIN_MARGIN_CUSHION: Optional[float] = None   # None = no free-funds floor
    # AGGREGATE exposure ceiling: total open LEAP premium (= total max-loss for a
    # long-call book) across ALL names as a fraction of NAV. The per-name cap
    # bounds single-name blowups; THIS bounds the whole correlated book so a
    # systemic AI-compute drawdown can't take the entire account. 1.0 = never
    # hold more premium-at-risk than NAV. Checked before every entry, fails
    # closed on an unreadable NAV/positions.
    AUTO_MAX_GROSS_PREMIUM_PCT_NAV: Optional[float] = None   # None = no aggregate premium ceiling
    # Per-THEME concentration: max open premium in any one theme as a fraction of
    # NAV. Themes are highly correlated (one AI/compute supply-chain graph), so
    # this stops a dozen 15%-of-NAV names in one theme becoming a single 100%-of-
    # NAV bet. 0.40 = ≤40% of NAV in any single theme.
    AUTO_MAX_THEME_PREMIUM_PCT_NAV: Optional[float] = None   # None = no per-theme ceiling
    # Minimum scorecard composite required to auto-enter. A weak "Buy" (e.g. a
    # degraded/noisy run) must not auto-trade real money identically to a strong
    # one. Sizing already scales with conviction; this is the floor below which
    # we don't participate at all. 0 disables the floor.
    ENTRY_MIN_COMPOSITE: float = 6.0
    # Pullback-add ("buy support"): average into a still-favored theme name when
    # it dips to SMA20/SMA50 support on ORDERLY (not distribution) volume with RSI
    # holding — but ONLY while the theme is in-contact AND no tracked hedge fund is
    # rotating out. All normal caps (per-name 15%, aggregate, per-theme, margin,
    # daily) still apply, and it's deduped to at most one add/name/day. Off-switch.
    PULLBACK_ADD_ENABLED: bool = True
    PULLBACK_ADD_MA_PROXIMITY_PCT: float = 0.05   # within 5% of SMA20/50 = "at support"
    PULLBACK_ADD_MIN_DIP_PCT: float = 0.05        # >=5% off the 20-day high (a real pullback)
    # RSI band for a healthy uptrend pullback: above the floor = not a breakdown;
    # below the ceiling = not overbought. 40-68 is the "dip in a strong trend"
    # sweet spot — a name AT support with RSI ~55-65 pulled back but held strength
    # (the earlier 38-58 ceiling wrongly excluded exactly those buy-the-dip setups).
    PULLBACK_ADD_RSI_MIN: float = 40.0
    PULLBACK_ADD_RSI_MAX: float = 68.0
    PULLBACK_ADD_MAX_VOLUME_RATIO: float = 1.6    # dip must be orderly, not a >1.6x distribution day
    # Reject LEAP entries whose bid/ask spread exceeds this fraction of mid —
    # illiquid LEAPs fill far from fair value (the CRDO overpay). 0.15 = skip
    # if the spread is wider than 15% of mid.
    ENTRY_MAX_LEAP_SPREAD_PCT: float = 0.15

    # ----- Earnings-miss crash detector (high-beta-aware exit) ----------
    # The ONE exit trigger that's allowed to fire on a sharp drop — but it is
    # GATED on earnings proximity so it can NEVER trip on a normal high-beta
    # down day (those have no earnings event attached). It arms only within
    # EARNINGS_BREAK_SESSIONS sessions of a report AND only on a move worse
    # than EARNINGS_BREAK_DROP_PCT. A routine -8% beta day → not near earnings
    # → ignored. A -25% earnings-miss crash (the AVGO case) → flagged for close.
    # It FLAGS + alerts (operator-confirmed close), not a naked auto-dump.
    EARNINGS_BREAK_SESSIONS: int = 2
    EARNINGS_BREAK_DROP_PCT: float = 0.12

    # ----- Limits / governance ----------------------------------------
    # MAX_RUNS_PER_USER_PER_HOUR was sized for the slow LangGraph pipeline
    # (one run took 30+ min, so 5/hr made sense). FastThemeRunner runs
    # in 30-60 sec, so we cap at 120/hr (= one full universe of 20 themes
    # six times an hour). Override via env if needed.
    MAX_RUNS_PER_USER_PER_HOUR: int = 120
    MAX_THEMES_PER_USER: int = 30
    MAX_TICKERS_PER_THEME: int = 50

    # ------------------------------------------------------------------
    # Validators / fail-fast
    # ------------------------------------------------------------------

    @field_validator("IBKR_MODE")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        # Paper is the default and what we recommend until the framework
        # has been validated against your strategy + capital. Live is
        # allowed but logged loudly at startup so it can't go silent.
        v = (v or "paper").lower()
        if v not in ("paper", "live"):
            raise ValueError(
                f"IBKR_MODE={v!r}: must be 'paper' or 'live'."
            )
        if v == "live":
            # LIVE-ARMING FRICTION: real-money autonomy must not be one env flag
            # away. Require an explicit second acknowledgment or refuse to start,
            # so IBKR_MODE=live can never be flipped on accidentally.
            import os
            ack = os.getenv("I_UNDERSTAND_LIVE_AUTONOMOUS_TRADING", "").strip().lower()
            if ack not in ("1", "true", "yes"):
                raise ValueError(
                    "IBKR_MODE=live requires explicit acknowledgment: set "
                    "I_UNDERSTAND_LIVE_AUTONOMOUS_TRADING=1 to arm REAL-MONEY autonomous "
                    "trading. Refusing to start otherwise."
                )
            import logging
            logger = logging.getLogger("agentic_edge.config")
            logger.warning(
                "=" * 64
                + "\n IBKR_MODE=live — REAL-MONEY autonomous trading ARMED."
                + "\n Connect to Gateway on port 4001 (live) with a U-prefix"
                + "\n account ID. Paper port is 4002, paper account starts D."
                + "\n" + "=" * 64
            )
        return v

    @field_validator("ADMIN_API_TOKEN")
    @classmethod
    def _no_default_admin_token(cls, v: Optional[str]) -> Optional[str]:
        # Refuse the canned dev placeholder. The kill-switch + reconcile
        # endpoints are real-money-impacting paths; an unset or default
        # token should fail-fast at startup, not silently leak the
        # endpoints for anyone on the local network to hit.
        if v in ("secret-replace-me", "changeme", "admin"):
            raise ValueError(
                f"ADMIN_API_TOKEN={v!r} is a placeholder. Set a real value "
                f"(generate with: python -c \"import secrets; print(secrets.token_urlsafe(32))\")."
            )
        return v

    @model_validator(mode="after")
    def _shape(self) -> "Settings":
        # When running with real data, demand that at minimum DeepSeek
        # plus enough providers for the four wired analysts are present.
        # Macro is treated as required because the architecture's analysts
        # ground their reasoning on it. The actual graceful-fallback
        # behavior (use yfinance when Polygon missing) is handled at the
        # provider layer; this gate is for production clarity.
        if not self.USE_MOCK_RUN and not self.MOCK_DATA:
            missing = [
                k for k, v in {
                    "DEEPSEEK_API_KEY":       self.DEEPSEEK_API_KEY,
                    "POLYGON_API_KEY":        self.POLYGON_API_KEY,
                    "UNUSUAL_WHALES_API_KEY": self.UNUSUAL_WHALES_API_KEY,
                    "FMP_API_KEY":            self.FMP_API_KEY,
                    "ALPHA_VANTAGE_API_KEY":  self.ALPHA_VANTAGE_API_KEY,
                }.items() if not v
            ]
            if missing:
                raise ValueError(
                    "Production mode requires these env vars: "
                    + ", ".join(missing)
                    + ". Set USE_MOCK_RUN=1 or MOCK_DATA=1 to run without them."
                )
        return self

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings instance. Read once, used everywhere."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings (test hook)."""
    get_settings.cache_clear()
