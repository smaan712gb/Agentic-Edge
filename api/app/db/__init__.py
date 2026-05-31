"""Database package for Agentic Edge.

Exposes the engine, sessionmaker, and ORM models. Repositories live in
``api/app/repos``; the ``main`` module wires them up via FastAPI deps.
"""

from .base import Base, db_engine, db_session, get_session, init_db
from .models import (
    AutoAction,
    ClosingAccumulationSignal,
    CusipTickerMap,
    EquitySnapshot,
    Filing,
    FundHolding,
    HedgeFundManager,
    IvSnapshot,
    ManagerCik,
    NewsMention,
    Position,
    PositionChange,
    Run,
    RunEvent,
    SystemState,
    Theme,
    ThemeReport,
    ThemeSymbol,
    TickerScore,
    TradeAuditLog,
    TradeIntent,
)

__all__ = [
    "Base",
    "db_engine",
    "db_session",
    "get_session",
    "init_db",
    "AutoAction",
    "ClosingAccumulationSignal",
    "CusipTickerMap",
    "EquitySnapshot",
    "Filing",
    "FundHolding",
    "HedgeFundManager",
    "IvSnapshot",
    "ManagerCik",
    "NewsMention",
    "Position",
    "PositionChange",
    "Run",
    "RunEvent",
    "SystemState",
    "Theme",
    "ThemeReport",
    "ThemeSymbol",
    "TickerScore",
    "TradeAuditLog",
    "TradeIntent",
]
