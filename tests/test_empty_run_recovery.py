"""The 2026-08-19 empty-batch incident.

A local socket filter refused every outbound call for ~17h. At 09:00 ET the
daily theme batch ran anyway: ``score_one_ticker`` and ``theme_health`` were
refused for all 73 symbols, so all nine themes stored ZERO ticker_scores.

Nothing said so. Each run was written ``status="done"``; the scheduler
recorded ``last_status="ok"``; the Runs tab showed nine completed runs whose
only tell was an em-dash in "Best positioned". Worse, the empty runs then
held the day's idempotency slot, so the cron, the missed-run watchdog and a
hand-fired ``run-themes`` all skipped them — the last reporting
``0 ok, 9 skipped, 0 failed (status=ok)``. The signals could not be
regenerated until the next calendar day.

Offline — no DB, no broker, no network.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from api.app.timefmt import iso_utc


# ---------------------------------------------------------------------------
# iso_utc — naive stored UTC must not reach a browser without an offset
# ---------------------------------------------------------------------------


def test_naive_datetime_is_stamped_utc():
    """ECMAScript parses an offset-less date-time as LOCAL, which rendered the
    09:00 ET batch as 1:01 PM on the Runs tab."""
    out = iso_utc(datetime(2026, 8, 19, 13, 1, 15, 283484))
    assert out.endswith("+00:00"), f"no UTC offset in {out!r}"
    assert out.startswith("2026-08-19T13:01:15")


def test_aware_datetime_is_converted_not_relabelled():
    from datetime import timedelta
    aware = datetime(2026, 8, 19, 9, 1, 15, tzinfo=timezone(timedelta(hours=-4)))
    assert iso_utc(aware) == "2026-08-19T13:01:15+00:00"


def test_none_passes_through():
    assert iso_utc(None) is None


# ---------------------------------------------------------------------------
# mark_done — zero scores is not success
# ---------------------------------------------------------------------------


class _FakeRun:
    def __init__(self):
        self.status = "running"
        self.progress = 0.0
        self.finished_at = None
        self.summary = None
        self.best_positioned = None
        self.error = None


class _FakeSession:
    """Enough of AsyncSession for RunRepo.mark_done: get() + scalar(count)."""

    def __init__(self, run, n_scores):
        self._run, self._n = run, n_scores

    async def get(self, _model, _pk):
        return self._run

    async def scalar(self, _stmt):
        return self._n


def _mark_done(n_scores):
    from api.app.repos.runs import RunRepo
    run = _FakeRun()
    repo = RunRepo(_FakeSession(run, n_scores))
    asyncio.run(repo.mark_done(
        run_id="r1", summary="a summary", best_positioned=["NVDA"]))
    return run


def test_run_that_scored_nothing_is_an_error_not_done():
    run = _mark_done(0)
    assert run.status == "error", (
        "a run that scored 0 symbols must not be indistinguishable from one "
        "that scored every symbol"
    )
    assert run.error and "0" in run.error
    assert run.finished_at is not None, "it is still finished, just not ok"


def test_run_with_scores_is_done():
    run = _mark_done(8)
    assert run.status == "done"
    assert run.error is None
    assert run.best_positioned == ["NVDA"]
    assert run.progress == 1.0


def test_empty_run_keeps_its_summary_for_diagnosis():
    """Marking it error must not throw away what the runner did report."""
    run = _mark_done(0)
    assert run.summary == "a summary"
