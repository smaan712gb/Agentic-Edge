"""Regression tests for deleting a theme (2026-08-17).

Deleting failed outright for any theme that had ever been run:

    IntegrityError: NOT NULL constraint failed: runs.theme_id

The ``Theme.runs`` relationship carries no cascade, so SQLAlchemy tried to
ORPHAN the runs by nulling their ``theme_id`` — a column declared NOT NULL.
Only never-run themes could be deleted, the exact opposite of what an operator
wants: the themes worth removing are the ones with history. On this book that
was 18 of 21 themes, each with ~50 runs.

The database cannot help. SQLite enforces foreign keys only under
``PRAGMA foreign_keys=ON``, which this app never sets, so the schema's
ON DELETE CASCADE clauses are decorative at runtime and every dependent row
has to be removed explicitly.

Source-level assertions: the delete path needs a live async session and a
populated graph, so these pin the invariants that made it wrong, and the
ordering the fix depends on.
"""

from __future__ import annotations

import inspect

from api.app.repos.themes import ThemeRepo


def _src() -> str:
    return inspect.getsource(ThemeRepo.delete)


def test_trade_intents_are_detached_never_deleted():
    """The single most dangerous cascade in this graph.

    trade_intents reference runs and carry the record of real positions,
    including OPEN ones — for a live LEAP the intent is the only row that knows
    the system owns it. Its FK is SET NULL by design. The theme deleted in
    testing had exactly one intent hanging off its runs; a cascade would have
    destroyed it silently.
    """
    src = _src()
    assert "update(TradeIntent)" in src, "intents must be UPDATEd, not deleted"
    assert "values(run_id=None)" in src
    assert "delete(TradeIntent)" not in src, (
        "trade intents must NEVER be deleted when removing a theme — they hold "
        "position history and may reference a live position"
    )


def test_run_children_are_removed_before_the_runs():
    """Order matters: children first, then runs, then the theme."""
    src = _src()
    for child in ("RunEvent", "TickerScore", "ThemeReport"):
        assert child in src, f"{child} rows must be cleaned up"
    assert src.index("RunEvent") < src.index("delete(Run)"), (
        "run children must be deleted before the runs they belong to"
    )


def test_theme_scoped_rows_are_cleaned_up():
    src = _src()
    assert "ThemeRotation" in src
    assert "NewsMention" in src


def test_news_is_detached_not_deleted():
    """News is shared research keyed to a ticker, not owned by one theme —
    its FK is SET NULL, so removing a theme must not destroy the article."""
    src = _src()
    assert "delete(NewsMention)" not in src
    assert "update(NewsMention)" in src


def test_missing_theme_still_returns_false():
    """The 404 path must survive the rewrite."""
    src = _src()
    assert "if not t:" in src and "return False" in src


def test_runs_relationship_still_lacks_a_cascade():
    """This is WHY the explicit cleanup exists.

    If someone later adds cascade="all, delete-orphan" to Theme.runs, the
    manual cleanup becomes redundant and this test should be revisited —
    but note lazy="raise" would then force loading the whole run graph
    (~1,600 event rows for one theme) just to delete it.
    """
    from api.app.db import Theme

    rel = Theme.__mapper__.relationships["runs"]
    assert "delete-orphan" not in (rel.cascade or ""), (
        "Theme.runs cascade changed — re-check ThemeRepo.delete for double work"
    )
