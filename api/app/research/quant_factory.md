# Quant Research Factory — design

> Status: **decision-support / research layer.** Like the Hedge Fund Tracker and
> Rotation Detector, nothing here is wired to a live entry/exit gate. Promotion
> of any signal to a gate stays human-gated through the validation harness
> (`feature_research.py`), exactly as exit-pressure weights are promoted through
> `exit_pressure_replay.py`.

## The idea

The system already has the *qualitative* analyst (FastThemeRunner: LLM scores a
theme's symbols from FMP fundamentals + UW flow + insider). This layer adds the
*quantitative* researcher alongside it: a deterministic, point-in-time feature
pipeline that turns every theme symbol into a row of numbers, labels those rows
with forward returns, and measures — honestly, out of sample — which signals
actually predict returns and how fast their edge decays.

That last clause is the whole point. The operator's recurring worry is *timely
decisions*. Most of the signals already collected (13F especially) have decayed
by the time they're visible. The factory's job is to **measure each signal's
alpha half-life** so we know whether we're early or late, per signal.

## The key structural insight: one graph, not 17 themes

The live universe is **17 themes / ~72 unique symbols**, and they overlap
heavily (ETN sits in 4 themes; VRT/PWR/MRVL in 3; many names in 2). The themes
are not independent watchlists — they are facets of one interconnected
AI-infrastructure supply chain. Three consequences:

1. **ML is viable.** Per-theme there are ~4 names (overfit guaranteed). Pooled
   across the whole universe over time there are thousands of `(symbol, date)`
   observations. The unit of analysis is the **universe**, with theme
   membership as a feature — never a per-theme model.
2. **The supply-chain graph is half-free.** Two symbols sharing a theme are
   adjacent nodes. The membership matrix *is* the skeleton of the graph the
   NER/impact-mapping layer (future) will enrich.
3. **Overlap is alpha.** Three signals invisible while everything is siloed
   per theme:
   - **Chokepoint centrality** = theme-count. A name at 4 bottlenecks captures
     more of the buildout spend than a name at 1. Structural conviction.
   - **Cross-theme confirmation.** Smart-money / flow / news confirming a name
     that lives in *multiple* themes is higher-conviction than the same signal
     on a single-theme name.
   - **Cross-theme rotation.** The rotation detector is per-theme; relative
     theme strength across all 17 is the meta-signal.

## Architecture (layers)

```
THEME (hypothesis)         have
  → RAW DATA               have ~60% (price/vol, UW flow+gamma, FMP, EDGAR, news)
  → FEATURE STORE          THIS (symbol_feature_snapshot, point-in-time)
  → MODELS                 IC harness now; ML ranker / event-study / MC later
  → VALIDATION             feature_research.py (IC + alpha decay), extends replay
  → DECISION               existing plumbing (scorecard, exit/entry pressure)
```

### Layer 2 — feature store (`symbol_feature_snapshot`)

One row per `(symbol, as_of)`. Every value is *as-of* that date — no lookahead,
so a backtest can ask "what did I know on day T". Forward-return **labels** are
filled in later by `labeler.py` once the future is known.

```
symbol, as_of, features{...}, labels{fwd_ret_5d, _20d, _60d}, label_status
```

Feature families (this cut):
- **Graph** (deterministic, from membership — zero new data, fully offline):
  `theme_count`, `theme_centrality` (0..1), `co_membership_degree`,
  `avg_theme_size`, `themes` (list).
- **Cross-theme confirmation** (DB joins): `smartmoney_theme_confirm` —
  in how many of its themes is the name smart-money-confirmed.
- **Market** (best-effort, price provider): `momentum_20d`, `momentum_60d`,
  `dist_50dma`, `rvol` (z-scored volume).
- **Flow** (best-effort, UW): `dark_pool_accum` (signed off-exchange notional
  vs lit), `flow_imbalance` (call vs put premium tilt).
- **Cross-sectional Z** — every raw numeric feature is additionally
  standardized *across the universe on the snapshot day* (`z_*`). This gives an
  immediate, history-free "how does this name rank vs its peers today" without
  waiting for a time-series to accumulate. Time-series Z (vs own history) comes
  free later as snapshots pile up.

Every family degrades gracefully: a provider miss writes `null` for that
feature, never fails the snapshot. Graph + cross-theme features never depend on
the network, so a snapshot always carries signal.

### Layer 4 — validation (`feature_research.py`)

Offline, read-only — the exact discipline of `exit_pressure_replay.py`:
- For each feature, **rank IC** = Spearman(feature, fwd_ret) at horizons
  {5, 20, 60} days.
- **Alpha-decay profile** = how IC changes across horizons (the half-life).
- Low-power / degenerate-outcome warnings so early numbers read as a smoke
  test of the machinery, not a mandate. Grows sharper as snapshots accumulate.

It recommends; it never writes config and never trades.

## Build status — ALL layers shipped (no deferrals)

1. ✅ Feature store schema + snapshot orchestrator (graph + cross-theme always
   on; market + flow best-effort) — `features.py`, Alembic 0014.
2. ✅ Forward-return labeler — `labeler.py`.
3. ✅ IC + alpha-decay harness — `feature_research.py`.
4. ✅ Scheduler jobs (nightly snapshot + label backfill), read API, admin
   triggers, `start-all.ps1` verbs.
5. ✅ Pooled cross-sectional ML ranker — `ml_ranker.py` (pure-numpy ridge +
   sklearn GBM option; cold-start heuristic until labels accrue).
6. ✅ Event-study engine — `event_study.py` (market-adjusted CAR around
   13D / news / 13F-change events already in the DB).
7. ✅ Monte-Carlo sizing & exit-path stress — `montecarlo.py` (seeded GBM,
   quarter-Kelly ∧ vol-target sizing capped at 10% NAV, drawdown stress).
8. ✅ NER + impact-mapping supply-chain graph — `ner.py` + `impact_graph.py`
   (deterministic entity core + LLM relation enrichment; heat-diffusion of a
   news shock across the membership graph). Manager-archetype personas —
   `personas.py` (Aschenbrenner + 3 more, folded into the snapshot as a
   `persona_*` feature family).

Every layer is decision-support: ML predictions, CARs, MC sizes, impact scores,
and persona scores are all surfaced and never read by an entry/exit gate.
Promotion of any signal to a gate remains human-gated through the harnesses.

Dashboard: a `/research` tab surfaces the ranking, centrality, event-study, and
impact views; `/research/[symbol]` drills into features, personas, and the
Monte-Carlo distribution.

## Consistency with the existing system

- **Storage**: SQLAlchemy 2.x async model + Alembic migration, JSON feature
  blobs (matches `ThemeRotation`, `ClosingAccumulationSignal`).
- **Jobs**: APScheduler, always-on, independent of the auto-fire flag, gated by
  a settings flag (matches rotation / news / EDGAR sweeps).
- **Validation**: same offline read-only harness pattern + honesty warnings as
  the exit-pressure replay.
- **Providers**: reuses the `tradingagents` provider registry (FMP, UW,
  fallback price chain); never imports a vendor name into a frontend-visible
  surface.
- **Safety**: research-only. No gate, no order, no position mutation.
