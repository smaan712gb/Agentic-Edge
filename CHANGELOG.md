# Changelog

Notable changes per phase. Each phase corresponds to one or more commits
on `main`; the agent-package code (`tradingagents/`) lives in a sibling
package and needs its own version-control trail.

## Unreleased

### Strategy refocus — LEAPS-only, long-only

- The platform now runs a **single long-only LEAPS playbook**. The
  long-equity and covered-LEAPS (poor-man's-covered-call) strategies, the
  short-call rolls/earnings hedges, multi-leg combo execution, and the
  stock-fallback path are retired in favour of one disciplined book of
  deep-ITM long calls. Execution is single-leg walking-limit near mid.
- **Short-dated option probing removed** in LEAPS-only mode: the
  front-month ATM-IV / momentum-exhaustion-by-IV signal no longer runs
  (it was the wrong horizon for a long-dated hold and generated needless
  option-chain churn). Exit pressure now runs on underlying-based signals.
- Exits are **signal-driven only** — thesis break, confirmed theme
  rotation, or momentum exhaustion — and never force-close on an ordinary
  down day.

### New signal layers

- **Smart-money tracker** — polls a configurable watchlist of well-known
  investors' SEC filings (13F / 13D / Form-4), stores holdings, computes
  Q/Q deltas and cross-fund overlap on chokepoint names, and surfaces the
  read (tilting conviction/sizing; activist 13D filings raise instant
  alerts).
- **Rotation detector** — catches institutions leaving a theme early
  (relative strength / breadth / flow); on confirmation it halts new
  entries into that theme and tightens exit sensitivity.
- **Account-health monitor** — periodic invariant checks (broker
  reachable, kill-switch/breaker state, position↔intent parity, margin
  cushion, signal freshness) that alert on drift.

### Reliability fixes

- Maintenance loop now manages `leap_open` positions (a stale state filter
  had excluded the entire LEAPS-only book).
- Orphan reconciliation recovers "abandoned-but-filled" LEAP orders.
- Provider cache switched to a lossless serializer (the prior one
  corrupted DataFrames/dataclasses on cache hits).
- Entry circuit breaker no longer false-trips on a cold startup snapshot
  when the broker is actually reachable.

## 2026-05-09 — Production hardening pass

Comprehensive maintenance-loop + entry-path overhaul to take the
research output all the way to a live paper-trading deployment with
production-grade observability.

### Phase A — stabilize the maintenance loop

- Heartbeat audit row written every tick so operators see the loop is alive.
- Orphan-position adoption — IBKR holdings without a `TradeIntent` get
  synthetic intents on each tick so they're monitored.
- Walking-limit cap loosened from 0.25 → 0.50 of half-spread on the
  entry path (thin LEAP combos kept abandoning at the old cap).

### Phase B — profit-preservation trim ladder

- New `profit_preservation` analyzer with the operator's exit ladder
  (+50% → 10% trim, +100% → 20–40%, +200% → 33% to recover capital).
- Strong-day gate (+3% today + 1.5× volume + RSI ≥ 70) for the lower
  bands; +200% trims regardless.
- Partial-close primitive (`_execute_stock_trim`) that decrements
  `intent.qty` without transitioning to closed.

### Phase C — theme health + thesis-break

- `theme_health` derives composite + 5-day deterioration streak on
  demand from existing Run + TickerScore data (no new tables).
- `thesis_break_signal` triggers a full exit when ALL containing themes
  for a symbol have been below the 60 floor for ≥5 trading days.

### Phase D — momentum exhaustion, rotation, exit-pressure

- `momentum_exhaustion` scores up to 7 indicators (MA distance, RSI,
  volume, gap-up, auction imbalance, insider acceleration, analyst
  upgrade-after-run); ≥50% of available signals trips.
- `rotation` engine flags when a non-held name in an active hot theme
  beats a held name's composite by ≥10 pts; surfaces as an alert,
  never auto-executes.
- `exit_pressure` produces a unified 0-100 score per position
  (30% theme + 20% profit-preservation + 20% tech-exhaustion + 15%
  options risk + 15% rotation). Bands: hold / trim_light / trim_heavy
  / aggressive.
- `position_pressure_{band}` audit row written for every monitored
  position every tick.

### Phase E — institutional-grade data feeds

- **E1**: closing-auction imbalance (IBKR generic tick 225) wired as
  the 5th exhaustion signal — only fetched for stretched names during
  the 15:50–16:00 ET window.
- **E2**: `macro_regime` overlay reads VIX + SPX live from IBKR Index
  contracts; classifies into calm / elevated / defensive / panic;
  exposes `sizing_factor`, `leap_roll_deferred`,
  `earnings_window_mult` for downstream consumers.
- **E3**: depth-aware walking-limit. New `IbkrProvider.get_market_depth`
  and `get_market_depth_by_conid` use `reqMktDepth` (NASDAQ TotalView /
  NYSE OpenBook / Cboe BZX Depth — all in the existing IBKR
  subscription). `walking_limit.py` reads per-leg L2 depth, constructs
  a synthetic combo book, and chooses a *smart starting price* — the
  level where resting size on both legs supports the contract count.
  Skips wasted walk steps through "air" between mid and where real
  liquidity sits.

### Phase F — SEC 8-K filings watcher

- Per-tick sweep of FMP `/stable/sec-filings-8k` for held + theme-
  universe symbols.
- Severity classifier (earnings / guidance / material_event /
  after_hours) using `hasFinancials` flag, time-of-day, and earnings-
  calendar proximity.
- `guidance` and `after_hours` filings on held names feed the
  thesis-break detector and force a full exit on the next per-symbol
  evaluation.

### Phase G — fundamental research feeds

- **Analyst grades** (`/stable/grades` + price-target consensus) →
  upgrade-after-major-run trips the 7th exhaustion signal.
- **13F institutional flow** (`/stable/institutional-ownership/...`)
  classifies each held name as accumulating / distributing / crowded
  per quarter; written into `position_pressure` audit payload.
- **Earnings call transcripts** — DeepSeek-pro analysis with
  `thinking: enabled` + `reasoning_effort: high` extracts structured
  guidance / demand / margin / inventory / executive signals as JSON.
  Severity-mapped back into the same `TranscriptSignal` dataclass the
  regex fallback produces. Cached by transcript content hash —
  effectively permanent per-content.
- Insider Form 4 (`/stable/insider-trading/search`) wired earlier
  (Phase D) as the 6th exhaustion signal — officer / director /
  10%-owner sales over last 30d at ≥2× the 30–180 day baseline,
  ≥2 distinct sellers, ≥$500K threshold.

### Phase H — discipline and reasoning

- Orphan adoption now gated on theme-universe membership. Random
  legacy holdings (non-themed tickers in the IBKR account) are
  intentionally left unmanaged. The framework's job is the chokepoint
  universe; everything else is out of scope.
- DeepSeek-pro transcript analyzer (see Phase G) replaces the regex
  keyword scan as the preferred path; falls back to regex when
  DEEPSEEK_API_KEY is unset or the LLM call errors.

### Phase I — entry-path readiness

- Entry loop reads `macro.sizing_factor` once per tick and applies it
  to both PMCC NAV target (7%) and stock-fallback NAV target (3%).
  Defensive regime tightens to 50%, panic blocks new entries entirely.
- Adaptive walking-limit for thin combos (>3% half-spread of mid):
  cap fraction bumps to 75% and step size scales to ~5% of half-spread.
  Yesterday's HPE / ANET / AEHR abandonment pattern should fill on
  Monday's session.
