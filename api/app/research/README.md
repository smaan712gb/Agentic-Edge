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

### Next steps (not yet built)

- Schedule a nightly `--json` run and persist reports to track the validation
  lift trend over time.
- Extend to entry decisions (replay `open_pmcc` gating vs forward outcomes).
- Once the sample is large enough to show a credible, stable lift, wire a
  DB-backed `strategy_params` table so promotion is a reviewed config change,
  not a code edit.
