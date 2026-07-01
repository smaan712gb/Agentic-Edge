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
            load_dotenv(path, override=False)
            return
        except ImportError:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip().upper()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
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
    LEAP_ENTRY_CAP_PCT: float = 0.30
    LEAP_ENTRY_ADAPTIVE_PRIORITY: str = "Normal"

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
    INSIDER_BUY_MIN_USD: float = 200_000          # 30d cluster $ floor

    # ----- SEC EDGAR (Hedge Fund Signal Tracker) ----------------------
    # SEC requires a descriptive User-Agent with a contact email or it 403s
    # every request. With this unset the EDGAR poller no-ops (logs a warning)
    # rather than hammering SEC anonymously. Free — no API key, just the UA.
    EDGAR_USER_AGENT_EMAIL: Optional[str] = None
    EDGAR_POLL_ENABLED: bool = True

    # ----- Phase 3 chokepoint news layer (free, IB-sourced) -----------
    # Sweeps the account's IBKR news providers for chokepoint-relevant items
    # on portfolio + theme names. No paid feeds. Off-switch only.
    NEWS_SWEEP_ENABLED: bool = True

    # ----- Theme Rotation Detector ------------------------------------
    # Flags a theme as "rotating out" when ROTATION_MIN_SIGNALS of {RS
    # breakdown, options-flow distribution, breadth deterioration} agree.
    # On a flagged theme: halt new entries + take profit on winners +
    # tighten exit-pressure sensitivity. Low-regret — never dumps a loser.
    ROTATION_DETECTOR_ENABLED: bool = True
    ROTATION_MIN_SIGNALS: int = 2               # require confirmation
    ROTATION_BREADTH_BELOW_MA_PCT: float = 0.60  # breadth-signal trip threshold
    # Exit-pressure delta injected for a held name in a rotating theme. Maps
    # via the rotation subscore (25 -> max subscore, weight 0.15 ≈ +15 pts of
    # composite) — tightens the existing signal, never forces a drawdown exit.
    ROTATION_EXIT_PRESSURE_DELTA: float = 25.0

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
    CORS_ALLOWED_ORIGINS: str = "http://localhost:3000"

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
    ENTRY_MAX_NAME_PCT_OF_NAV: float = 0.15
    # Free-funds (dry-powder) cushion to REMAIN after an entry. For a fully-paid
    # LONG-options book this is a deployment/reserve policy, NOT a margin-safety
    # line — long calls can't be margin-called, and the real safety floor is the
    # circuit breaker's MAINTENANCE-margin cushion (BREAKER_MIN_MARGIN_CUSHION_PCT,
    # a different metric that stays ~100% here). Operator set 0.10 (max deploy,
    # 2026-07-01) — total premium-at-risk is still bounded by the aggregate
    # (AUTO_MAX_GROSS_PREMIUM_PCT_NAV), per-theme, and per-name caps.
    ENTRY_MIN_MARGIN_CUSHION: float = 0.10
    # AGGREGATE exposure ceiling: total open LEAP premium (= total max-loss for a
    # long-call book) across ALL names as a fraction of NAV. The per-name cap
    # bounds single-name blowups; THIS bounds the whole correlated book so a
    # systemic AI-compute drawdown can't take the entire account. 1.0 = never
    # hold more premium-at-risk than NAV. Checked before every entry, fails
    # closed on an unreadable NAV/positions.
    AUTO_MAX_GROSS_PREMIUM_PCT_NAV: float = 1.0
    # Per-THEME concentration: max open premium in any one theme as a fraction of
    # NAV. Themes are highly correlated (one AI/compute supply-chain graph), so
    # this stops a dozen 15%-of-NAV names in one theme becoming a single 100%-of-
    # NAV bet. 0.40 = ≤40% of NAV in any single theme.
    AUTO_MAX_THEME_PREMIUM_PCT_NAV: float = 0.40
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
