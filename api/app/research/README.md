# Research harness (`app.research`)

Offline, read-only tools that replay the system's own decision audit log
against realised outcomes to validate and tune the hand-set parameters that
drive live trading. Lineage: Karpathy's `autoresearch` loop — mutate one
small parameter set, score against a *fixed, deterministic* metric, keep only
what beats the incumbent out-of-sample. Here the "training run" is a backtest
over `auto_actions` + `positions`; nothing touches the broker or live config.

## Exit-pressure replay

Validates the five exit-pressure weights and band thresholds (currently
hand-set in `tradingagents.strategies.maintenance.exit_pressure`) by asking:
does a high score actually predict a bad forward outcome in *this* book?

```bash
cd api
PYTHONPATH="$PWD;C:/Projects/TradingAgents" \
  python -m app.research.exit_pressure_replay --horizon 21 --samples 4000
# add --json for a machine-readable report (nightly cron)
# add --no-dedupe to keep every intraday sample (pseudo-replication; not advised)
```

Reads the DB path from `app.config.get_settings().DATABASE_URL` (override with
`--db`). The `tradingagents` package on `PYTHONPATH` keeps the incumbent
weights in sync with production; without it the harness falls back to the
documented constants.

### What it reports

- **Sub-score variance** — flags *dead components* (zero variance ⇒ their
  weight cannot affect ranking). As of the first run, `options_risk` is dead
  (LEAPS/stock book, no short-call delta) → **15% of the weight is inert**.
- **rank_ic** — sign-flipped Spearman(score, forward max-drawdown); positive
  means high pressure predicts deep drawdown (good).
- **band_edge** — mean forward return of HOLD positions minus EXIT positions.
  `n/a` until the score actually reaches a trim/exit band.
- **best vs incumbent**, walk-forward (train → validation), with a **human
  promotion gate**: the tool recommends; a person changes live weights only
  when the validation lift is positive *and* credible on a real sample.

### Known data caveat (read before trusting numbers)

Pressure logging currently spans a ~2-day window collapsing to ~24
independent symbols, and `positions` snapshots have a multi-week gap — so use
`--horizon 21`+ to reach past the gap, and treat output as a **smoke test of
the machinery**, not a re-weight mandate. The framework sharpens automatically
as the live loop accumulates more pressure logs; re-run nightly.

## Sector-dispersion IC study

The go/no-go test for an intraday market-neutral sector strategy (long the
strongest sectors, short the weakest, market beta hedged out). It asks the one
question that makes everything downstream worth building or not:

> Does a sector's **residual overnight move** — its gap beyond what its beta
> predicts — rank-predict its **residual return from 09:33** to later the same
> session?

```bash
cd api
python -m app.research.fetch_sector_bars --years 2 --end 2026-08-11  # ~30 min, once
python -m app.research.sector_dispersion_ic                          # instant, offline
# --json for machine-readable, --dump panel.csv for the per-day panel
```

Universe is the 11 GICS sector SPDRs + SMH, with SPY as the market leg (hedge
instrument and beta denominator — never ranked, since its residual against
itself is zero by construction).

### What it reports

- **Rank IC** — Spearman(signal, forward residual return) computed
  cross-sectionally *per day*, then averaged with a t-stat on the daily series
  (Grinold/Kahn). The stability of the daily ICs is the evidence; a single
  pooled correlation is not.
- **Tradeable spread** — top-3 minus bottom-3, gross and net of an explicit
  round-trip cost (4 legs x 1.0bp). IC can be real and still unprofitable; the
  net Sharpe column is the one that decides.
- **Beta drift** — the net beta a naive 50/50 dollar-neutral top-3/bottom-3
  book carries. Momentum ranking puts high-beta sectors on the long side and
  defensives on the short side, so a dollar-neutral book is *persistently* net
  long beta. This quantifies how much of any raw edge is market direction.
- **Alpha decay** — IC at 09:33→10:30 vs 09:33→15:50: does the edge live in
  the first hour or persist to the close?
- **Data quality** — per-ticker premarket print coverage at 09:20 and staleness.
  A sector that cannot be ranked premarket cannot be traded on this signal.

### Design notes worth knowing before reading the numbers

