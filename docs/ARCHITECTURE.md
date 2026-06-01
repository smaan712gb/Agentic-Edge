# Agentic Edge — Architecture

A walkthrough of how the system is organised: the agent graph, the data
providers, the persistence layer, and the boundaries between them.

This document is meant to be read by someone new to the codebase. It moves
from the user's mental model down to the code surfaces. Skim the diagrams
first, then dip into the sections that matter for what you're trying to do.

---

## 1. The graph at a glance

A **theme** is the unit of work. Each theme owns a set of tickers and a
short thesis sentence. When you trigger a run, the system fans out across
those tickers, runs the same per-ticker analysis on each in parallel, then
fans back into a ranker that orders the names by composite score.

```text
ThemeRunner
├── for each ticker in theme:                (parallel)
│     TickerSubgraph(ticker)
│       ├── Analyst fan-out
│       │   ├── Market analyst        (price action, regime)
│       │   ├── Fundamentals analyst  (filings, growth, balance sheet)
│       │   ├── News + macro analyst  (catalysts, surprise risk)
│       │   ├── Options-flow analyst  (unusual activity, gamma, max pain)
│       │   └── Theme-thesis analyst  (fit against the theme statement)
│       ├── Bull ⇄ Bear debate         (N rounds)
│       ├── Research Manager           (synthesis + conviction score)
│       ├── Trader                     (entry, sizing, levels)
│       ├── Risk panel                 (3 voices: aggressive, conservative, neutral)
│       ├── Portfolio Manager          (final go/no-go)
│       └── Scorecard scorer           (TickerScore: MTF / options / thesis)
└── ThemeRanker
      orders all TickerScores into a final ScoreReport with prose justification
```

Two structural design decisions worth knowing:

- **The conviction gate** between Research Manager and Trader requires the
  manager to score consensus (1–5). Below threshold the ticker is auto-Held
  — keeps you out of names the system feels conflicted about.
- **The bear researcher reads the options-flow report.** This lets it argue
  things like "gamma is pinning price; calls are short-vol traps." Without
  it the bear case is too easy to miss in a momentum tape.

---

## 2. Repository layout

```text
.
├── api/                              FastAPI service
│   ├── app/
│   │   ├── main.py                   App + middleware + lifespan
│   │   ├── admin.py                  Admin endpoints (kill switch, scheduler)
│   │   ├── positions.py              Live positions + equity curve
│   │   ├── trade_intents.py          Trade lifecycle (open, manage, close)
│   │   ├── scheduler.py              APScheduler bindings
│   │   ├── real_run.py               Bridges run events to the API stream
│   │   ├── autotrade/                Automated entry + maintenance loops
│   │   │   ├── auto_gate.py          5-stage gate stack
│   │   │   ├── entry_loop.py         Opens positions on accepted intents
│   │   │   ├── maint_loop.py         Forward rolls + signal-driven exits
│   │   │   ├── alerts.py             Operator notifications
│   │   │   └── universe.py           Theme-scoped ticker enforcement
│   │   ├── db/                       SQLAlchemy 2.x async models
│   │   └── repos/                    Theme / run / event repositories
│   └── alembic/                      Migrations
│
├── web/                              Next.js 15 app router
│   ├── app/
│   │   ├── themes/                   Theme manager UI
│   │   ├── runs/[id]/                Live agent reasoning + scorecard
│   │   └── performance/              Equity, P&L, positions
│   └── components/
│       ├── workflow-diagram.tsx      Live agent execution graph
│       ├── KillSwitch.tsx            Emergency halt
│       ├── PmccBuilder.tsx           LEAP trade builder
│       └── ...
│
├── docs/                             Architecture + screenshots
├── scripts/                          One-off admin utilities
└── tests/                            Provider smoke tests
```

The agent graph and provider SDK are organised under an `agents/` package
that the API imports. Internally it's structured around three responsibilities:

