"""Portfolio-level exposure management.

Everything above this package makes TICKER decisions: is MU a buy, has SNDK
broken its thesis, should this LEAP be trimmed. That is stock selection, and
the system was already reasonable at it.

This package makes PORTFOLIO decisions, which are a different question:
should the AI-infrastructure book as a whole be running at 65%, 85%, 100% or
somewhat above 100% exposure right now. The thesis is a multi-year supercycle,
so the objective is not to pick tops in MU or COHR — it is to stay
structurally invested while the theme is intact, convert portfolio-level
euphoria into dry powder, and redeploy it into portfolio-level dislocations.

Three tactical decisions, and only three:

    1. when to stop adding
    2. when to reduce aggregate exposure
    3. when to redeploy into the next leg

The layers:

    exposure     — what the book is ACTUALLY worth in delta terms, which is
                   not the premium paid. This is the foundation; every other
                   number here is meaningless without it.
    sleeves      — permanent core / tactical / reserve, so a trim can touch
                   the tactical layer and leave the core alone.
    cycle_score  — one top-down read of the whole complex: extension, breadth,
                   rotation, catalysts, positioning.
    states       — the exposure state machine and its target bands, moved one
                   step at a time.
"""
