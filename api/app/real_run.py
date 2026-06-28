"""Real-graph run executor.

Translates the agent ``ThemeRunner``'s ``RunEvent`` stream into:
  * the in-memory SSE queue (live UI consumers)
  * persistent rows in ``run_events`` / ``ticker_scores`` / ``theme_reports``

The frontend contract is shared with ``_simulate_run`` in ``main.py`` so
the UI doesn't know which path produced a run.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .config import get_settings
from .db import get_session as db_session
from .main import AgentEvent, RUN_QUEUES, SymbolScore  # type: ignore[attr-defined]
from .repos import EventRepo, RunRepo

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def real_run(run_id: str, theme: dict[str, Any]) -> None:
    """Execute a real DeepSeek+providers theme run; persist + stream events.

    Two runners are available:

    * FastThemeRunner (default) — ~3 LLM calls per theme + 1 per ticker.
      A 6-ticker theme run completes in 15-60 seconds. Production-grade
      latency. Uses deepseek-v4-flash with thinking disabled + JSON output.
      Same output schema as the slow runner.
    * ThemeRunner (legacy) — full LangGraph multi-agent pipeline with
      bull/bear debate, risk panel, etc. ~15 LLM calls per ticker;
      30-90 minutes per theme. Demo-grade. Kept for research mode.

    Toggle via env: USE_FAST_SCORER=false to fall back to the slow path.
    """
    import os
    use_fast = (os.environ.get("USE_FAST_SCORER", "true").lower()
                in ("true", "1", "yes", "on"))
    if use_fast:
        from tradingagents.scorecard.fast_runner import (
            RunEvent, FastThemeRunner as _RunnerCls,
        )
    else:
        from tradingagents.scorecard.runner import (
            RunEvent, ThemeRunner as _RunnerCls,
        )
    from tradingagents.scorecard.schema import ThemeInput

    q = RUN_QUEUES[run_id]

    if not theme["symbols"]:
        async with db_session() as s:
            await RunRepo(s).mark_error(run_id, "Theme has no symbols.")
            await EventRepo(s).append(
                run_id=run_id, agent_id="system", symbol=None,
                status="finished", summary="Theme has no symbols.",
            )
        await q.put({"type": "done", "run": {"id": run_id, "status": "error"}})
        return

    # Quant overlay — autonomously fold the research factory into the decision.
    # Ensure today's feature snapshot exists (same-day freshness), then build the
    # per-ticker quant-signal block the scorer weighs. Best-effort: a failure
    # here must never block a run — the scorer simply scores without the block.
    extra_context: dict[str, str] = {}
    if get_settings().QUANT_OVERLAY_ENABLED:
        try:
            from .research.features import ensure_todays_snapshot
            from .research.overlay import build_extra_context
            await ensure_todays_snapshot()
            extra_context = await build_extra_context(list(theme["symbols"]))
        except Exception as e:
            logger.warning("quant overlay unavailable for run %s (%s) — scoring without it",
                           run_id, e)

    theme_in = ThemeInput(
        id=theme["id"], name=theme["name"],
        thesis=theme["thesis"], chokepoint=theme.get("chokepoint", ""),
        tickers=list(theme["symbols"]),
        extra_context=extra_context,
    )

    # Per-ticker finished-event count differs by runner:
    #   * FastThemeRunner emits 8 per ticker (market, fundamentals, news,
    #     options, social, research_manager, trader, scorecard) + 1 ranker.
    #   * Slow ThemeRunner emits ~14 (5 analysts + bull + bear + manager +
    #     trader + 3 risk + PM + scorecard) + 1 ranker.
    UNITS_PER_TICKER = 8 if use_fast else 14
    total_units = UNITS_PER_TICKER * len(theme_in.tickers) + 1
    done_units = {"n": 0}

    async def push_event(agent_id: str, symbol: str | None, status: str,
                         summary: str | None) -> None:
        ev = AgentEvent(
            agent_id=agent_id, symbol=symbol, status=status,
            summary=summary, timestamp=_now_iso(),
        )
        # Persist
        async with db_session() as s:
            await EventRepo(s).append(
                run_id=run_id, agent_id=agent_id, symbol=symbol,
                status=status, summary=summary,
            )
            if status == "finished":
                done_units["n"] += 1
                progress = min(0.999, round(done_units["n"] / total_units, 3))
                await RunRepo(s).update_progress(run_id, progress)
        # Stream
        msg: dict[str, Any] = {"type": "event", "event": asdict(ev)}
        if status == "finished":
            msg["progress"] = min(0.999, round(done_units["n"] / total_units, 3))
        await q.put(msg)

    async def on_event(ev: "RunEvent") -> None:
        if ev.type == "agent_started":
            await push_event(ev.agent_id or "?", ev.ticker, "started", None)

        elif ev.type == "agent_finished":
            await push_event(ev.agent_id or "?", ev.ticker, "finished", ev.summary)

        elif ev.type == "ticker_scored" and ev.score:
            score_dto = SymbolScore(
                symbol=ev.score.ticker,
                setup=ev.score.mtf_setup,
                options=ev.score.options_sentiment,
                thesis_fit=ev.score.thesis_fit,
                composite=ev.score.composite,
                decision=ev.score.decision,
                drivers=list(ev.score.drivers),
                risks=list(ev.score.risks),
            )
            async with db_session() as s:
                await RunRepo(s).add_score(
                    run_id=run_id, symbol=score_dto.symbol,
                    setup=score_dto.setup, options=score_dto.options,
                    thesis_fit=score_dto.thesis_fit, composite=score_dto.composite,
                    decision=score_dto.decision, conviction=ev.score.conviction,
                    drivers=score_dto.drivers, risks=score_dto.risks,
                    rationale=ev.score.rationale,
                )
            await q.put({
                "type": "score",
                "score": asdict(score_dto),
                "progress": min(0.999, round(done_units["n"] / total_units, 3)),
            })

        elif ev.type == "ranker_started":
            await push_event("ranker", None, "started", None)

        elif ev.type == "ranker_finished" and ev.report:
            best = list(ev.report.best_positioned)
            async with db_session() as s:
                await RunRepo(s).add_report(
                    run_id=run_id, theme_id=theme["id"],
                    summary=ev.report.summary,
                    ranking=list(ev.report.ranking),
                    best_positioned=best,
                )
            await push_event("ranker", None, "finished", ev.report.summary)

        elif ev.type == "error":
            await push_event(
                ev.agent_id or "system", ev.ticker, "finished",
                f"⚠ {ev.error or 'unknown error'}",
            )

    runner = _RunnerCls(on_event=on_event)
    try:
        report = await runner.run(theme_in)
        async with db_session() as s:
            await RunRepo(s).mark_done(
                run_id=run_id,
                summary=report.summary,
                best_positioned=list(report.best_positioned),
            )
    except Exception as e:
        logger.exception("Real run failed: %s", e)
        async with db_session() as s:
            await RunRepo(s).mark_error(run_id, f"{type(e).__name__}: {e}")
            await EventRepo(s).append(
                run_id=run_id, agent_id="system", symbol=None,
                status="finished", summary=f"⚠ Run failed: {e}",
            )
        ev = AgentEvent("system", None, "finished",
                        f"⚠ Run failed: {e}", _now_iso())
        await q.put({"type": "event", "event": asdict(ev)})
    finally:
        async with db_session() as s:
            r = await RunRepo(s).get(run_id)
            payload = RunRepo.to_dto(r) if r else {"id": run_id}
        await q.put({"type": "done", "run": payload})
