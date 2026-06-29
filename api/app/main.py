"""Agentic Edge API.

Postgres / SQLite-backed surface that serves the frontend. The frozen
contract — exact JSON shapes — lives in ``web/lib/api.ts``; if you
change a shape here, change it there too.

Modes:
  USE_MOCK_RUN=1   the run executor is deterministic (no LLM cost) — see _simulate_run
  MOCK_DATA=1      positions / equity curve return seeded data instead of real IBKR
  (neither)        production: real LLM + real providers + paper brokerage

Run:
    alembic -c api/alembic.ini upgrade head
    uvicorn api.app.main:app --reload --port 8000
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Windows asyncio policy — MUST run before any other module imports asyncio
# state. ib_insync binds its Futures (_OverlappedFuture under Proactor) to the
# loop that created them; uvicorn defaults to ProactorEventLoop on Windows,
# and the heartbeat / entry / maint background tasks then trip "Future
# attached to a different loop" the moment they touch a reconnected provider.
# Selector loop avoids the IOCP-future binding entirely.
# ---------------------------------------------------------------------------
import sys as _sys
if _sys.platform == "win32":
    import asyncio as _asyncio_bootstrap
    _asyncio_bootstrap.set_event_loop_policy(
        _asyncio_bootstrap.WindowsSelectorEventLoopPolicy()
    )

# Trust the OS certificate store for ALL outbound TLS — LLM API (DeepSeek via
# the OpenAI SDK), SEC EDGAR, and every data vendor. Behind a corporate
# TLS-intercepting proxy the default certifi bundle rejects the proxy's CA, so
# the OpenAI/httpx clients fail with "Connection error" and every agent call
# dies silently (runs finish in seconds with zero scores). truststore makes
# the stdlib ssl use the OS store (which trusts the proxy CA), fixing all
# clients at once. Must run before any SSL context is created.
try:
    import truststore as _truststore
    _truststore.inject_into_ssl()
except Exception as _e:  # pragma: no cover — falls back to certifi
    import logging as _logging
    _logging.getLogger("agentic_edge").warning("truststore inject failed: %s", _e)

# Note: we deliberately do NOT call nest_asyncio.apply() here. ib_insync.IB
# captures the running loop at construction (__init__), and nest_asyncio's
# loop patching can hand back a different loop than the running one,
# manifesting as "Future attached to a different loop" once background
# tasks (heartbeat / entry / maint) touch the provider. The fix that works
# on Windows is: keep the selector policy above, and pre-create the IBKR
# provider in lifespan startup so the socket binds to the FastAPI main
# loop before any background task can race it. See `lifespan()` below.

import asyncio
import json
import logging
import random
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import init_db, get_session as db_session
from .repos import EventRepo, RunRepo, ThemeRepo

logger = logging.getLogger(__name__)


def _setup_file_logging() -> None:
    """Persist logs to a rotating file alongside the console.

    Backend logs otherwise live only in the uvicorn console window and are lost
    on restart, which makes auditing what the autonomous loops did impossible
    (the gap surfaced in the 2026-06-29 system audit). Attaches a
    RotatingFileHandler to the root logger so every ``agentic_edge.*`` line is
    durably captured. Idempotent + best-effort: a filesystem failure must never
    block API startup.
    """
    try:
        s = get_settings()
        if not s.LOG_DIR:
            return
        import os
        from logging.handlers import RotatingFileHandler
        root = logging.getLogger()
        # Don't double-attach across reloads.
        if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
            return
        os.makedirs(s.LOG_DIR, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(s.LOG_DIR, "agentic_edge.log"),
            maxBytes=s.LOG_MAX_BYTES, backupCount=s.LOG_BACKUP_COUNT, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        level = getattr(logging, str(s.LOG_LEVEL).upper(), logging.INFO)
        handler.setLevel(level)
        root.addHandler(handler)
        if root.level > level or root.level == logging.NOTSET:
            root.setLevel(level)
        logger.info("file logging enabled -> %s/agentic_edge.log (level=%s)", s.LOG_DIR, s.LOG_LEVEL)
    except Exception as e:  # pragma: no cover - never block startup on logging
        logger.warning("file logging setup failed: %s", e)


_setup_file_logging()


# ---------------------------------------------------------------------------
# Friendly agent catalog — names + plain-English descriptions surfaced in
# the UI. Vendor names (Polygon, FMP, Unusual Whales) intentionally never
# appear here.
# ---------------------------------------------------------------------------

AGENTS: list[dict[str, Any]] = [
    {"id": "market", "name": "Market Analyst", "role": "analyst", "lane": "analysts",
     "summary": "Reads price action, trend, momentum, and volatility across multiple timeframes.",
     "explanation": "Pulls daily and intraday price history, computes moving averages, RSI, MACD, and volatility bands, and writes a multi-timeframe setup grade."},
    {"id": "fundamentals", "name": "Fundamentals Analyst", "role": "analyst", "lane": "analysts",
     "summary": "Evaluates revenue, margins, cash flow, and balance-sheet strength.",
     "explanation": "Reviews the latest income statement, balance sheet, and cash-flow statement, looking for owner-earnings quality and trend changes vs. prior periods."},
    {"id": "news", "name": "News Analyst", "role": "analyst", "lane": "analysts",
     "summary": "Surfaces catalysts: earnings, guidance, product launches, regulatory events.",
     "explanation": "Scans recent company news plus macro context (rates, inflation, jobs) and flags items that could move the stock in the next 1–10 sessions."},
    {"id": "options", "name": "Options Flow Analyst", "role": "analyst", "lane": "analysts",
     "summary": "Reads what large-premium options buyers are doing right now.",
     "explanation": "Aggregates today's notable options activity, computes bullish vs. bearish premium, and locates the gamma walls and max-pain price most likely to act as magnets."},
    {"id": "social", "name": "Social Sentiment Analyst", "role": "analyst", "lane": "analysts",
     "summary": "Reads retail and institutional sentiment shifts.",
     "explanation": "Tracks social-channel mention velocity and tone. Distinguishes hype spikes from sustained sentiment changes that lead price."},
    {"id": "bull", "name": "Bull Researcher", "role": "researcher", "lane": "debate",
     "summary": "Argues the case to buy, citing the analyst reports.",
     "explanation": "Builds the strongest case for upside, grounding every claim in the analyst evidence. Will be challenged by the Bear."},
    {"id": "bear", "name": "Bear Researcher", "role": "researcher", "lane": "debate",
     "summary": "Argues the case to avoid or short, citing the analyst reports.",
     "explanation": "Builds the strongest case against, focusing on options-flow traps, fundamentals deterioration, and macro risks."},
    {"id": "research_manager", "name": "Research Manager", "role": "synthesizer", "lane": "synthesis",
     "summary": "Judges the bull/bear debate and scores conviction.",
     "explanation": "Decides which side won and rates conviction 1–5. Below the configured floor, the symbol is auto-held — keeping us out of low-conviction trades."},
    {"id": "trader", "name": "Trader", "role": "executor", "lane": "synthesis",
     "summary": "Proposes entry, size, and risk levels.",
     "explanation": "Translates the research call into a concrete trade plan: side, target size, entry zone, stop, and target."},
    {"id": "risk_aggressive", "name": "Aggressive Risk Voice", "role": "risk", "lane": "risk",
     "summary": "Pushes for more size when the setup is strong.",
     "explanation": "Argues for upsizing when conviction and setup quality align — keeps the system from leaving good trades too small."},
    {"id": "risk_conservative", "name": "Conservative Risk Voice", "role": "risk", "lane": "risk",
     "summary": "Pushes back on size when risk is unclear.",
     "explanation": "Argues for trimming size or wider stops when correlations, vol, or news risk are elevated."},
    {"id": "risk_neutral", "name": "Neutral Risk Voice", "role": "risk", "lane": "risk",
     "summary": "Mediates between aggressive and conservative voices.",
     "explanation": "Reconciles the two extremes into a balanced position-sizing recommendation."},
    {"id": "portfolio_manager", "name": "Portfolio Manager", "role": "decider", "lane": "synthesis",
     "summary": "Final go / no-go on the trade.",
     "explanation": "Looks at the trade plan plus the risk debate and approves, rejects, or sends back for revision. Owns the final decision."},
    {"id": "scorecard", "name": "Scorecard Scorer", "role": "scorer", "lane": "scoring",
     "summary": "Produces the symbol's score and key drivers.",
     "explanation": "Reads every report from this symbol's run and emits a structured score: setup quality, options sentiment, and how well the symbol fits the theme thesis."},
    {"id": "ranker", "name": "Theme Ranker", "role": "ranker", "lane": "scoring",
     "summary": "Ranks every symbol in the theme.",
     "explanation": "Once every symbol is scored, ranks them and writes the 'Best Positioned' list with rationale grounded in each symbol's reports."},
]

AGENT_EDGES: list[dict[str, str]] = [
    {"from": "start", "to": "market"}, {"from": "start", "to": "fundamentals"},
    {"from": "start", "to": "news"}, {"from": "start", "to": "options"},
    {"from": "start", "to": "social"},
    {"from": "market", "to": "bull"}, {"from": "fundamentals", "to": "bull"},
    {"from": "news", "to": "bull"}, {"from": "options", "to": "bull"},
    {"from": "social", "to": "bull"},
    {"from": "market", "to": "bear"}, {"from": "fundamentals", "to": "bear"},
    {"from": "news", "to": "bear"}, {"from": "options", "to": "bear"},
    {"from": "social", "to": "bear"},
    {"from": "bull", "to": "research_manager"}, {"from": "bear", "to": "research_manager"},
    {"from": "research_manager", "to": "trader"},
    {"from": "trader", "to": "risk_aggressive"}, {"from": "trader", "to": "risk_conservative"},
    {"from": "trader", "to": "risk_neutral"},
    {"from": "risk_aggressive", "to": "portfolio_manager"},
    {"from": "risk_conservative", "to": "portfolio_manager"},
    {"from": "risk_neutral", "to": "portfolio_manager"},
    {"from": "portfolio_manager", "to": "scorecard"},
    {"from": "scorecard", "to": "ranker"},
]


# ---------------------------------------------------------------------------
# In-memory live state (per-run SSE queue) — ephemeral, recreated per run.
# Durable persistence happens via the DB; this is just the streaming bus.
# ---------------------------------------------------------------------------


RUN_QUEUES: dict[str, asyncio.Queue[dict[str, Any]]] = defaultdict(asyncio.Queue)


# ---------------------------------------------------------------------------
# DTOs the runner emits onto the queue. These match what web/lib/api.ts
# expects, so route handlers and the runner share one shape.
# ---------------------------------------------------------------------------


@dataclass
class AgentEvent:
    agent_id: str
    symbol: Optional[str]
    status: str  # "started" | "finished"
    summary: Optional[str]
    timestamp: str


@dataclass
class SymbolScore:
    symbol: str
    setup: float
    options: float
    thesis_fit: float
    composite: float
    decision: str
    drivers: list[str]
    risks: list[str]


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------


class ThemeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    thesis: str = Field(..., min_length=1, max_length=500)
    chokepoint: str = Field("", max_length=900)


class SymbolIn(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10)


class RunStartIn(BaseModel):
    theme_id: str


# ---------------------------------------------------------------------------
# Lifespan: bootstrap DB on startup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "Agentic Edge API starting (mock_run=%s mock_data=%s db=%s)",
        settings.USE_MOCK_RUN, settings.MOCK_DATA, settings.DATABASE_URL.split('@')[-1],
    )
    await init_db()

    # Hedge Fund Signal Tracker — upsert tracked managers from managers.toml
    # (config is source of truth). Best-effort: a config/parse error must not
    # block API startup.
    try:
        from .hedge_funds.config_loader import load_managers_from_config
        await load_managers_from_config()
    except Exception as e:
        logger.warning("hedge-fund manager config load failed: %s", e)

    # Pre-bind the IBKR provider to THIS loop. ib_insync.IB captures the
    # running loop at construction; if we let the heartbeat task be the
    # first to call _ibkr(), the IB socket binds to a different loop
    # reference than the one running the entry/maint/scheduler tasks,
    # producing "Future attached to a different loop" errors. Pre-creating
    # it here (eagerly, swallowing connection errors) guarantees the
    # socket's loop is the main FastAPI loop. If the broker isn't running,
    # the heartbeat will retry on its own cadence — the failure here is
    # not fatal to startup.
    if not settings.MOCK_DATA:
        try:
            from .positions import _ibkr
            await _ibkr()
            logger.info("IBKR provider bound to main loop")
        except Exception as e:
            logger.warning(
                "IBKR pre-bind failed (%s) — heartbeat will retry; "
                "autotrade entries will defer until broker is up", e,
            )

    from .scheduler import start_scheduler, stop_scheduler
    from .autotrade.entry_loop import start_entry_loop, stop_entry_loop
    from .autotrade.maint_loop import start_maintenance_loop, stop_maintenance_loop
    from .autotrade.heartbeat import start_heartbeat, stop_heartbeat
    await start_scheduler()
    await start_heartbeat()
    await start_entry_loop()
    await start_maintenance_loop()
    try:
        yield
    finally:
        await stop_maintenance_loop()
        await stop_entry_loop()
        await stop_heartbeat()
        await stop_scheduler()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


app = FastAPI(title="Agentic Edge API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Admin (kill switch + reconcile triggers) — guarded by X-Admin-Token.
from .admin import router as _admin_router  # noqa: E402
app.include_router(_admin_router)

# Trade-intent endpoints (build PMCC, submit, cancel).
from .trade_intents import router as _trade_intents_router  # noqa: E402
app.include_router(_trade_intents_router)

# Hedge Fund Signal Tracker (managers, holdings, overlap, smart-money read).
from .hedge_funds.routes import router as _hedge_funds_router  # noqa: E402
app.include_router(_hedge_funds_router)

# Quant Research Factory — universe-graph + point-in-time feature reads.
from .research.routes import router as _research_router  # noqa: E402
app.include_router(_research_router)


# ---------------------------------------------------------------------------
# Health + agents
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    from .autotrade.heartbeat import is_connected_snapshot
    return {
        "status": "ok",
        "mode": {
            "use_mock_run": settings.USE_MOCK_RUN,
            "mock_data": settings.MOCK_DATA,
            "ibkr": settings.IBKR_MODE,
        },
        "ibkr_connected": is_connected_snapshot(),
    }


@app.get("/api/agents")
async def list_agents() -> dict[str, Any]:
    return {"agents": AGENTS, "edges": AGENT_EDGES}


@app.get("/api/scheduler/status")
async def public_scheduler_status() -> dict[str, Any]:
    """Public, read-only scheduler status — used by the Themes page badge.
    Mutating endpoints (enable/disable/cron) live behind /api/admin and
    require the admin token."""
    from .scheduler import scheduler_status
    return await scheduler_status()


@app.get("/api/themes/{theme_id}/regime")
async def theme_regime(theme_id: str) -> dict[str, Any]:
    """Sector-ETF regime for the theme. Powers the regime chip on the UI
    and is read by the auto-gate before approving new long entries."""
    async with db_session() as s:
        if (await ThemeRepo(s).get(theme_id)) is None:
            raise HTTPException(404, "theme not found")
    from tradingagents.signals.sector_regime import get_theme_regime
    ctx = await get_theme_regime(theme_id)
    return ctx.to_dict()


# ---------------------------------------------------------------------------
# Themes CRUD — DB-backed
# ---------------------------------------------------------------------------


@app.get("/api/themes")
async def list_themes() -> list[dict[str, Any]]:
    async with db_session() as s:
        themes = await ThemeRepo(s).list()
        return [ThemeRepo.to_dto(t) for t in themes]


@app.post("/api/themes", status_code=201)
async def create_theme(body: ThemeIn) -> dict[str, Any]:
    settings = get_settings()
    async with db_session() as s:
        repo = ThemeRepo(s)
        # Per-user cap from Settings — single-user mode hits it once a user
        # creates 20+ themes; stays in place for when auth lands.
        existing = await repo.count_for_user(user_id=None)
        if existing >= settings.MAX_THEMES_PER_USER:
            raise HTTPException(429, f"max {settings.MAX_THEMES_PER_USER} themes")
        t = await repo.create(name=body.name, thesis=body.thesis, chokepoint=body.chokepoint)
        return ThemeRepo.to_dto(t)


@app.delete("/api/themes/{theme_id}")
async def delete_theme(theme_id: str) -> Response:
    async with db_session() as s:
        ok = await ThemeRepo(s).delete(theme_id)
    if not ok:
        raise HTTPException(404, "theme not found")
    return Response(status_code=204)


@app.post("/api/themes/{theme_id}/symbols", status_code=201)
async def add_symbol(theme_id: str, body: SymbolIn) -> dict[str, Any]:
    settings = get_settings()
    async with db_session() as s:
        repo = ThemeRepo(s)
        t = await repo.get(theme_id)
        if t is None:
            raise HTTPException(404, "theme not found")
        if len(t.symbols) >= settings.MAX_TICKERS_PER_THEME:
            raise HTTPException(429, f"max {settings.MAX_TICKERS_PER_THEME} tickers per theme")
        t = await repo.add_symbol(theme_id, body.symbol)
        assert t is not None
        return ThemeRepo.to_dto(t)


@app.delete("/api/themes/{theme_id}/symbols/{symbol}")
async def remove_symbol(theme_id: str, symbol: str) -> Response:
    async with db_session() as s:
        ok = await ThemeRepo(s).remove_symbol(theme_id, symbol)
    if not ok:
        raise HTTPException(404, "theme/symbol not found")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Performance — under MOCK_DATA=1 returns seeded curve; otherwise reads
# from equity_snapshots / IBKR. Real wiring lives in api/app/positions.py.
# ---------------------------------------------------------------------------


def _seeded_equity_curve() -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    rng = random.Random(42)
    equity = 100_000.0
    rows: list[dict[str, Any]] = []
    for i in range(89, -1, -1):
        day = today - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        equity *= 1 + 0.0008 + rng.gauss(0, 0.011)
        rows.append({"date": day.isoformat(), "equity": round(equity, 2)})
    return rows


@app.get("/api/performance/curve")
async def performance_curve() -> dict[str, Any]:
    if get_settings().MOCK_DATA:
        return {"points": _seeded_equity_curve(), "starting_equity": 100_000.0}
    from .positions import equity_curve_real
    return await equity_curve_real()


@app.get("/api/performance/today")
async def performance_today() -> dict[str, Any]:
    if get_settings().MOCK_DATA:
        rows = _seeded_equity_curve()
        if len(rows) < 2:
            return {"gain": 0.0, "gain_pct": 0.0, "equity": rows[-1]["equity"] if rows else 0.0,
                    "as_of": rows[-1]["date"] if rows else ""}
        last, prev = rows[-1]["equity"], rows[-2]["equity"]
        gain = round(last - prev, 2)
        gain_pct = round((last / prev - 1) * 100, 2) if prev else 0.0
        return {"equity": last, "gain": gain, "gain_pct": gain_pct, "as_of": rows[-1]["date"]}
    from .positions import equity_today_real
    return await equity_today_real()


@app.get("/api/positions")
async def positions() -> list[dict[str, Any]]:
    if get_settings().MOCK_DATA:
        return [
            {"symbol": "NVDA", "qty": 40, "avg_price": 118.40, "last_price": 124.10, "pnl": 228.0},
            {"symbol": "AVGO", "qty": 25, "avg_price": 178.55, "last_price": 181.20, "pnl": 66.25},
            {"symbol": "VRT",  "qty": 80, "avg_price":  91.30, "last_price":  95.05, "pnl": 300.0},
        ]
    from .positions import positions_real
    return await positions_real()


# ---------------------------------------------------------------------------
# Runs — DB-backed; events double-write to DB while streaming live via queue
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.post("/api/runs", status_code=201)
async def start_run(
    body: RunStartIn,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    settings = get_settings()
    async with db_session() as s:
        repo = RunRepo(s)
        theme_repo = ThemeRepo(s)

        if idempotency_key:
            prior = await repo.find_by_idempotency_key(user_id=None, key=idempotency_key)
            if prior is not None:
                # Same key + same theme → return the prior run (per RFC-style idempotency).
                full = await repo.get(prior.id)
                return RunRepo.to_dto(full) if full else RunRepo.to_dto(prior)

        # Rate limit: per the Settings cap.
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        recent = await repo.count_recent_for_user(user_id=None, since=since)
        if recent >= settings.MAX_RUNS_PER_USER_PER_HOUR:
            raise HTTPException(
                429,
                f"rate limit: max {settings.MAX_RUNS_PER_USER_PER_HOUR} runs per hour",
            )

        theme = await theme_repo.get(body.theme_id)
        if theme is None:
            raise HTTPException(404, "theme not found")
        if not theme.symbols:
            raise HTTPException(400, "theme has no symbols — add at least one before running")

        run = await repo.create(theme_id=body.theme_id, idempotency_key=idempotency_key)

        # Snapshot the data needed by the runner before the session closes.
        run_id = run.id
        theme_dto = {
            "id": theme.id, "name": theme.name, "thesis": theme.thesis,
            "chokepoint": theme.chokepoint,
            "symbols": [sy.symbol for sy in sorted(theme.symbols, key=lambda x: x.position)],
        }

    # Kick off the background task. Mock vs real toggled by Settings.
    if settings.USE_MOCK_RUN:
        asyncio.create_task(_simulate_run(run_id, theme_dto))
    else:
        from .real_run import real_run
        asyncio.create_task(real_run(run_id, theme_dto))

    # Re-fetch with relationships loaded so the response matches GET /api/runs/{id}.
    async with db_session() as s2:
        full = await RunRepo(s2).get(run_id)
        return RunRepo.to_dto(full)  # type: ignore[arg-type]


@app.get("/api/runs")
async def list_runs() -> list[dict[str, Any]]:
    async with db_session() as s:
        runs = await RunRepo(s).list()
        # The list view doesn't need full event log; trim DTOs.
        out = []
        for r in runs:
            out.append({
                "id": r.id,
                "theme_id": r.theme_id,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "status": r.status,
                "progress": r.progress,
                "summary": r.summary,
                "best_positioned": r.best_positioned or [],
                "events": [],
                "scores": [
                    {
                        "symbol": sc.symbol,
                        "setup": sc.setup, "options": sc.options,
                        "thesis_fit": sc.thesis_fit, "composite": sc.composite,
                        "decision": sc.decision,
                        "drivers": sc.drivers or [], "risks": sc.risks or [],
                    } for sc in r.scores
                ],
            })
        return out


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    async with db_session() as s:
        r = await RunRepo(s).get(run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        return RunRepo.to_dto(r)


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    async with db_session() as s:
        r = await RunRepo(s).get(run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        snapshot_payload = {"type": "snapshot", "run": RunRepo.to_dto(r)}
        initial_terminal = r.status in ("done", "error")

    async def gen():
        yield f"data: {json.dumps(snapshot_payload)}\n\n"

        # If the run was already terminal at snapshot time, drain any
        # unconsumed messages still on the queue (e.g., a `done` that
        # landed between snapshot read and now) without blocking, then
        # emit a synthetic done so the client transitions cleanly.
        if initial_terminal:
            q = RUN_QUEUES.get(run_id)
            if q is not None:
                while True:
                    try:
                        msg = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    yield f"data: {json.dumps(msg)}\n\n"
                    if msg.get("type") == "done":
                        return
            yield f"data: {json.dumps({'type': 'done', 'run': snapshot_payload['run']})}\n\n"
            return

        # Live stream — drain the queue and re-check DB on keepalive so a
        # run that finished between snapshot read and queue subscribe still
        # terminates cleanly (the runner's `done` message may have been
        # consumed by an earlier subscriber or never queued at all).
        q = RUN_QUEUES[run_id]
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15.0)
            except asyncio.TimeoutError:
                async with db_session() as s2:
                    r2 = await RunRepo(s2).get(run_id)
                if r2 is not None and r2.status in ("done", "error"):
                    yield f"data: {json.dumps({'type': 'done', 'run': RunRepo.to_dto(r2)})}\n\n"
                    return
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(msg)}\n\n"
            if msg.get("type") == "done":
                return

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Mock simulator — used when USE_MOCK_RUN=1.
# Persists everything to the DB so the run is indistinguishable from a real
# one when read back later.
# ---------------------------------------------------------------------------


async def _simulate_run(run_id: str, theme: dict[str, Any]) -> None:
    q = RUN_QUEUES[run_id]
    rng = random.Random(hash(run_id) & 0xFFFFFFFF)

    if not theme["symbols"]:
        async with db_session() as s:
            await RunRepo(s).mark_error(run_id, "Theme has no symbols.")
        await q.put({"type": "done", "run": {"id": run_id, "status": "error",
                                             "summary": "Theme has no symbols."}})
        return

    phases = [
        ["market", "fundamentals", "news", "options", "social"],
        ["bull", "bear"], ["research_manager"], ["trader"],
        ["risk_aggressive", "risk_conservative", "risk_neutral"],
        ["portfolio_manager"], ["scorecard"],
    ]
    total_steps = sum(len(p) for p in phases) * len(theme["symbols"]) + 1
    done_steps = 0

    for symbol in theme["symbols"]:
        for phase in phases:
            for agent_id in phase:
                ev = AgentEvent(agent_id, symbol, "started", None, _now_iso())
                async with db_session() as s:
                    await EventRepo(s).append(
                        run_id=run_id, agent_id=ev.agent_id, symbol=ev.symbol,
                        status=ev.status, summary=ev.summary,
                    )
                await q.put({"type": "event", "event": asdict(ev)})
            await asyncio.sleep(0.4)
            for agent_id in phase:
                summary = _synth_summary(agent_id, symbol, rng)
                ev = AgentEvent(agent_id, symbol, "finished", summary, _now_iso())
                async with db_session() as s:
                    await EventRepo(s).append(
                        run_id=run_id, agent_id=ev.agent_id, symbol=ev.symbol,
                        status=ev.status, summary=ev.summary,
                    )
                done_steps += 1
                progress = round(done_steps / total_steps, 3)
                async with db_session() as s:
                    await RunRepo(s).update_progress(run_id, progress)
                await q.put({"type": "event", "event": asdict(ev), "progress": progress})

        score = _synth_score(symbol, rng)
        async with db_session() as s:
            await RunRepo(s).add_score(
                run_id=run_id, symbol=score.symbol,
                setup=score.setup, options=score.options, thesis_fit=score.thesis_fit,
                composite=score.composite, decision=score.decision,
                conviction=None, drivers=score.drivers, risks=score.risks,
            )
        await q.put({"type": "score", "score": asdict(score),
                     "progress": round(done_steps / total_steps, 3)})

    # Ranker
    ev = AgentEvent("ranker", None, "started", None, _now_iso())
    async with db_session() as s:
        await EventRepo(s).append(run_id=run_id, agent_id="ranker", symbol=None,
                                  status="started", summary=None)
    await q.put({"type": "event", "event": asdict(ev)})
    await asyncio.sleep(0.5)

    # Build a final summary from the persisted scores.
    async with db_session() as s:
        r = await RunRepo(s).get(run_id)
        scores = sorted(r.scores, key=lambda x: x.composite, reverse=True) if r else []

    best = [sc.symbol for sc in scores[:3]]
    summary_parts: list[str] = []
    if scores:
        top = scores[0]
        summary_parts.append(
            f"Top pick: {top.symbol} (composite {top.composite:.1f})."
        )
        if top.drivers:
            summary_parts.append(f"Drivers: {'; '.join(top.drivers[:2])}.")
    held = [sc.symbol for sc in scores if sc.decision == "Hold"]
    if held:
        summary_parts.append(f"Held: {', '.join(held)}.")
    summary = " ".join(summary_parts) or "No actionable picks this run."

    async with db_session() as s:
        await EventRepo(s).append(
            run_id=run_id, agent_id="ranker", symbol=None,
            status="finished", summary=summary,
        )
        await RunRepo(s).add_report(
            run_id=run_id, theme_id=theme["id"],
            summary=summary, ranking=[sc.symbol for sc in scores],
            best_positioned=best,
        )
        await RunRepo(s).mark_done(
            run_id=run_id, summary=summary, best_positioned=best,
        )

    ev2 = AgentEvent("ranker", None, "finished", summary, _now_iso())
    await q.put({"type": "event", "event": asdict(ev2), "progress": 1.0})

    # Re-fetch and emit the terminal snapshot.
    async with db_session() as s:
        r = await RunRepo(s).get(run_id)
        await q.put({"type": "done", "run": RunRepo.to_dto(r)})  # type: ignore[arg-type]


def _synth_summary(agent_id: str, symbol: str, rng: random.Random) -> str:
    pool = {
        "market": [
            f"{symbol} is in an uptrend on the daily, holding above the 50-day with rising momentum.",
            f"{symbol} chopping in a range; awaiting a breakout above resistance to confirm.",
            f"{symbol} broke down through the 50-day with expanding volume — caution.",
        ],
        "fundamentals": [
            "Revenue growth re-accelerating, margins expanding YoY, balance sheet healthy.",
            "Margins compressing this quarter; watching guidance closely.",
            "Cash flow strong, buybacks active, no material balance-sheet concerns.",
        ],
        "news": [
            "No material news; macro tape is constructive into next CPI print.",
            "Earnings in 9 sessions — historical move ~6%.",
            "Recent product cycle commentary positive; sell-side raising estimates.",
        ],
        "options": [
            "Bullish premium 3:1 vs bearish today; gamma wall sits ~3% above spot.",
            f"Heavy put buying near-term; max-pain pulling toward {rng.randint(80, 200)}.",
            "Balanced flow; no clear directional bias from large premium today.",
        ],
        "social": [
            "Mention velocity steady; tone tilting positive over the past week.",
            "Hype spike fading — likely retail-driven, not institutional.",
            "Sentiment quietly improving with no clear catalyst yet.",
        ],
        "bull": [
            "Setup, fundamentals, and flow all align — case to add on weakness.",
            "Trend + earnings momentum create an attractive risk/reward into the catalyst.",
        ],
        "bear": [
            "Gamma traps and stretched positioning create downside risk into next week.",
            "Macro headwinds plus elevated valuation cap upside — prefer to wait.",
        ],
        "research_manager": [
            f"Bull case stronger; conviction {rng.randint(3, 5)}/5.",
            f"Mixed evidence; conviction {rng.randint(1, 3)}/5 — leaning Hold.",
        ],
        "trader": [
            "Plan: long with stop ~3% below entry, target ~7% above.",
            "No actionable plan — wait for confirmation.",
        ],
        "risk_aggressive": ["Setup is strong; willing to size up to full position."],
        "risk_conservative": ["Vol is elevated; recommend half size with tighter stop."],
        "risk_neutral": ["Balanced view: standard size, standard stop."],
        "portfolio_manager": [
            "Approved at the proposed size.",
            "Approved at half size given risk debate.",
            "Held — conviction below threshold.",
        ],
        "scorecard": [f"Score recorded for {symbol}."],
    }
    return rng.choice(pool.get(agent_id, [f"{agent_id} reasoning complete."]))


def _synth_score(symbol: str, rng: random.Random) -> SymbolScore:
    setup = round(rng.uniform(4.0, 9.5), 1)
    options = round(rng.uniform(3.5, 9.0), 1)
    thesis = round(rng.uniform(5.0, 9.5), 1)
    composite = round(0.4 * setup + 0.3 * options + 0.3 * thesis, 1)
    decision = "Buy" if composite >= 7.0 else "Hold" if composite >= 5.5 else "Avoid"
    drivers = rng.sample(
        ["Strong multi-timeframe trend", "Bullish options premium tilt",
         "Re-accelerating revenue", "Improving margins",
         "Constructive macro tape", "Earnings catalyst in window",
         "High thesis fit"], k=3,
    )
    risks = rng.sample(
        ["Gamma wall above spot", "Macro CPI print risk",
         "Elevated implied volatility", "Crowded long positioning",
         "Sector rotation risk"], k=2,
    )
    return SymbolScore(symbol, setup, options, thesis, composite, decision, drivers, risks)
