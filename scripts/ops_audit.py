"""Hourly operations audit — find the step that did NOT happen.

The health monitor answers "is anything broken right now". This answers a
different and harder question: "did every step that should have run,
run?" Those fail differently. A stalled loop is loud; a cron tick that
never fired is silent, and on 2026-08-19 that silence cost the 16:05
rotation sweep and the 16:20 portfolio decision — which is how the book
opened un-gated the next morning with a 41h-stale basket index.

Every check states what it expected, what it found, and what to do. A
check that cannot determine an answer says UNKNOWN rather than OK,
because "no news" is exactly how the 2026-08-19 gap looked.

Usage:
    python -m scripts.ops_audit            # human-readable
    python -m scripts.ops_audit --json     # machine-readable
    python -m scripts.ops_audit --quiet    # only FAIL/WARN lines

Exit code is 0 when nothing is worse than INFO, 1 on WARN, 2 on FAIL, so
it can drive an alert without parsing anything.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "agentic_edge.db"
ET = ZoneInfo("America/New_York")

OK, INFO, WARN, FAIL, UNKNOWN = "OK", "INFO", "WARN", "FAIL", "UNKNOWN"
_RANK = {OK: 0, INFO: 0, UNKNOWN: 1, WARN: 1, FAIL: 2}


@dataclass
class Finding:
    check: str
    status: str
    detail: str
    fix: str = ""


@dataclass
class Audit:
    findings: list[Finding] = field(default_factory=list)

    def add(self, check: str, status: str, detail: str, fix: str = "") -> None:
        self.findings.append(Finding(check, status, detail, fix))

    @property
    def worst(self) -> str:
        return max((f.status for f in self.findings), key=lambda s: _RANK.get(s, 0), default=OK)


def now_et() -> datetime:
    return datetime.now(ET)


def _naive_utc(dt: datetime) -> datetime:
    """DB timestamps are naive UTC; compare like with like."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def is_rth(t: datetime) -> bool:
    if t.weekday() >= 5:
        return False
    return dtime(9, 30) <= t.time() <= dtime(16, 0)


def is_trading_day(t: datetime) -> bool:
    return t.weekday() < 5


