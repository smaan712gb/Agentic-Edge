"""Hedge Fund Signal Tracker — named-manager EDGAR positioning.

Phase 1 decision-support layer: poll SEC EDGAR for a configured set of
legendary investors, store their 13F holdings / 13D-G / Form-4 filings,
compute quarter-over-quarter deltas and cross-fund overlap, and surface the
conviction read (dashboard + API + alerts). No auto-execution and no
automatic wiring into the scorecard/gates — the operator overlays the signal
on entry decisions.
"""