- **Separate overnight and intraday betas.** A sector's sensitivity to the
  market across the gap is not its sensitivity during the session; using one
  for the other leaks market direction into the "residual".
- **Ex-dividend sessions are dropped.** Polygon adjusts for splits but not
  dividends, so an ex-div morning shows a spurious gap down of the distribution
  size — a fake signal roughly 4x/year/ticker.
- **Everything is strictly causal.** Betas and the vol standardiser are shifted
  so day *t* never sees its own value.
- **Free-tier pacing.** Polygon allows 5 calls/min; `polygon_bars.py` paces,
  backs off on 429, follows `next_url` pagination, and caches every chunk to
  `.cache/polygon/` so re-runs are free and an interrupted pull resumes.
- **TLS interception.** Norton MITMs outbound TLS on this machine; the fetcher
  calls `truststore.inject_into_ssl()` for the same reason `app.main` does.

### What it found (first run, 2024-08-11 .. 2026-08-11, 500 sessions)

**No usable edge. The premarket sector ranking does not predict the session.**

| Specification | mean IC | t | spread | net SR |
|---|---:|---:|---:|---:|
| Residual signal → 09:33-10:30, 12 sectors | +0.037 | +1.80 | +0.61bp | -0.99 |
| Residual signal → 09:33-15:50, 12 sectors | +0.011 | +0.54 | -0.44bp | -0.95 |
| Raw gap → raw return (the naive design) | -0.004 | -0.18 | -3.05bp | — |
| Residual → 10:30, 7 high-coverage sectors | +0.048 | +2.04 | +2.74bp | — |

Supporting detail:

- **The one cell that clears |t|>2 does not survive scrutiny.** It is a
  post-hoc subsample, one of ~6 cuts tried (multiple-testing threshold is
  nearer t=2.6), its *tradeable* counterpart is insignificant (spread t=+0.97),
  and it decays year over year: IC 0.101 (2024) → 0.041 (2025) → 0.020 (2026).
- **Net Sharpe is negative in every specification** — costs alone (4bp round
  trip) exceed the entire gross spread.
- **No reversal either.** The overnight-reversal literature predicts negative
  IC; measured IC is mildly positive. Sector ETFs appear efficiently arbitraged
  across the gap in both directions — that effect is a single-stock phenomenon.
- **The dispersion gate made things worse**, not better (hi-dispersion IC
  -0.016 vs +0.037 unconditional). Gating on dispersion is not a fix.
- **A 100bp spread target is not reachable.** The top-3/bottom-3 daily spread
  has sd=75bp; |spread|>100bp on only 16% of sessions (median 50bp, p90 120bp).
- **Premarket data is a hard blocker regardless of edge.** At 09:20, coverage
  is XLC 62%, XLB 65%, XLRE 66%, XLY 78%, XLI 81% — with median staleness
  11-14 minutes. On a third of sessions those sectors cannot be ranked at all.
- **Beta drift was NOT observed for a gap-based ranking** (net beta +0.01 to
  +0.07, both residual and raw). A one-morning gap is idiosyncratic-news
  driven and carries no systematic beta tilt. The beta-drift concern applies to
  multi-day *trend/momentum* composites, which do load on beta — not to this
  signal. Re-test before assuming it for any slower variant.

Machinery was validated on the same data before the null was accepted: rolling
betas come out economically correct (XLP 0.45, XLU 0.53 ... XLK 1.33, SMH 1.59)
and residualising drops XLK's correlation with SPY from 0.92 to 0.15.

**Verdict: do not build the execution layer, the breadth pipeline, or the
factor overlay for this signal.** The cheap test did its job.

### Trusting the harness

`tests/test_sector_dispersion.py` runs recovery-and-null on synthetic panels:
a planted continuation must come back with a positive IC at the right strength,
pure noise must come back flat, and a planted *reversal* must read negative.
That last one is the sign-error trap — the overnight-reversal literature
predicts exactly that shape, so reading it as +IC would invert the thesis.

### Next steps (not yet built)

- Schedule a nightly `--json` run and persist reports to track the validation
  lift trend over time.
- Extend to entry decisions (replay `open_pmcc` gating vs forward outcomes).
- Once the sample is large enough to show a credible, stable lift, wire a
  DB-backed `strategy_params` table so promotion is a reviewed config change,
  not a code edit.
