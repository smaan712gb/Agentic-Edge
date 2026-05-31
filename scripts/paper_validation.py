"""Paper-trading validation helper — preflight, arm, watch, disarm.

Drives the *running* API (start it first, see docs/paper_validation.md). All
checks are read-only by default; arming the dual kill switch is an explicit
flag so it never happens by accident.

Usage (PowerShell, API already running on :8000):
  python scripts/paper_validation.py                 # GO/NO-GO preflight
  python scripts/paper_validation.py --arm           # enable autotrade (DB half)
  python scripts/paper_validation.py --watch         # poll status every 30s
  python scripts/paper_validation.py --rearm-breaker # clear a tripped breaker
  python scripts/paper_validation.py --disarm        # hard stop (kill switch off)

Reads ADMIN_API_TOKEN + API base from settings/env. Refuses to arm unless
the connected account is paper.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from api.app.config import get_settings

BASE = __import__("os").environ.get("API_BASE", "http://127.0.0.1:8000")


def _token() -> str:
    tok = get_settings().ADMIN_API_TOKEN
    if not tok:
        print("ERROR: ADMIN_API_TOKEN not set — admin endpoints need it.")
        sys.exit(2)
    return tok


def _get(path: str, admin: bool = False) -> dict:
    headers = {"X-Admin-Token": _token()} if admin else {}
    r = httpx.get(f"{BASE}{path}", headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    r = httpx.post(f"{BASE}{path}", headers={"X-Admin-Token": _token()}, json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _mark(ok: bool) -> str:
    return "[ OK ]" if ok else "[FAIL]"


def preflight() -> bool:
    print(f"=== Paper validation preflight ({BASE}) ===")
    go = True

    try:
        h = _get("/api/health")
    except Exception as e:
        print(f"[FAIL] API not reachable at {BASE} — start it first ({e})")
        return False

    ibkr_mode = (h.get("mode") or {}).get("ibkr")
    connected = h.get("ibkr_connected")
    mock = (h.get("mode") or {}).get("mock_data")
    print(f"{_mark(ibkr_mode == 'paper')} IBKR mode = {ibkr_mode} (must be 'paper')")
    print(f"{_mark(bool(connected))} IBKR connected = {connected}")
    print(f"{_mark(not mock)} MOCK_DATA off = {not mock}")
    go = go and ibkr_mode == "paper" and bool(connected) and not mock

    try:
        perf = _get("/api/performance/today")
        eq = perf.get("equity")
        print(f"{_mark(bool(eq))} account equity readable = ${eq:,.0f}" if eq else
              f"[FAIL] account equity not readable: {perf.get('error')}")
        go = go and bool(eq)
    except Exception as e:
        print(f"[WARN] performance/today errored: {e}")

    try:
        st = _get("/api/admin/autotrade/status", admin=True)
        eff = st.get("effective_enabled")
        tripped = st.get("entry_breaker_tripped")
        print(f"[INFO] autotrade effective_enabled = {eff} "
              f"(env={st.get('env_autotrade_enabled')}, db={st.get('db_autotrade_enabled')})")
        print(f"{_mark(not tripped)} entry circuit breaker tripped = {tripped}"
              + (f"  reason: {st.get('entry_breaker_reason')}" if tripped else ""))
        go = go and not tripped
    except Exception as e:
        print(f"[FAIL] admin status errored (token set?): {e}")
        go = False

    try:
        mgrs = _get("/api/managers")
        seeded = sum(1 for m in mgrs if m.get("active"))
        print(f"[INFO] hedge-fund managers active = {seeded}/{len(mgrs)}")
    except Exception as e:
        print(f"[WARN] managers endpoint errored: {e}")

    print("\n" + ("GO — safe to arm on paper (run with --arm)" if go
                  else "NO-GO — resolve the [FAIL] items above before arming"))
    return go


def watch(interval: int = 30) -> None:
    print(f"=== Watching autotrade + breaker every {interval}s (Ctrl-C to stop) ===")
    while True:
        try:
            st = _get("/api/admin/autotrade/status", admin=True)
            perf = _get("/api/performance/today")
            eq = perf.get("equity")
            print(f"{time.strftime('%H:%M:%S')}  enabled={st.get('effective_enabled')} "
                  f"breaker_tripped={st.get('entry_breaker_tripped')} "
                  f"equity={f'${eq:,.0f}' if eq else '?'}"
                  + (f"  BREAKER: {st.get('entry_breaker_reason')}"
                     if st.get('entry_breaker_tripped') else ""))
        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')}  poll error: {e}")
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="Paper-trading validation helper.")
    ap.add_argument("--arm", action="store_true", help="Enable autotrade (DB half of the dual switch)")
    ap.add_argument("--disarm", action="store_true", help="Hard stop — flip the kill switch off")
    ap.add_argument("--rearm-breaker", action="store_true", help="Clear a tripped entry circuit breaker")
    ap.add_argument("--watch", action="store_true", help="Poll status + equity continuously")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    if args.disarm:
        print(_post("/api/admin/autotrade/disable", {"reason": "paper validation disarm", "actor": "validation"}))
        return 0
    if args.rearm_breaker:
        print(_post("/api/admin/autotrade/rearm-breaker", {"actor": "validation"}))
        return 0
    if args.arm:
        if not preflight():
            print("Refusing to arm — preflight NO-GO.")
            return 1
        print(_post("/api/admin/autotrade/enable", {"reason": "paper validation", "actor": "validation"}))
        print("Armed (DB half). Ensure AUTOTRADE_ENABLED=true in .env for the env half to take effect.")
        return 0
    if args.watch:
        watch(args.interval)
        return 0
    return 0 if preflight() else 1


if __name__ == "__main__":
    raise SystemExit(main())