- **`dataflows/providers/`** — one module per vendor. Each implements a
  Protocol so the agents bind to capabilities (`OptionsFlowProvider`,
  `FundamentalsProvider`, etc.) rather than to vendor names.
- **`agents/`** — the analyst, researcher, trader, and risk nodes. Each
  is a LangGraph node with a clear contract: read state, write state, no
  side effects beyond logging.
- **`scorecard/`** — the theme-level overlay. Fan-out across tickers,
  per-ticker scoring, fan-in ranking.

---

## 3. Model assignment

DeepSeek V4 powers reasoning. Two tiers, assigned per node:

| Node                         | Model                | Rationale                          |
| ---------------------------- | -------------------- | ---------------------------------- |
| Sentiment / social analyst   | `deepseek-v4-flash`  | Volume work, latency-sensitive     |
| Fundamentals analyst         | `deepseek-v4-flash`  | Structured extraction              |
| News / macro analyst         | `deepseek-v4-flash`  | Bulk text, summary-heavy           |
| Options-flow analyst         | `deepseek-v4-flash`  | Numeric reasoning, well-grounded   |
| Bull / bear researcher       | `deepseek-v4-pro`    | Adversarial reasoning              |
| Research Manager             | `deepseek-v4-pro`    | Synthesis + conviction scoring     |
| Trader                       | `deepseek-v4-pro`    | Position sizing, levels            |
| Risk debaters (3)            | `deepseek-v4-pro`    | High-stakes argument quality       |
| Portfolio Manager            | `deepseek-v4-pro`    | Final go / no-go                   |
| Scorecard scorer             | `deepseek-v4-flash`  | Structured scoring on grounded ctx |
| Theme ranker                 | `deepseek-v4-pro`    | Cross-ticker prioritisation        |

The `thinking: {type: disabled}` parameter is set on V4 calls because the
agents bind tools and the deep-think stream is incompatible with strict
`tool_choice`.

---

## 4. Data providers

| Provider          | Owns these capabilities                                                                    |
| ----------------- | ------------------------------------------------------------------------------------------ |
| **Polygon**       | OHLCV, options chain, options snapshot, sector ETF history                                 |
| **Unusual Whales**| Options flow alerts, gamma exposure, max pain, dark pool prints                            |
| **FMP Ultimate**  | Fundamentals, income statement, balance sheet, cashflow, owner earnings, earnings calendar |
| **AlphaVantage**  | Macro signals, global news, treasury yields, CPI                                           |
| **IBKR**          | Paper-trade execution, positions, account summary, option chains, historical bars          |

Each provider implements the appropriate `Protocol` from `providers/base.py`
and is registered into the dispatch layer. Adding a new provider is a
file + a registration line + an env var entry — no changes to agents.

`yfinance` is kept as a fallback so the stack runs without paid keys.

### Caching, retries, secrets

- **Caching.** A decorator with sensible defaults: 60s for quotes, 5 min
  for snapshots, 24 h for fundamentals and macro. Backed by Redis when
  available, with a filesystem fallback so local development still works.
- **Retries.** `httpx.AsyncClient` wrapped with `tenacity`: exponential
  backoff, jitter, retry only on 5xx / 429 / connect errors. Up to four
  attempts.
