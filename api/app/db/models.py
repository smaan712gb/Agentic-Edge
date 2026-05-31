"""ORM models for Agentic Edge.

All ten tables from the production roadmap live here. The schema includes
``user_id`` columns (nullable for now — single-user mode) on every
user-scoped table so the eventual auth wiring (Auth0/Clerk) is a no-op
migration: just backfill and tighten the constraint.

JSON-shaped fields (drivers, risks, configs, payloads) are stored as
JSONB on Postgres / JSON on SQLite via the database-agnostic ``JSON``
column type. pgvector ``embedding`` columns on ``run_events`` and
``ticker_scores`` are added by an Alembic migration only when running
against Postgres; SQLite dev paths skip them gracefully.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    """Short, URL-safe primary keys for entities the UI quotes verbatim.

    Eight hex chars is plenty for the run/theme cardinality this product
    is built for and keeps URLs tidy.
    """
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Themes & symbols
# ---------------------------------------------------------------------------


class Theme(Base):
    __tablename__ = "themes"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_themes_user_id"),
        Index("ix_themes_user_id", "user_id"),
    )

    id:         Mapped[str]             = mapped_column(String(64), primary_key=True)
    user_id:    Mapped[Optional[str]]   = mapped_column(String(64), nullable=True)
    name:       Mapped[str]             = mapped_column(String(120), nullable=False)
    thesis:     Mapped[str]             = mapped_column(Text, nullable=False, default="")
    chokepoint: Mapped[str]             = mapped_column(Text, nullable=False, default="")
    weights:    Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]        = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime]        = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    symbols: Mapped[list["ThemeSymbol"]] = relationship(
        back_populates="theme", cascade="all, delete-orphan", lazy="selectin",
    )
    runs: Mapped[list["Run"]] = relationship(back_populates="theme", lazy="raise")


class ThemeSymbol(Base):
    __tablename__ = "theme_symbols"
    __table_args__ = (
        UniqueConstraint("theme_id", "symbol", name="uq_theme_symbol"),
        Index("ix_theme_symbols_symbol", "symbol"),
    )

    id:        Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    theme_id:  Mapped[str]    = mapped_column(String(64), ForeignKey("themes.id", ondelete="CASCADE"), nullable=False)
    symbol:    Mapped[str]    = mapped_column(String(10), nullable=False)
    weight:    Mapped[float]  = mapped_column(Float, default=1.0, nullable=False)
    notes:     Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    position:  Mapped[int]    = mapped_column(Integer, default=0, nullable=False)
    created_at:Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    theme: Mapped[Theme] = relationship(back_populates="symbols")


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_user_id", "user_id"),
        Index("ix_runs_theme_id_started_at", "theme_id", "started_at"),
        Index("ix_runs_status", "status"),
    )

    id:           Mapped[str]   = mapped_column(String(64), primary_key=True, default=_new_id)
    user_id:      Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    theme_id:     Mapped[str]   = mapped_column(String(64), ForeignKey("themes.id"), nullable=False)
    status:       Mapped[str]   = mapped_column(String(16), default="running", nullable=False)
    progress:     Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    started_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    finished_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    summary:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    best_positioned: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    config:       Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error:        Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    theme:  Mapped[Theme] = relationship(back_populates="runs")
    events: Mapped[list["RunEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan",
        order_by="RunEvent.id", lazy="raise",
    )
    scores: Mapped[list["TickerScore"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="raise",
    )
    report: Mapped[Optional["ThemeReport"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False, lazy="raise",
    )


class RunEvent(Base):
    """Append-only audit of every event emitted during a run.

    The live SSE consumer reads from an in-memory queue (``RUN_QUEUES``)
    for low-latency streaming. This table is the durable replay of the
    same events: a client connecting after a run finished can be served
    a snapshot from here.
    """

    __tablename__ = "run_events"
    __table_args__ = (
        Index("ix_run_events_run_id_id", "run_id", "id"),
    )

    id:        Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id:    Mapped[str]    = mapped_column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    agent_id:  Mapped[str]    = mapped_column(String(48), nullable=False)
    symbol:    Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    status:    Mapped[str]    = mapped_column(String(16), nullable=False)  # started | finished
    summary:   Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    run: Mapped[Run] = relationship(back_populates="events")


class TickerScore(Base):
    __tablename__ = "ticker_scores"
    __table_args__ = (
        UniqueConstraint("run_id", "symbol", name="uq_ticker_scores_run_symbol"),
        Index("ix_ticker_scores_symbol", "symbol"),
    )

    id:                Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id:            Mapped[str]    = mapped_column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    symbol:            Mapped[str]    = mapped_column(String(10), nullable=False)
    setup:             Mapped[float]  = mapped_column(Float, nullable=False)
    options:           Mapped[float]  = mapped_column(Float, nullable=False)
    thesis_fit:        Mapped[float]  = mapped_column(Float, nullable=False)
    composite:         Mapped[float]  = mapped_column(Float, nullable=False)
    decision:          Mapped[str]    = mapped_column(String(16), nullable=False)  # Buy | Hold | Avoid
    conviction:        Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    drivers:           Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    risks:             Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    rationale:         Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    agent_reports:     Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at:        Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    run: Mapped[Run] = relationship(back_populates="scores")


class ThemeReport(Base):
    """Final ScoreReport produced by the Theme Ranker per run."""

    __tablename__ = "theme_reports"

    id:               Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id:           Mapped[str]    = mapped_column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), unique=True, nullable=False)
    theme_id:         Mapped[str]    = mapped_column(String(64), ForeignKey("themes.id"), nullable=False)
    summary:          Mapped[str]    = mapped_column(Text, nullable=False, default="")
    ranking:          Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    best_positioned:  Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    generated_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    run:   Mapped[Run]   = relationship(back_populates="report")


# ---------------------------------------------------------------------------
# Portfolio / paper account
# ---------------------------------------------------------------------------


class Position(Base):
    """Snapshot of an open paper position."""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("user_id", "account_id", "symbol", name="uq_positions_user_account_symbol"),
        Index("ix_positions_user_id", "user_id"),
    )

    id:           Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:      Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    account_id:   Mapped[str]    = mapped_column(String(64), nullable=False)  # IBKR account
    symbol:       Mapped[str]    = mapped_column(String(10), nullable=False)
    qty:          Mapped[float]  = mapped_column(Float, nullable=False)
    avg_price:    Mapped[float]  = mapped_column(Float, nullable=False)
    last_price:   Mapped[float]  = mapped_column(Float, nullable=False, default=0.0)
    pnl:          Mapped[float]  = mapped_column(Float, nullable=False, default=0.0)
    captured_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class EquitySnapshot(Base):
    """One row per trading day, used by the Performance dashboard chart."""

    __tablename__ = "equity_snapshots"
    __table_args__ = (
        UniqueConstraint("user_id", "account_id", "date", name="uq_equity_user_account_date"),
        Index("ix_equity_user_id_date", "user_id", "date"),
    )

    id:          Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:     Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    account_id:  Mapped[str]    = mapped_column(String(64), nullable=False)
    date:        Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity:      Mapped[float]  = mapped_column(Float, nullable=False)
    cash:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notional:    Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class ClosingAccumulationSignal(Base):
    """Daily closing-bell accumulation sweep — one row per (symbol, date).

    Surfaces three look-alike patterns and discriminates between them:
      - real accumulation (VWAP-bid all day + AH follow-through)
      - short covering (rallies into close but AH fades)
      - gamma-pinned ramp (also fades AH)

    The AH-holds-close + theme-confirmation booleans are what separates
    real institutional flow from the look-alikes.
    """

    __tablename__ = "closing_accumulation_signals"
    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_cba_symbol_date"),
        Index("ix_cba_date_confidence", "date", "confidence"),
    )

    id:              Mapped[int]     = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol:          Mapped[str]     = mapped_column(String(10), nullable=False)
    date:            Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    theme_id:        Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("themes.id", ondelete="SET NULL"), nullable=True,
    )
    setup_passes:    Mapped[bool]    = mapped_column(Boolean, nullable=False)
    confidence:      Mapped[str]     = mapped_column(String(8), nullable=False)
    gate1_passes:    Mapped[bool]    = mapped_column(Boolean, nullable=False)
    gate2_passes:    Mapped[bool]    = mapped_column(Boolean, nullable=False)
    theme_confirmed: Mapped[Optional[bool]]  = mapped_column(Boolean, nullable=True)
    last_30m_rvol:   Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    day_rvol:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pct_session_above_vwap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ah_print_count:  Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    ah_cumulative_volume: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ah_holds_close:  Mapped[Optional[bool]]  = mapped_column(Boolean, nullable=True)
    moc_price:       Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vwap:            Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_recommendation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rationale:       Mapped[Optional[str]]   = mapped_column(Text, nullable=True)
    failure_filters: Mapped[Optional[Any]]   = mapped_column(JSON, nullable=True)
    captured_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class IvSnapshot(Base):
    """Daily front-month ATM call IV snapshot per symbol.

    Drives the 8th momentum-exhaustion signal: short-term call IV at
    extreme percentile. Captured by the daily IV scheduler job for
    every theme-universe symbol. After 30+ days of capture the
    percentile becomes statistically meaningful.
    """

    __tablename__ = "iv_snapshots"
    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_iv_snapshots_symbol_date"),
        Index("ix_iv_snapshots_symbol_date", "symbol", "date"),
    )

    id:               Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol:           Mapped[str]    = mapped_column(String(10), nullable=False)
    date:             Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    atm_call_iv:      Mapped[float]  = mapped_column(Float, nullable=False)
    dte_used:         Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    strike_used:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spot_at_capture:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    captured_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Trade intents + audit
# ---------------------------------------------------------------------------


class TradeIntent(Base):
    """A trade the user has reviewed and approved (or is reviewing).

    Two structural shapes covered by the same row:

    * ``structure="stock"`` — the basic case: one symbol, one side, one
      limit/stop/target price. Used for direct equity entries.
    * ``structure="pmcc"`` (or ``"pmcc_sequenced"``) — multi-leg PMCC
      combo. The leap_* and short_call_* columns describe the two legs;
      ``net_debit_*`` capture the combo's pricing; ``walking_config``
      snapshots the execution policy at submission for replay/audit.

    State machine for PMCC:
      pending → leap_pending → leap_open_naked → short_pending → pmcc_full → closing → closed
                            ↳ abandoned   (executor walked to cap, no fill)
    Stock intents stay in pending → submitted → filled | cancelled.
    """

    __tablename__ = "trade_intents"
    __table_args__ = (
        Index("ix_trade_intents_user_id_status", "user_id", "status"),
        Index("ix_trade_intents_run_id", "run_id"),
        Index("ix_trade_intents_structure_state", "structure", "position_state"),
    )

    id:              Mapped[str]    = mapped_column(String(64), primary_key=True, default=_new_id)
    user_id:         Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    run_id:          Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    symbol:          Mapped[str]    = mapped_column(String(10), nullable=False)
    side:            Mapped[str]    = mapped_column(String(8), nullable=False)  # BUY | SELL
    qty:             Mapped[float]  = mapped_column(Float, nullable=False)
    order_type:      Mapped[str]    = mapped_column(String(8), default="LMT", nullable=False)
    limit_px:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_px:         Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_px:       Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status:          Mapped[str]    = mapped_column(String(16), default="pending", nullable=False)
    ibkr_order_id:   Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rationale:       Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    # PMCC extension (added 0005)
    structure:       Mapped[str]    = mapped_column(String(24), default="stock", nullable=False)
    position_state:  Mapped[str]    = mapped_column(String(24), default="pending", nullable=False)

    leap_expiry:        Mapped[Optional[str]]      = mapped_column(String(10), nullable=True)
    leap_strike:        Mapped[Optional[float]]    = mapped_column(Float, nullable=True)
    leap_delta_target:  Mapped[Optional[float]]    = mapped_column(Float, nullable=True)
    leap_delta_actual:  Mapped[Optional[float]]    = mapped_column(Float, nullable=True)
    leap_iv:            Mapped[Optional[float]]    = mapped_column(Float, nullable=True)
    leap_open_interest: Mapped[Optional[int]]      = mapped_column(Integer, nullable=True)
    leap_qty:           Mapped[Optional[int]]      = mapped_column(Integer, nullable=True)
    leap_filled_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    leap_fill_price:    Mapped[Optional[float]]    = mapped_column(Float, nullable=True)

    short_call_expiry:        Mapped[Optional[str]]      = mapped_column(String(10), nullable=True)
    short_call_strike:        Mapped[Optional[float]]    = mapped_column(Float, nullable=True)
    short_call_delta_target:  Mapped[Optional[float]]    = mapped_column(Float, nullable=True)
    short_call_delta_actual:  Mapped[Optional[float]]    = mapped_column(Float, nullable=True)
    short_call_iv:            Mapped[Optional[float]]    = mapped_column(Float, nullable=True)
    short_call_open_interest: Mapped[Optional[int]]      = mapped_column(Integer, nullable=True)
    short_call_qty:           Mapped[Optional[int]]      = mapped_column(Integer, nullable=True)
    short_call_filled_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    short_call_fill_price:    Mapped[Optional[float]]    = mapped_column(Float, nullable=True)

    net_debit_target:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_debit_cap:     Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_debit_filled:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_loss:          Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    walking_config:    Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    entry_strategy:     Mapped[Optional[str]]              = mapped_column(String(24), nullable=True)
    support_reference:  Mapped[Optional[float]]            = mapped_column(Float, nullable=True)
    trigger_conditions: Mapped[Optional[dict[str, Any]]]   = mapped_column(JSON, nullable=True)
    trigger_status:     Mapped[Optional[dict[str, Any]]]   = mapped_column(JSON, nullable=True)

    ibkr_combo_conid:   Mapped[Optional[str]]              = mapped_column(String(64), nullable=True)


class SystemState(Base):
    """Singleton row holding runtime kill switches.

    Always exactly one row with id=1. Toggling ``autotrade_enabled`` to
    False halts every automation loop without a deploy. The env-level
    ``AUTOTRADE_ENABLED`` is the orthogonal deploy-time gate; *both*
    must be true for the auto gate to pass.

    The scheduler_* columns control the daily theme-runner cron. Note
    these are deliberately separate from autotrade_enabled — you can run
    the analysis on auto-pilot without trading, which is the production
    pipeline (Phase A) before execution comes online (Phase C).
    """

    __tablename__ = "system_state"

    id:                Mapped[int]    = mapped_column(Integer, primary_key=True)
    autotrade_enabled: Mapped[bool]   = mapped_column(Boolean, default=False, nullable=False)
    last_kill_at:      Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    kill_reason:       Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Entry circuit breaker — halts NEW entries (never closes positions) on a
    # severe account-level breach. Latches until manually re-armed. The daily
    # NAV reference (captured at the first RTH tick) drives the intraday-drop
    # check. Deliberately distinct from autotrade_enabled: the breaker pauses
    # only new entries; the maintenance/exit loop keeps managing open
    # positions on their own signals.
    entry_breaker_tripped:    Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    entry_breaker_reason:     Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    entry_breaker_tripped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    breaker_nav_ref:          Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    breaker_nav_ref_date:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    scheduler_enabled:      Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scheduler_cron:         Mapped[str]  = mapped_column(String(64), default="0 9 * * 1-5", nullable=False)
    scheduler_next_run_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduler_last_run_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduler_last_status:  Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    updated_at:        Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    updated_by:        Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class AutoAction(Base):
    """Audit row for every automation decision, executed or rejected.

    Distinct from ``TradeAuditLog``: auto_actions records the *decision*
    layer (gate result, why an action was taken or skipped) while the
    audit log records broker-call attempt + outcome. Together they answer
    the two questions ops needs after an incident: what did the system
    decide, and what did the broker confirm.
    """

    __tablename__ = "auto_actions"
    __table_args__ = (
        Index("ix_auto_actions_timestamp", "timestamp"),
        Index("ix_auto_actions_loop_status", "loop", "gate_status"),
        Index("ix_auto_actions_symbol", "symbol"),
    )

    id:             Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp:      Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    loop:           Mapped[str]    = mapped_column(String(32), nullable=False)
    action_type:    Mapped[str]    = mapped_column(String(48), nullable=False)
    symbol:         Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    intent_id:      Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("trade_intents.id", ondelete="SET NULL"), nullable=True)
    gate_status:    Mapped[str]    = mapped_column(String(16), nullable=False)  # passed | rejected | error
    gate_failures:  Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    payload:        Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    outcome:        Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    ibkr_order_id:  Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error:          Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class TradeAuditLog(Base):
    """Append-only log of every IBKR action.

    Every order placement writes one row before the IBKR call (intent)
    and one row after (outcome). Never amend rows in place. This is the
    legal-grade record of why and when a trade was sent.
    """

    __tablename__ = "trade_audit_log"
    __table_args__ = (
        Index("ix_trade_audit_log_user_id_ts", "user_id", "timestamp"),
    )

    id:              Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:         Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    intent_id:       Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("trade_intents.id", ondelete="SET NULL"), nullable=True)
    action:          Mapped[str]    = mapped_column(String(32), nullable=False)
    outcome:         Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ibkr_account:    Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payload:         Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    timestamp:       Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Hedge Fund Signal Tracker — named-manager EDGAR positioning
# ---------------------------------------------------------------------------
#
# Decision-support layer that tracks *named* legendary investors (Gerstner,
# Coatue, Aschenbrenner, Baker, ...) via their SEC EDGAR filings, distinct
# from the anonymous aggregate-13F signal the agents already consume. A
# manager is a logical entity that may file under several CIKs (Situational
# Awareness has two; Coatue files across many sub-advisers) — hence the
# split between HedgeFundManager and ManagerCik.


class HedgeFundManager(Base):
    """A tracked investor. Seeded from ``managers.toml`` (config is the
    source of truth) and upserted on startup, so adding a manager is a
    config edit, not a migration."""

    __tablename__ = "hedge_fund_managers"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_hf_manager_slug"),
        Index("ix_hf_manager_active", "active"),
    )

    id:                Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug:              Mapped[str]    = mapped_column(String(64), nullable=False)
    name:              Mapped[str]    = mapped_column(String(200), nullable=False)
    # macro_only managers (e.g. D.E. Shaw) get tracked for context but never
    # produce a single-name conviction signal — their alpha decays too fast.
    macro_only:        Mapped[bool]   = mapped_column(Boolean, nullable=False, default=False)
    active:            Mapped[bool]   = mapped_column(Boolean, nullable=False, default=True)
    primary_themes:    Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)   # list[str] of theme ids
    weighting_profile: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)   # dict[str,float]
    last_filing_at:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:        Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at:        Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    ciks: Mapped[list["ManagerCik"]] = relationship(
        back_populates="manager", cascade="all, delete-orphan", lazy="selectin",
    )


class ManagerCik(Base):
    """A SEC CIK that maps to a manager. Many-to-one so the two-CIK and
    sub-adviser cases collapse to one logical manager."""

    __tablename__ = "manager_ciks"
    __table_args__ = (
        UniqueConstraint("cik", name="uq_manager_cik"),
        Index("ix_manager_cik_manager", "manager_id"),
    )

    id:          Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    manager_id:  Mapped[int]    = mapped_column(Integer, ForeignKey("hedge_fund_managers.id", ondelete="CASCADE"), nullable=False)
    cik:         Mapped[str]    = mapped_column(String(10), nullable=False)   # 10-digit zero-padded
    entity_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    manager: Mapped[HedgeFundManager] = relationship(back_populates="ciks")


class Filing(Base):
    """One EDGAR filing we've ingested. ``accession_no`` is the idempotency
    key — the poller skips anything already stored."""

    __tablename__ = "filings"
    __table_args__ = (
        UniqueConstraint("accession_no", name="uq_filing_accession"),
        Index("ix_filing_manager_form", "manager_id", "form_type"),
        Index("ix_filing_filed_at", "filed_at"),
    )

    id:           Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    manager_id:   Mapped[int]    = mapped_column(Integer, ForeignKey("hedge_fund_managers.id", ondelete="CASCADE"), nullable=False)
    cik:          Mapped[str]    = mapped_column(String(10), nullable=False)
    form_type:    Mapped[str]    = mapped_column(String(16), nullable=False)   # 13F-HR, SC 13D, 4, ...
    accession_no: Mapped[str]    = mapped_column(String(32), nullable=False)
    filed_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_url:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parsed_json:  Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)   # form-specific summary
    ingested_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class FundHolding(Base):
    """One position from a 13F filing, deduped to the parent-manager level
    and value-normalised to whole USD on ingest. CUSIP + issuer name are the
    canonical identity; ``ticker`` is a best-effort enrichment (13F never
    reports a ticker)."""

    __tablename__ = "fund_holdings"
    __table_args__ = (
        UniqueConstraint("filing_id", "cusip", "put_call_flag", name="uq_holding_filing_cusip_pc"),
        Index("ix_holding_manager_period", "manager_id", "period_end"),
        Index("ix_holding_ticker", "ticker"),
        Index("ix_holding_cusip", "cusip"),
    )

    id:               Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    manager_id:       Mapped[int]    = mapped_column(Integer, ForeignKey("hedge_fund_managers.id", ondelete="CASCADE"), nullable=False)
    filing_id:        Mapped[int]    = mapped_column(Integer, ForeignKey("filings.id", ondelete="CASCADE"), nullable=False)
    cusip:            Mapped[str]    = mapped_column(String(9), nullable=False)
    issuer_name:      Mapped[str]    = mapped_column(String(200), nullable=False)
    ticker:           Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    value_usd:        Mapped[float]  = mapped_column(Float, nullable=False)
    shares:           Mapped[float]  = mapped_column(Float, nullable=False)
    # '' for long underlying, 'C'/'P' for option rows. Part of the unique key
    # so a put and the underlying don't collide.
    put_call_flag:    Mapped[str]    = mapped_column(String(1), nullable=False, default="")
    pct_of_portfolio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    period_end:       Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class PositionChange(Base):
    """Quarter-over-quarter delta for a (manager, security). Recomputed from
    the latest two 13F periods each sweep; ``change_type`` is the headline."""

    __tablename__ = "position_changes"
    __table_args__ = (
        UniqueConstraint("manager_id", "cusip", "current_period", name="uq_poschg_mgr_cusip_period"),
        Index("ix_poschg_manager", "manager_id"),
        Index("ix_poschg_ticker", "ticker"),
    )

    id:             Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    manager_id:     Mapped[int]    = mapped_column(Integer, ForeignKey("hedge_fund_managers.id", ondelete="CASCADE"), nullable=False)
    cusip:          Mapped[str]    = mapped_column(String(9), nullable=False)
    ticker:         Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    issuer_name:    Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    prior_shares:   Mapped[float]  = mapped_column(Float, nullable=False, default=0.0)
    current_shares: Mapped[float]  = mapped_column(Float, nullable=False, default=0.0)
    change_pct:     Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    change_type:    Mapped[str]    = mapped_column(String(8), nullable=False)   # new|add|trim|exit|hold
    prior_period:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class CusipTickerMap(Base):
    """CUSIP → ticker resolution cache. 13F reports CUSIP + issuer name only;
    this is the best-effort enrichment layer, populated from Form 4 issuer
    symbols and provider lookups."""

    __tablename__ = "cusip_ticker_map"

    cusip:        Mapped[str]    = mapped_column(String(9), primary_key=True)
    ticker:       Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    issuer_name:  Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    resolved_via: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)   # form4|fmp|manual
    updated_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
