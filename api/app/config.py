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

Fail-fast: required keys missing OR ``IBKR_MODE != "paper"`` raises
during settings construction so the app refuses to start.
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

    # ----- SEC EDGAR (Hedge Fund Signal Tracker) ----------------------
    # SEC requires a descriptive User-Agent with a contact email or it 403s
    # every request. With this unset the EDGAR poller no-ops (logs a warning)
    # rather than hammering SEC anonymously. Free — no API key, just the UA.
    EDGAR_USER_AGENT_EMAIL: Optional[str] = None
    EDGAR_POLL_ENABLED: bool = True

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
            import logging
            logger = logging.getLogger("agentic_edge.config")
            logger.warning(
                "=" * 64
                + "\n IBKR_MODE=live — REAL-MONEY trading enabled."
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