- **Rate limits.** Per-provider semaphores. Vendor-specific limits
  (Unusual Whales 600 rpm, Polygon's per-tier ceilings) are codified in
  the provider classes.
- **Secrets.** Environment-only. Keys are never logged. Provider classes
  fail-fast on construction if a required key is missing — surfaces
  configuration errors at startup, not mid-graph.

---

## 5. Execution layer

When a run produces a *Buy*, the autotrade loop carries it through five
gates before any order is placed:

1. **Kill switch.** Both an environment flag and a database-backed
   system-state row. Either one being off blocks every trade.
2. **Universe.** The symbol must be in the current theme's universe.
   Stops the system from acting on tickers it's no longer researching.
3. **Strategy budget.** Per-day caps on entries, rolls, closes, and a
   minimum interval between actions to prevent thundering herds.
4. **Sector regime.** A multi-timeframe read on the theme's reference
   ETFs. In a confirmed pullback the gate moves to defensive mode.
5. **Circuit breaker.** Auto-flips the kill switch if the last *N*
   consecutive *system* actions errored. Eligibility failures, pre-trade
   data-quality rejections, and abandoned walks are excluded — those
   aren't bugs.

### Walking-limit executor (single-leg LEAP)

Each LEAP entry is a single long call submitted via a walking-limit order
that starts near mid and walks toward a cap derived from the spread
(default: mid + a configurable % of half-spread), abandoning cleanly if the
book moves away rather than crossing the full spread. There are no combos
and no naked-leg risk — every position is one long call. Sells (exits,
forward rolls) run the same algorithm in reverse, starting near mid and
walking down toward the bid.

### Maintenance loop

Polls during regular trading hours and handles the LEAP lifecycle. It is
deliberately **long-biased: it never force-closes on an ordinary down day**
— exits are signal-driven only.

- **Forward roll** of the LEAP when remaining tenor falls under ~six
  months, to stay ahead of the time-decay / gamma cliff.
- **Thesis-break exit** when the agent rating flips to *Avoid*, or on a
  high-severity 8-K.
- **Rotation exit pressure.** When the rotation detector confirms
  institutions leaving the theme, exit sensitivity tightens (and new
  entries into that theme halt).
- **Momentum-exhaustion exit** from a blended signal — trend vs moving
  averages, RSI, volume, opening range, ATR, auction imbalance, insider
  and analyst pressure (all underlying-based; no short-dated option data).
- **Position/intent reconciliation** — adopts broker positions with no
  intent, and recovers "abandoned-but-filled" orders so nothing sits
  unmanaged.

Every action records to `auto_actions` with gate state, payload, IBKR
order ID (when applicable), and outcome. The runs page in the UI surfaces
this audit trail.

---

## 6. Persistence

```text
themes(id, name, thesis, weights jsonb, created_at, updated_at)
theme_tickers(theme_id, ticker, weight, notes)
runs(id, theme_id, started_at, finished_at, status, config jsonb)
ticker_scores(run_id, ticker, mtf, options, thesis_fit, composite,
              drivers jsonb, risks jsonb, agent_reports jsonb)
agent_decisions(run_id, ticker, agent, role, content, model, latency_ms,
                tokens_in, tokens_out, created_at)
options_snapshots(ticker, captured_at, source, gamma_walls jsonb,
                  max_pain numeric, gex numeric, raw jsonb)
trade_intents(id, run_id, ticker, side, qty, limit_px, stop_px,
              status, ibkr_order_id, created_at)
auto_actions(id, timestamp, loop, action_type, symbol, intent_id,
             gate_status, gate_failures jsonb, payload jsonb,
             outcome, ibkr_order_id, error)
equity_snapshots(account_id, date, equity, cash, notional)
positions(account_id, symbol, qty, avg_price, last_price, pnl, captured_at)
system_state(autotrade_enabled, last_kill_at, kill_reason, updated_by)
```

SQLite for development; Postgres-ready for production. `pgvector` on
`agent_decisions.content` powers cross-ticker analogy lookup over prior
runs.

---

## 7. Boundaries that stay bright

A few constraints the project does not cross:

- **No live brokerage.** `IBKR_MODE` is enforced at startup. Live trading
  is a manual code change, never an agent action or a config toggle.
- **No vendor names in the UI.** Agents and reports are described in
  plain English. Provider attribution lives in the architecture doc and
  the `.env`, not in the user-facing surface.
- **No silent state changes.** Every gate decision and every order is
  recorded with enough metadata to reconstruct what the system saw and
  decided. The audit trail is the deliverable.
