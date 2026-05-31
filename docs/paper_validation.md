# Paper-trading validation runbook

The gate before any live cutover. Run the full autotrade stack against the
**paper** IBKR account for several sessions, confirm every loop behaves, then
(separately, deliberately) plan the live switch. Nothing here trades real money.

> Helper script: `scripts/paper_validation.py` (preflight / arm / watch / disarm).

## 0. Prerequisites
- IB Gateway running in **paper** mode (port **4002**), API enabled.
- `.env` has the paper wiring (`IB_PORT=4002`, `IB_PAPER_TRADING=true`), a real
  `ADMIN_API_TOKEN`, `DEEPSEEK_API_KEY` + provider keys, and
  `EDGAR_USER_AGENT_EMAIL` for the manager tracker.
- `MOCK_DATA` and `USE_MOCK_RUN` **unset/false** (we want the real broker + data).

## 1. Start the stack
```powershell
python -m alembic -c api/alembic.ini upgrade head   # should report 0009 (head)
python -m uvicorn api.app.main:app --port 8000
```
Lifespan pre-binds IBKR to the loop, seeds managers from `managers.toml`, and
starts the scheduler + entry/maint/heartbeat loops. **Autotrade is still OFF**
(dual switch defaults off), so no orders fire yet.

## 2. Preflight (read-only GO/NO-GO)
```powershell
python scripts/paper_validation.py
```
Confirms: IBKR mode = paper, broker connected, `MOCK_DATA` off, account equity
readable, circuit breaker not tripped, managers seeded. Must print **GO**.

## 3. Arm on paper (deliberate, dual switch)
The system needs *both* halves to trade:
1. Env half: set `AUTOTRADE_ENABLED=true` in `.env` and restart the API.
2. DB half: `python scripts/paper_validation.py --arm` (re-runs preflight, then
   enables) — or `POST /api/admin/autotrade/enable`.

## 4. Watch a session
```powershell
python scripts/paper_validation.py --watch
```
What good looks like:
- **Entry loop**: only fires during RTH; respects the circuit breaker (day-open
  NAV captured at the open); entries go through the walking-limit executor;
  slippage-vs-mid alerts are sane.
- **Circuit breaker**: on a severe account breach (intraday NAV −12%, margin
  cushion <10%, or a disconnected broker) it **pauses new entries only** and
  latches — open positions keep being managed. Re-arm with
  `python scripts/paper_validation.py --rearm-breaker`.
- **Maintenance loop**: exits/trims fire on **signals** (thesis break, momentum
  exhaustion, exit-pressure, theme health) — never on a plain down day.
- **Off-theme sweep**: stays quiet unless a non-theme name is *also* weakening
  (thesis break or below its 20d MA). Off-theme winners are retained.
- **Manager tracker**: `/managers` populated; EDGAR sweep logs every 15 min.

Cross-check the audit trail: `auto_actions` + `trade_audit_log` rows, and
`/api/admin/autotrade/status` for breaker state.

## 5. Acceptance criteria (before considering live)
- [ ] Several clean RTH sessions with entries + maintenance behaving as above.
- [ ] Circuit breaker verified (force a trip in a scratch test, confirm new
      entries pause and positions are untouched; re-arm works).
- [ ] No forced closes on red days — every close traces to a named signal.
- [ ] Slippage telemetry within tolerance; no stuck `submitting` intents.
- [ ] Option P&L / short-call deltas read live (not cost basis).

## 6. Stop / rollback
- Hard stop: `python scripts/paper_validation.py --disarm` (or the UI kill
  switch) — flips the DB half off immediately. Open positions remain; the
  maintenance loop keeps managing them.
- Full halt: also set `AUTOTRADE_ENABLED=false` and restart.

## 7. Live cutover (later, deliberate — NOT part of this runbook)
Going live is a code change, not a toggle: the IBKR provider refuses any
account whose id doesn't start with `D` (paper). Live requires changing that
guard, `IBKR_MODE=live`, the live Gateway (port 4001), and a conscious re-arm.
Do this only after the criteria above are met, and start with conservative caps.
