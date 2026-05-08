"""Async SQLAlchemy engine + session factory.

Postgres in production (with pgvector via ``vector`` column type from
``pgvector.sqlalchemy``). SQLite in dev/tests for zero-setup. The ORM
models avoid Postgres-specific column types in their default columns;
pgvector embedding columns are added via Alembic only when running
against Postgres.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from api.app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Project-wide declarative base."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def db_engine() -> AsyncEngine:
    """Lazily create the global async engine."""
    global _engine, _sessionmaker
    if _engine is None:
        url = get_settings().DATABASE_URL
        # SQLAlchemy treats "sqlite+aiosqlite:///./" specially — use it as-is.
        _engine = create_async_engine(
            url,
            future=True,
            echo=False,
            pool_pre_ping=not url.startswith("sqlite"),
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
        logger.info("db: engine initialized for %s", _scrub_url(url))
    return _engine


def db_session() -> async_sessionmaker[AsyncSession]:
    """Return the configured sessionmaker. Engine is initialised lazily."""
    db_engine()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a transactional async session that commits on success."""
    sm = db_session()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create tables for the dev SQLite path; Alembic owns Postgres.

    Postgres deployments must run ``alembic upgrade head`` on container
    start. SQLite dev paths get auto-create so a fresh checkout boots
    without an Alembic invocation.
    """
    settings = get_settings()
    if not settings.DATABASE_URL.startswith("sqlite"):
        return  # Postgres: Alembic owns schema
    engine = db_engine()
    # Import models so the metadata sees them.
    from . import models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("db: SQLite tables created (dev path)")


def _scrub_url(url: str) -> str:
    """Redact credentials before logging a DATABASE_URL."""
    if "@" not in url:
        return url
    head, tail = url.split("@", 1)
    if "://" in head:
        scheme, _, _ = head.partition("://")
        return f"{scheme}://***@{tail}"
    return f"***@{tail}"
