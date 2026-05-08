<div align="center">

# Agentic Edge

**An open-source agentic research platform for thematic, fundamental, and options-aware investing.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776ab.svg)](https://www.python.org/)
[![Node 20+](https://img.shields.io/badge/Node-20+-339933.svg)](https://nodejs.org/)
[![Next.js 15](https://img.shields.io/badge/Next.js-15-000000.svg)](https://nextjs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688.svg)](https://fastapi.tiangolo.com/)
![Status](https://img.shields.io/badge/status-active-brightgreen.svg)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-ff69b4.svg)](CONTRIBUTING.md)

*Give it a theme. Get back a ranked scorecard with the reasoning attached.*

</div>

---

You feed it a theme — *AI infrastructure*, *advanced memory*, *grid power*, *small modular reactors*, *optical networking*, anything else you're researching. A team of agents fans out, reads catalysts, pulls fundamentals, watches options flow, debates the bull and bear case, and ranks the names that fit best. The scorecard is the output. The reasoning is on the page, not behind a black box.

Connect a paper Interactive Brokers account and the system can attempt to act on its own conclusions — with NAV-aware sizing, a five-stage gate stack, and a kill switch one click away.

> **This is research and decision-support software, not financial advice.** Live brokerage execution is disabled by default and the project actively refuses to run against a non-paper account. Markets do things no model has seen. Use at your own risk.

---

## Why this exists

Discretionary research teams spend most of their week on the same loop: build the universe, stitch provider feeds together, pre-screen for thesis fit, summarise flow, do it again tomorrow on a different theme. Agentic Edge moves that loop into a graph of cooperating agents so human time goes where it actually matters — judgement on the names the system surfaces, and the decisions to size, hold, or exit.

The system is meant to be *legible*. Open the runs page, click any agent, and read what it concluded for each ticker and why. No opaque scoring. The reasoning is the deliverable.

### What's different

- **Theme-first, not ticker-first.** You bring an investment thesis; the system finds and ranks the names that fit, not the other way around.
- **Bull and bear actually argue.** Adversarial reasoning between two researcher agents, with a research-manager synthesis and a conviction gate that pulls you out of low-confidence names automatically.
- **Options-aware.** Unusual flow, gamma exposure, and max-pain are first-class inputs to the bear case. Most agentic stacks ignore the options tape; this one doesn't.
- **Two strategies in one platform.** Long equity for cleaner setups, covered-LEAPS (poor man's covered call) for capital efficiency on high-conviction names. Walking-limit combo execution and an autonomous maintenance loop handle rolls, defensive closes, and earnings hedges.
- **Paper-only by design.** Live brokerage is a manual code change, not a config toggle. The line stays bright.

---

## What it looks like

The interface is a Next.js 15 app with a live agent-network visualisation on the home page, theme management, run history, performance tracking, and a kill switch always one click away.

- **Home — the digital trading floor.** A live, animated agent-network diagram. Each node is a specialist agent; edges animate when work is flowing through them; the active theme and chokepoint summary sit at the top.
- **Themes.** Add a theme — *AI infrastructure*, *advanced memory*, *grid power*, *small modular reactors*, *optical networking*, or your own — seed it with tickers and a thesis sentence. The universe is editable at any time.
- **Runs.** Every research run is recorded with the agent timeline, intermediate findings, and the final scorecard. Click any agent in the workflow diagram to read what it concluded for each ticker and why.
- **Scorecard.** Ranked output combining the bull/bear debate, options flow, regime read, and the trader's recommendation per name. Composite score and the supporting rationale travel together.
- **Performance.** Today's gain/loss, account equity curve, and current positions when an IBKR paper account is connected.
- **Kill switch.** One click in the sidebar halts every automation loop. The state is stored in the database so a restart can't accidentally re-arm trading.

### Home — the digital trading floor

![Agentic Edge — the digital trading floor](docs/diagrams/digital-trading-floor.svg)

> **Try the click-through version** — every digital employee is inspectable, and the "Run a theme" button walks the full sequence: **[live demo](https://smaan712gb.github.io/Agentic-Edge/workflow-diagram.html)**.

### Performance — paper account, live

![Performance dashboard with KPI cards, 90-day equity curve, and open positions](docs/diagrams/performance.svg)

KPI cards at the top, the 90-day equity curve drawing itself in below, and the open-positions table beneath. Once IBKR Gateway is connected, every value is the live read from the paper account.

### Themes — your investment universe

![Themes page showing six thematic baskets with conviction scores](docs/diagrams/themes.svg)

Each card is a thesis the agent team scores against. Add a theme, seed it with tickers and a one-line rationale, and the next scheduled run picks it up automatically.

### How the agents score

![Scorecard with six scoring dimensions per ticker and the ranked theme leaderboard](docs/diagrams/scorecard.svg)

The system ranks names in three ordered passes — quality first, cycle weighting next, entry timing last.

**Pass 1 — Fundamental quality** (the floor; permanent, not theme-driven)

Every candidate name is scored across six fundamental dimensions:

- **Profit Generation** — earnings power, gross margin trajectory.
- **Revenue Growth** — top-line momentum and the durability of it.
- **Capital Efficiency** — ROIC, working-capital discipline, capex justified by returns.
- **Free Cash Flow Production** — cash conversion across the cycle.
- **Balance Sheet Health** — net cash, debt coverage, liquidity buffer.
- **Future Outlook** — forward demand, pricing power, structural inevitability.

The Future Outlook dimension carries extra weight by design. A name with negative current income but a confirmed chokepoint position and strong revenue growth can still earn a Buy — structural inevitability beats a clean income statement when the supply-chain position can't be substituted.

**Pass 2 — Hot-cycle weighting** (which quality names lead *today*)

A name's fundamental score gets weighted by which chokepoint themes are currently in contact. NVIDIA may score higher than Micron in absolute fundamental terms, but when memory is the binding leg of the AI cycle — HBM3e capacity is the constraint, hyperscalers are short of it, the price curve is up — Micron bubbles to the top of the leaderboard. Quality earns the right to be on the board; the hot cycle decides who's swinging the bat right now.

**Pass 3 — Entry timing** (when, not whether)

For names that clear pass 1 and pass 2, the agents look for ideal entry: unusual options flow (whale call sweeps, gamma walls, max-pain trail), GEX positioning, multi-timeframe trend, relative-strength leadership, and volume confirmation. Timing signals never override quality — they decide *when* to pull the trigger on a name the framework already wants.

The composite is on the page, the leaderboard is the ranked output, and the reasoning behind every score travels with the rank.

The illustrations above are generated from the same components that ship with the app — see `web/components/` for the React versions. Real screenshots will land in `docs/screenshots/` (see the [screenshot guide](docs/screenshots/README.md) for the filenames the README picks up automatically).

---

## How it works

1. **You add a theme.** From the frontend you create a theme — say, *AI bottlenecks: power and cooling* — and seed it with the tickers you want under research. You can edit, add, or remove names at any time. Themes also carry a one-line rationale you can revisit later.

2. **The agent team takes over.** Each run spins up a graph of specialist agents:
   - **Market analyst** reads the price action and trend regime against the theme's reference ETFs.
   - **Fundamentals analyst** pulls the latest filings, growth, and balance-sheet health.
   - **Options flow analyst** watches unusual options activity and gamma exposure.
   - **News + macro analyst** stitches together earnings, catalysts, and macro signals.
   - **Bull and bear researchers** argue the case in structured debate.
   - **Research manager** weighs the debate and writes a thesis.
   - **Trader** translates the thesis into an actionable plan with entry, sizing, and risk.
   - **Risk panel** stress-tests the plan from three perspectives (aggressive, conservative, neutral).
   - **Portfolio manager** signs off or vetoes.
   - **Theme ranker** ranks every name in the theme by composite score so you see the best-positioned candidates at the top.

3. **You read the scorecard.** Every run produces a ranked scorecard with the chain of reasoning. You can stop here and use it as decision support, or keep going.

4. **Optional execution.** If you wire a paper IBKR account, the system can attempt to enter and manage positions inside guardrails you configure. There are five gates between an idea and a fill, plus a hardware-style kill switch. Sizing is NAV-aware. Nothing trades on a live brokerage account by default — paper mode is enforced at the configuration layer.

---

## Strategies supported

Agentic Edge is built around two complementary playbooks for high-conviction theme names:

**Long equity.** Straightforward stock entries with NAV-aware sizing, ATR-based stops, and exit on either a thesis break (the agent team flips the rating to *Avoid*) or a volatility-adjusted drawdown.

**Covered LEAPS (poor man's covered call).** A capital-efficient alternative to owning the underlying:

- Long a deep-in-the-money LEAP call, typically 18–24 months out, around 0.85 delta.
- Short a near-dated call (21–35 days), typically around 0.25 delta, against it.
- Position is built and closed atomically through a walking-limit combo executor — no naked-leg risk between fills.
- Maintenance loop handles defensive rolls when the short call delta climbs, time-based rolls as the short approaches expiry, forward rolls of the LEAP when remaining tenor falls under six to nine months, and a close-only window two sessions before earnings.

The strategy honours the operator's decisions: there are tunable thresholds for delta bands, spread ceilings, roll cost guards, and momentum-aware short-call placement. Walking-limit execution caps the worst price you'll pay so wide quotes don't translate into wide fills. If a name's option market is too thin to honour the discipline, the system can fall back to a long equity attempt.

---

## Quick start

You'll need:

- **Node 20+** (frontend)
- **Python 3.11+** (API)
- **Interactive Brokers Gateway** in paper mode (optional, for live paper trading)
- API keys for the providers you want to use (see `.env.example`)

### 1. Configure

```bash
cp .env.example .env
# fill in the keys you have; missing keys disable the relevant capability
```

The system reads only what you give it. You can run with a partial set of providers and it will skip the agents that depend on the missing ones.

### 2. Run the API

```bash
pip install -r api/requirements.txt
python -m alembic -c api/alembic.ini upgrade head
python -m uvicorn api.app.main:app --port 8000 --reload
```

Health check: `http://127.0.0.1:8000/api/health`.

### 3. Run the web app

```bash
cd web
npm install
npm run dev
```

Open `http://localhost:3000`. The Next.js dev server proxies `/api/*` to the FastAPI app, so both run side-by-side without extra wiring.

### 4. (Optional) Connect IBKR

Start IB Gateway in paper mode, enable the API, and set `IBKR_PORT=4002` (paper). The system enforces paper mode at the config layer and refuses to start against a live account ID. If you want live, you'll have to take the safety off yourself — and that's a conversation, not a checkbox.

---

## Project layout

```text
.
├── api/               FastAPI service: routes, scheduler, autotrade loops, persistence
│   ├── app/
│   │   ├── autotrade/   Entry, maintenance, gates, alerts, heartbeat
│   │   ├── db/          SQLAlchemy 2.x async models
│   │   └── repos/       Theme / run / event repositories
│   └── alembic/       Migrations
├── web/               Next.js 15 + Tailwind v4 + React Flow frontend
│   ├── app/             Routes (themes, runs, performance)
│   └── components/      Workflow diagram, kill switch, PMCC builder, etc.
├── docs/              Architecture, design notes, screenshots
├── scripts/           One-off admin utilities
└── tests/             Provider smoke tests
```

The agent graph and provider SDK live under an `agents/` package the API imports. See `docs/ARCHITECTURE.md` for the full breakdown — analyst nodes, model assignment, the gate stack, the walking-limit executor, and the persistence schema.

---

## What's real, what's a stub

| Layer | Status |
| --- | --- |
| Frontend | Real |
| API contract | Real and stable |
| Theme + run persistence | Real (SQLAlchemy + Alembic; SQLite for dev, Postgres-ready) |
| Provider clients (Polygon, FMP, AlphaVantage, Unusual Whales, IBKR, DeepSeek) | Real |
| Agent scorecard graph | Working end-to-end with DeepSeek V4 |
| IBKR paper execution | Working (combo + single-leg, walking-limit, atomic) |
| Maintenance loop (rolls, closes, earnings hedge) | Working |
| Live-broker mode | Disabled by design — paper only |

Treat anything not in the list as planned but unverified.

---

## Configuration

The service is configured entirely through environment variables — see `.env.example` for the full list. A few that matter:

- `DEEPSEEK_API_KEY` — required to run real agent flows.
- `POLYGON_API_KEY`, `UNUSUAL_WHALES_API_KEY`, `FMP_API_KEY`, `ALPHA_VANTAGE_API_KEY` — provider keys; missing keys disable the corresponding analyst.
- `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, `IBKR_MODE=paper` — paper-trading wiring. The system rejects `IBKR_MODE=live`.
- `ADMIN_API_TOKEN` — required for admin endpoints (kill switch, scheduler, reconcile). The service refuses placeholder values like `changeme`.

---

## Roadmap

Things on the near horizon:

- Per-symbol options flow staleness checks pre-trade.
- Drawdown-aware kill-switch (auto-flip on intraday equity breach).
- Sequenced legging for the diagonal — defer the short-call sale until the LEAP shows P&L or the underlying clears a volatility threshold.
- Earnings-cycle short re-establishment after a hedged close.
- A library of saved themes you can fork.

If any of those interest you, the contributing guide explains how to pick one up.

---

## Disclaimer

Agentic Edge is research and decision-support software. It is **not** investment advice, a recommendation to buy or sell any security, or a guarantee of any outcome. The default execution mode is paper trading on Interactive Brokers and the system actively refuses to operate against a live account without explicit, deliberate configuration changes. Past performance is not indicative of future results. Markets can and do produce outcomes that no model has seen. Use this software at your own risk and consult a licensed financial advisor before making investment decisions.

---

## Contributing

Issues, bug reports, and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) to see how the project is organised and which contributions are most useful right now. Security reports go through [SECURITY.md](SECURITY.md), not public issues.

Topics that help others find this project on GitHub: `agentic-ai`, `langgraph`, `quantitative-finance`, `options-trading`, `interactive-brokers`, `nextjs`, `fastapi`, `deepseek`, `paper-trading`, `investment-research`.

---

## Star history

If this project saved you a few mornings of research, consider starring the repo. It's the cheapest way to help others find it.

[![Star History Chart](https://api.star-history.com/svg?repos=smaan712gb/Agentic-Edge&type=Date)](https://star-history.com/#smaan712gb/Agentic-Edge&Date)

---

## License

[MIT](LICENSE) — with an additional financial-software disclaimer. The short version: do whatever you want with the code, but don't blame us for what the market does.