def q(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_loops(con: sqlite3.Connection, a: Audit, t: datetime) -> None:
    """Both trading loops must be writing during RTH.

    Cadence is the point, not presence. A loop that ran an hour ago looks
    identical in the DB to one running now unless you measure the age.
    """
    rth = is_rth(t)
    # (loop, action_type filter, max age in minutes during RTH)
    expectations = [
        ("maintenance", "heartbeat", 12.0),
        ("entry", None, 15.0),
    ]
    for loop, action, max_age in expectations:
        sql = "SELECT MAX(timestamp) FROM auto_actions WHERE loop=?"
        args: tuple = (loop,)
        if action:
            sql += " AND action_type=?"
            args += (action,)
        rows = q(con, sql, args)
        newest = rows[0][0] if rows and rows[0][0] else None
        label = f"loop:{loop}"
        if newest is None:
            a.add(label, UNKNOWN, "no rows have ever been written",
                  "Confirm the loop starts in the uvicorn lifespan.")
            continue
        age = (_naive_utc(t) - datetime.fromisoformat(newest)).total_seconds() / 60.0
        if not rth:
            a.add(label, INFO, f"last activity {age:.0f} min ago (outside RTH — idle is correct)")
        elif age > max_age:
            a.add(label, FAIL, f"last activity {age:.0f} min ago, expected <= {max_age:.0f}",
                  "Loop is not ticking. Check the backend window for a traceback, "
                  "then restart-all.ps1 restart.")
        else:
            a.add(label, OK, f"active, last write {age:.0f} min ago")


def check_rotation(con: sqlite3.Connection, a: Audit, t: datetime) -> None:
    """Rotation must be fresh whenever entries can fire.

    Staleness is fail-open by design: the loops ignore old rows rather
    than blocking. That is the right call and it is also why nothing
    stops trading when this breaks — it has to be checked explicitly.
    """
    rows = q(con, "SELECT MAX(computed_at) FROM theme_rotation")
    newest = rows[0][0] if rows and rows[0][0] else None
    if newest is None:
        a.add("rotation:freshness", FAIL, "no rotation rows exist — book is un-gated",
              "Run start-all.ps1 run-themes, then confirm the 09:31 cron.")
        return
    age_h = (_naive_utc(t) - datetime.fromisoformat(newest)).total_seconds() / 3600.0
    if is_rth(t) and age_h > 6.0:
        a.add("rotation:freshness", FAIL,
              f"{age_h:.1f}h old (limit 6h) during RTH — entries are NOT rotation-gated",
              "A tick was missed. The boot/watchdog catch-up should recover it; "
              "if not, the sweep itself is failing — check the backend log.")
    elif is_rth(t):
        a.add("rotation:freshness", OK, f"{age_h:.1f}h old — book is gated")
    else:
        a.add("rotation:freshness", INFO, f"{age_h:.1f}h old (outside RTH — not gating anything)")

    # Is the gate actually being consulted? Fresh rows that nothing reads
    # would look healthy while gating nothing.
    if is_rth(t):
        today = _naive_utc(t).strftime("%Y-%m-%d")
        hits = q(con, "SELECT COUNT(*) FROM auto_actions WHERE loop='entry' "
                      "AND action_type LIKE '%rotation%' AND timestamp >= ?", (today,))
        n = hits[0][0] if hits else 0
        a.add("rotation:gate-applied", INFO if n else UNKNOWN,
              f"{n} rotation gate decisions logged today"
              + ("" if n else " — no entry attempts yet, or the gate is not wired"))


def check_missed_jobs(con: sqlite3.Connection, a: Audit, t: datetime) -> None:
    """Scheduled work that should have produced a row today, and did not.

    This is the check that would have caught 2026-08-19. Each entry is
    (label, ET deadline, SQL probe). A job is only judged after its
    deadline has passed today.
    """
    today = _naive_utc(t).strftime("%Y-%m-%d")
    jobs = [
        # `runs` timestamps with started_at, and only status='done' proves the
        # run actually produced signals — a row that started and died is not
        # a run that happened.
        ("daily theme run", dtime(9, 30),
         "SELECT COUNT(*) FROM runs WHERE started_at >= ? AND status='done'", (today,)),
        ("rotation 09:31 tick", dtime(9, 40),
         "SELECT COUNT(*) FROM theme_rotation WHERE computed_at >= ?", (today,)),
        ("portfolio decision 16:20", dtime(16, 35),
         "SELECT COUNT(*) FROM auto_actions WHERE loop='portfolio' AND timestamp >= ?", (today,)),
    ]
    if not is_trading_day(t):
        a.add("jobs:missed", INFO, "not a trading day — no scheduled work expected")
        return
    for label, deadline, sql, args in jobs:
        if t.time() < deadline:
            a.add(f"job:{label}", INFO, f"not due yet (deadline {deadline.strftime('%H:%M')} ET)")
            continue
        rows = q(con, sql, args)
        n = rows[0][0] if rows else 0
        if n:
            a.add(f"job:{label}", OK, f"ran today ({n} row(s))")
        else:
            a.add(f"job:{label}", FAIL,
                  f"deadline {deadline.strftime('%H:%M')} ET passed with no output",
                  "The server was probably down at the tick. Confirm the watchdog "
                  "is running, then fire the job manually.")


def check_feeds(con: sqlite3.Connection, a: Audit, t: datetime) -> None:
    """Feeds that have gone silent or flatlined.

    A flatline is the dangerous one: the row count keeps rising so every
    freshness check passes, while the provider serves one cached value.
    """
    # Readings are audit rows, not their own table: loop='feeds', feed name in
    # `outcome`, value inside the JSON payload.
    rows = q(con, "SELECT outcome, MAX(timestamp) FROM auto_actions "
                  "WHERE loop='feeds' GROUP BY outcome")
    if not rows:
        a.add("feeds", UNKNOWN, "no feed observations recorded",
              "Check FEED_INTEGRITY_ENABLED and that observe() callers are running.")
        return
    stale = []
    for feed, newest in rows:
        if not newest:
            continue
        age_h = (_naive_utc(t) - datetime.fromisoformat(newest)).total_seconds() / 3600.0
        if age_h > 24:
            stale.append(f"{feed} ({age_h:.0f}h)")
    if stale:
        a.add("feeds:silent", WARN, f"{len(stale)} of {len(rows)} feed(s) >24h old: "
              + ", ".join(sorted(stale)[:5]),
              "Decision-support only unless a gate reads them — but a silent feed "
              "that a gate DOES read is a silent gate.")
    else:
        a.add("feeds:silent", OK, f"{len(rows)} feeds, none over 24h")

    # Flatline is the dangerous shape: rows keep arriving so every freshness
    # check passes, while the provider serves one cached value forever.
    flat = []
    for feed, _ in rows:
        vals = q(con, "SELECT payload FROM auto_actions WHERE loop='feeds' AND outcome=? "
                      "ORDER BY timestamp DESC LIMIT 5", (feed,))
        nums = []
        for (payload,) in vals:
            try:
                v = json.loads(payload).get("numeric")
            except (TypeError, ValueError):
                continue
            if v is not None:
                nums.append(v)
        if len(nums) >= 4 and len(set(nums)) == 1:
            flat.append(f"{feed}={nums[0]}")
    if flat:
        a.add("feeds:flatline", WARN,
              f"{len(flat)} feed(s) identical across last 5 readings: " + ", ".join(sorted(flat)[:5]),
              "The provider is almost certainly serving cached data. Freshness "
              "checks cannot catch this — the rows keep arriving.")
    else:
        a.add("feeds:flatline", OK, "no flatlined feeds")


def check_integrity(con: sqlite3.Connection, a: Audit, t: datetime) -> None:
    """Position/intent bookkeeping — the errors that cost money quietly."""
    today = _naive_utc(t).strftime("%Y-%m-%d")
    dupes = q(con, "SELECT COUNT(*) FROM auto_actions WHERE action_type LIKE '%duplicate%' "
                   "AND timestamp >= ?", (today,))
    n = dupes[0][0] if dupes else 0
    a.add("integrity:duplicate-intents", INFO if n else OK,
          f"{n} duplicate intent(s) retired today"
          + (" — normal housekeeping, but a rising count means something re-creates them" if n else ""))

    for kind in ("orphan", "phantom"):
        rows = q(con, "SELECT COUNT(*) FROM auto_actions WHERE action_type LIKE ? "
                      "AND timestamp >= ?", (f"%{kind}%", today))
        c = rows[0][0] if rows else 0
        a.add(f"integrity:{kind}s", WARN if c else OK,
              f"{c} {kind} event(s) today",
              "Reconcile: start-all.ps1 reconcile" if c else "")


def check_errors(a: Audit, t: datetime) -> None:
    """Exceptions since the open — including ones swallowed into a blank."""
    log = ROOT / "logs" / "agentic_edge.log"
    if not log.exists():
        a.add("errors:log", UNKNOWN, "log file not found")
        return
    today = t.strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    blank = 0
    try:
        with log.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith(today):
                    continue
                if " ERROR " in line or "CRITICAL" in line or "Traceback" in line:
                    key = line.split(":", 3)[-1].strip()[:70] or "(blank)"
                    counts[key] = counts.get(key, 0) + 1
                # An exception logged with an empty message cannot be diagnosed.
                if "failed: " in line and line.rstrip().endswith("failed:"):
                    blank += 1
    except OSError as e:
        a.add("errors:log", UNKNOWN, f"could not read log: {e}")
        return

    if counts:
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
        a.add("errors:today", WARN,
              "; ".join(f"{v}x {k}" for k, v in top),
              "Recurring broker errors (10197) usually mean a competing IBKR session.")
    else:
        a.add("errors:today", OK, "no ERROR/CRITICAL lines today")

    if blank:
        a.add("errors:swallowed", WARN,
              f"{blank} exception(s) logged with an empty message",
              "These are undiagnosable by construction — log repr(e), not str(e).")


def check_supervisor(a: Audit) -> None:
    """Is anything going to restart the stack if it dies?

    Every stale-data finding today traced back to a shutdown nobody
    recovered, so this is a first-class check, not an afterthought.
    """
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='pwsh.exe'\" | "
             "Where-Object { $_.CommandLine -like \"*start-all.ps1' watchdog*\" } | "
             "Measure-Object).Count"],
            capture_output=True, text=True, timeout=25,
        )
        n = int((out.stdout or "0").strip() or 0)
    except Exception as e:
        a.add("supervisor:watchdog", UNKNOWN, f"could not check: {e}")
        return
    if n:
        a.add("supervisor:watchdog", OK, "watchdog running — stack relaunches within 5 min")
    else:
        a.add("supervisor:watchdog", FAIL,
              "NO watchdog — a crash goes unrecovered until someone notices",
              "start-all.ps1 watchdog  (in its own window)")


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def run() -> Audit:
    a = Audit()
    t = now_et()
    if not DB.exists():
        a.add("db", FAIL, f"{DB} not found")
        return a
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        check_loops(con, a, t)
        check_rotation(con, a, t)
        check_missed_jobs(con, a, t)
        check_feeds(con, a, t)
        check_integrity(con, a, t)
    finally:
        con.close()
    check_errors(a, t)
    check_supervisor(a)
    return a


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="only WARN/FAIL/UNKNOWN")
    args = p.parse_args()

    a = run()
    t = now_et()

    if args.json:
        print(json.dumps({
            "at": t.isoformat(), "rth": is_rth(t), "worst": a.worst,
            "findings": [f.__dict__ for f in a.findings],
        }, indent=2))
    else:
        print("=" * 72)
        print(f"AGENTIC EDGE — ops audit   {t:%Y-%m-%d %H:%M:%S %Z}"
              f"   ({'RTH' if is_rth(t) else 'outside RTH'})")
        print("=" * 72)
        for f in a.findings:
            if args.quiet and f.status in (OK, INFO):
                continue
            print(f"  [{f.status:<7}] {f.check:<28} {f.detail}")
            if f.fix and f.status in (WARN, FAIL, UNKNOWN):
                print(f"            -> {f.fix}")
        print("-" * 72)
        counts = {s: sum(1 for f in a.findings if f.status == s) for s in (OK, INFO, WARN, FAIL, UNKNOWN)}
        print("  " + "  ".join(f"{k}={v}" for k, v in counts.items() if v))
        print(f"  worst: {a.worst}")

    return {OK: 0, INFO: 0, UNKNOWN: 1, WARN: 1, FAIL: 2}[a.worst]


if __name__ == "__main__":
    sys.exit(main())
