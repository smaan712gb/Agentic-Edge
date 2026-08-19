// Thin client over the FastAPI mock. Keeps shapes the frontend depends on
// in one file so future swaps (real backend, different transport) are local.

export type Theme = {
  id: string;
  name: string;
  thesis: string;
  chokepoint: string;
  symbols: string[];
  created_at: string;
};

export type AgentDef = {
  id: string;
  name: string;
  role: string;
  lane: string;
  summary: string;
  explanation: string;
};

export type AgentEdge = { from: string; to: string };

export type AgentEvent = {
  agent_id: string;
  symbol: string | null;
  status: "started" | "finished";
  summary: string | null;
  timestamp: string;
};

export type SymbolScore = {
  symbol: string;
  setup: number;
  options: number;
  thesis_fit: number;
  composite: number;
  decision: "Buy" | "Hold" | "Avoid";
  drivers: string[];
  risks: string[];
};

export type Run = {
  id: string;
  theme_id: string;
  started_at: string;
  finished_at: string | null;
  // Lifecycle: runs are inserted in "running" state directly (no queue),
  // transition to "done" on success or "error" on failure.
  status: "running" | "done" | "error";
  progress: number;
  events: AgentEvent[];
  scores: SymbolScore[];
  summary: string | null;
  best_positioned: string[];
};

// What GET /api/runs actually returns. The list endpoint deliberately omits
// `events` and `scores`: scores alone were 91% of that payload and the list
// renders none of them. Open a run to get the full `Run` from /api/runs/{id}.
export type RunListItem = Omit<Run, "events" | "scores">;

export type EquityPoint = { date: string; equity: number };
export type Position = {
  symbol: string;
  qty: number;
  avg_price: number;
  last_price: number;
  pnl: number;
  // Option-leg metadata (empty/null for stock positions). For PMCC entries
  // the same underlying produces two rows (LEAP long + short call short) —
  // conid is the only unique key across them.
  sec_type?: "STK" | "OPT" | "FUT" | string;
  conid?: number;
  local_symbol?: string;
  expiry?: string;       // YYYYMMDD when sec_type=OPT
  strike?: number | null;
  right?: "C" | "P" | "";
  multiplier?: string;
};
export type TodayPerf = {
  equity: number;
  gain: number;
  gain_pct: number;
  unrealized_pnl?: number;
  realized_pnl?: number;
  cash?: number;
  notional?: number;
  available_funds?: number;
  buying_power?: number;
  account_id?: string;
  as_of: string;
  error?: string;
};

export type SchedulerStatus = {
  running: boolean;
  enabled: boolean;
  cron: string | null;
  next_run_at: string | null;
  last_run_at: string | null;
  // "noop" = the job ran but every theme was skipped, so nothing was
  // produced. Distinct from "ok" on purpose: a recovery fired by hand
  // must not report success when it did no work.
  last_status: "ok" | "partial" | "error" | "noop" | null;
};

export type Regime = "uptrend" | "pullback" | "range" | "downtrend" | "unknown";

export type UwContext = {
  gamma_sign: "positive" | "negative" | "neutral" | null;
  gamma_flip_strike: number | null;
  flow_premium_24h_call: number;
  flow_premium_24h_put: number;
  flow_tilt: "bullish" | "bearish" | "neutral" | null;
  note: string;
};

export type OptionLegSummary = {
  expiry: string | null;
  strike: number | null;
  delta_target: number | null;
  delta_actual: number | null;
  iv: number | null;
  open_interest: number | null;
  qty: number | null;
  filled_at: string | null;
  fill_price: number | null;
};

export type TradeIntent = {
  id: string;
  run_id: string | null;
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  status: "pending_review" | "pending" | "submitting" | "filled" | "abandoned" | "rejected" | "cancelled" | "error";
  structure: "stock" | "pmcc" | "pmcc_sequenced";
  position_state: string;
  entry_strategy: string | null;
  leap: OptionLegSummary;
  short_call: OptionLegSummary;
  net_debit_target: number | null;
  net_debit_cap: number | null;
  net_debit_filled: number | null;
  max_loss: number | null;
  walking_config: Record<string, any> | null;
  rationale: string | null;
  ibkr_order_id: string | null;
  created_at: string;
  updated_at: string;
};

export type ThemeRegime = {
  theme_id: string;
  etfs: string[];
  regime: Regime;
  rationale: string;
  spot: Record<string, number>;
  vs_50ma_pct: Record<string, number>;
  vs_200ma_pct: Record<string, number>;
  momentum_20d_pct: Record<string, number>;
  realized_vol_30d_pct: Record<string, number>;
  dd_from_52wh_pct: Record<string, number>;
  uw: Record<string, UwContext>;
  price_source: Record<string, string>;
  captured_at: string;
};

// --- Hedge Fund Signal Tracker -------------------------------------------

export type ManagerChange = {
  ticker: string | null;
  cusip: string;
  issuer_name: string | null;
  prior_shares: number;
  current_shares: number;
  change_pct: number | null;
  change_type: "new" | "add" | "trim" | "exit" | "hold";
  current_period: string | null;
};

export type Manager = {
  slug: string;
  name: string;
  tier: "tier1" | "tier2" | "activist" | string;
  macro_only: boolean;
  active: boolean;
  primary_themes: string[];
  ciks: string[];
  last_filing_at: string | null;
  top_changes: ManagerChange[];
};

export type ManagerHolding = {
  issuer_name: string;
  ticker: string | null;
  cusip: string;
  value_usd: number;
  shares: number;
  put_call_flag: string;
  pct_of_portfolio: number | null;
  period_end: string | null;
};

export type OverlapRow = {
  cusip: string;
  issuer_name: string;
  ticker: string | null;
  managers: { slug: string; name: string; value_usd: number; shares: number; pct_of_portfolio: number | null }[];
  aggregate_value_usd: number;
  aggregate_shares: number;
  manager_count: number;
  confirmation: boolean;
};

export type UwFlow = {
  gamma_sign: "positive" | "negative" | "neutral" | null;
  gamma_flip_strike: number | null;
  flow_premium_24h_call: number;
  flow_premium_24h_put: number;
  flow_tilt: "bullish" | "bearish" | "neutral" | null;
  note: string;
};

export type RotationRow = {
  theme_id: string;
  flagged: boolean;
  score: number;
  signals_tripped: string[];
  evidence: Record<string, any>;
  computed_at: string | null;
};

export type Mention = {
  ticker: string;
  theme_id: string | null;
  headline: string;
  provider: string;
  chokepoint_hits: string[];
  sentiment: "bullish" | "bearish" | "neutral" | null;
  conviction: number | null;
  summary: string | null;
  published_at: string | null;
  captured_at: string | null;
};

export type SmartMoney = {
  matched: boolean;
  ticker: string | null;
  cusip: string | null;
  confirmation: boolean;
  manager_count: number;
  aggregate_value_usd: number;
  aggregate_shares: number;
  managers: { slug: string; name: string; value_usd: number; shares: number; put_call_flag: string; pct_of_portfolio: number | null; period_end: string | null }[];
};

// --- Morning Report --------------------------------------------------------

export type IdeaTechnical = {
  momentum_20d: number | null;
  momentum_60d: number | null;
  dist_50dma: number | null;
  rvol: number | null;
  flow_imbalance: number | null;
  gamma_sign: "positive" | "negative" | "neutral" | null;
  as_of: string | null;
};

export type IdeaTrend = {
  score: number;
  label: "perfect alignment" | "mostly aligned" | "mixed" | "mostly inverted" | "downtrend stack";
  pairs: { pair: string; ok: boolean }[];
  emas: Record<string, number>;
  price: number;
  price_above_8ema: boolean;
};

export type IdeaPattern = {
  key: string;
  name: string;
  direction: "bullish" | "bearish";
  confidence: "high" | "medium";
  detail: string;
};

export type IdeaStructure = {
  structure: "uptrend" | "downtrend" | "range";
  higher_highs: boolean;
  higher_lows: boolean;
  accumulation_days_25: number;
  distribution_days_25: number;
  distribution_flag: boolean;
  liquidity_sweep: { side: "lows" | "highs"; detail: string } | null;
  supply_zone: { low: number; high: number; dist_pct: number } | null;
  demand_zone: { low: number; high: number; dist_pct: number } | null;
};

export type IdeaEntry = {
  score: number;
  label: "prime setup" | "good setup" | "developing" | "not ready";
  reasons: { ok: boolean; text: string }[];
};

export type PortfolioRisk = {
  empty: boolean;
  n_holdings: number;
  health_pct: number | null;
  risk_label: string;
  avg_correlation: number | null;
  effective_bets: number | null;
  cash_pct: number | null;
  theme_exposure: { theme: string; pct: number }[];
};

export type IdeaAnalyst = {
  pt_consensus: number | null;
  pt_high: number | null;
  pt_low: number | null;
  price: number | null;
  upside_pct: number | null;
  grades_30d: { date: string | null; firm: string | null; from: string | null; to: string | null; direction: "up" | "down" | "neutral" }[];
  n_up_30d: number;
  n_down_30d: number;
};

export type IdeaResearch = {
  headline: string;
  sentiment: "bullish" | "bearish" | "neutral" | null;
  conviction: number | null;
  summary: string | null;
  published_at: string | null;
};

export type InstitutionalRead = {
  label: "constructive" | "cautious" | "mixed" | "limited data";
  bull: string[];
  bear: string[];
};

export type ReportIdea = {
  symbol: string;
  theme_id: string;
  theme: string;
  composite: number;
  setup: number;
  options: number;
  thesis_fit: number;
  conviction: number | null;
  drivers: string[];
  risks: string[];
  rationale: string | null;
  run_id: string;
  held: boolean;
  rotation_flagged: boolean;
  technical?: IdeaTechnical | null;
  trend?: IdeaTrend | null;
  patterns?: IdeaPattern[];
  structure?: IdeaStructure | null;
  entry?: IdeaEntry;
  analyst?: IdeaAnalyst | null;
  research?: IdeaResearch[];
  smart_money?: { manager_count: number; confirmation: boolean } | null;
  institutional_read?: InstitutionalRead;
};

export type ReportHolding = {
  symbol: string;
  structure: string;
  position_state: string;
  qty: number | null;
  intent_id: string | null;
  pnl?: number;
  last_price?: number;
  avg_price?: number;
  exit_pressure: {
    score: number | null;
    band: "hold" | "trim_light" | "trim_heavy" | "aggressive" | null;
    rationale: string | null;
    sub_scores: Record<string, number> | null;
    theme: { composite: number | null; streak: number } | null;
    as_of: string | null;
  } | null;
  latest_score: {
    composite: number;
    decision: string;
    conviction: number | null;
    theme_id: string;
    theme: string;
    as_of: string | null;
  } | null;
  themes: string[];
  rotation_flagged: boolean;
  rotation_themes: string[];
  smart_money: { manager_count: number; confirmation: boolean; aggregate_value_usd: number } | null;
  suggested_action: string;
};

export type ReportStakeFiling = {
  ticker: string | null;
  form_type: string;
  change_type: string;
  percent_of_class: number | null;
  prior_percent: number | null;
  is_activist: boolean;
  is_holding: boolean;
  filed_at: string | null;
};

export type FilingImpact = "positive" | "negative" | "neutral" | "unclear";

export type ReportActivity = {
  stake_filings: ReportStakeFiling[];
  fund_moves: { manager: string; ticker: string | null; issuer_name: string | null; change_type: string; change_pct: number | null }[];
  insider_alerts: {
    symbol: string | null;
    direction: "buy" | "sale";
    headline: string;
    owner_type: string | null;
    value_usd: number | null;
    filing_date: string | null;
    impact: FilingImpact;
    impact_note: string;
    timestamp: string;
  }[];
  filing_alerts: {
    symbol: string | null;
    form_type: string | null;
    severity: string | null;
    rationale: string | null;
    link: string | null;
    is_held: boolean;
    meaning: string;
    impact: FilingImpact;
    impact_note: string;
    timestamp: string;
  }[];
};

export type ReportAlerts = {
  entry_breaker: { tripped: boolean; reason: string | null; tripped_at: string | null };
  autotrade_enabled: boolean;
  rotation_flagged: { theme_id: string; flagged: boolean; score: number; signals_tripped: string[] }[];
  holding_stake_reductions: (ReportStakeFiling & { held: boolean })[];
};

export type ReportThemeHealth = {
  theme_id: string;
  theme: string;
  composite: number;
  n_scored: number;
  n_buys: number;
  rotation_flagged: boolean;
};

export type ReportPosture = {
  score: number;
  label: "defensive" | "cautious" | "neutral" | "constructive" | "risk-on";
  components: { theme_health: number; rotation_calm: number; buy_breadth: number };
  breaker_capped: boolean;
  themes_flagged: number;
  themes_total: number;
};

export type ReportBrief = {
  text: string;
  source: "agent" | "fallback";
  generated_at: string;
};

export type PulseTheme = {
  theme_id: string;
  theme: string;
  avg_change_pct: number;
  n: number;
  rotation_flagged: boolean;
};

export type IntradayPulse = {
  as_of_et: string;
  n_symbols_quoted: number;
  breadth_up_pct: number;
  avg_change_pct: number;
  theme_dispersion_pct: number;
  day_type: "accumulation" | "distribution" | "rotation" | "consolidation";
  day_note: string;
  benchmarks_pct: Record<string, number | null>;
  semis_rotation: "into_semis" | "out_of_semis" | "within_semis" | "neutral";
  semis_rotation_note: string;
  catalysts: { title: string; why_it_matters: string | null; bull_case: string | null; bear_case: string | null }[];
  status_0_10: number;
  leaders: PulseTheme[];
  laggards: PulseTheme[];
  themes_rotation_flagged: string[];
  narrative: string;
  narrative_source: "agent" | "fallback";
  themes: PulseTheme[];
  generated_at: string;
};

export type MorningReport = {
  as_of: string;
  generated_at: string;
  posture: ReportPosture;
  brief: ReportBrief | { error: string };
  account: { equity: number; cash: number | null; notional: number | null; cash_pct: number | null; as_of: string } | null;
  top_ideas: ReportIdea[] | { error: string };
  holdings: ReportHolding[] | { error: string };
  off_universe_positions: { symbol: string; qty: number; pnl: number }[];
  portfolio_risk: PortfolioRisk | { error: string };
  institutional_activity: ReportActivity | { error: string };
  alerts: ReportAlerts | { error: string };
  theme_health: ReportThemeHealth[] | { error: string };
};

// --- Quant Research Factory ----------------------------------------------

export type UniverseGraphNode = {
  symbol: string;
  theme_count: number;
  theme_centrality: number;
  co_membership_degree: number;
  avg_theme_size: number;
  themes: string[];
};

export type FeatureSnapshot = {
  symbol: string;
  as_of: string | null;
  features: Record<string, number | null | string[]>;
  labels: Record<string, number>;
  label_status: "pending" | "partial" | "final" | string;
};

export type RankRow = {
  symbol: string;
  predicted_return: number;
  rank: number;
  percentile: number;
};

export type RankReport = {
  model: "ridge" | "gbm" | "heuristic" | string;
  horizon_days: number;
  n_train_rows: number;
  n_ranked: number;
  as_of: string | null;
  features_used: string[];
  top_drivers: string[];
  ranking: RankRow[];
  warnings: string[];
};

export type EventStudyReport = {
  n_events: number;
  n_symbols: number;
  windows: number[];
  by_event_type: Record<string, Record<string, { n: number; mean_car: number; median_car: number; win_rate: number; stdev: number }>>;
  events: { event_type: string; count: number }[];
  warnings: string[];
};

export type MonteCarloReport = {
  symbol: string;
  horizon_days: number;
  n_paths: number;
  spot: number | null;
  mu_daily: number;
  sigma_daily: number;
  annualised_vol: number;
  terminal: Record<string, number>;
  max_drawdown: Record<string, number>;
  sizing: Record<string, number>;
  exit_stress: Record<string, number>;
  warnings: string[];
};

export type ImpactGraph = {
  nodes: { symbol: string; impact: number; seed: number; theme_count: number }[];
  edges: { source: string; target: string; weight: number }[];
  seeded_from: string;
};

export type PersonaMeta = { key: string; label: string; description: string };
export type PersonaScores = { symbol: string; as_of: string | null; archetypes: Record<string, number> };

async function jfetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} ${text}`);
  }
  if (res.status === 204) return undefined as unknown as T;
  return res.json() as Promise<T>;
}

export const api = {
  agents: () => jfetch<{ agents: AgentDef[]; edges: AgentEdge[] }>("/api/agents"),
  themes: {
    list: () => jfetch<Theme[]>("/api/themes"),
    create: (body: { name: string; thesis: string; chokepoint: string }) =>
      jfetch<Theme>("/api/themes", { method: "POST", body: JSON.stringify(body) }),
    remove: (id: string) => jfetch<void>(`/api/themes/${id}`, { method: "DELETE" }),
    addSymbol: (id: string, symbol: string) =>
      jfetch<Theme>(`/api/themes/${id}/symbols`, { method: "POST", body: JSON.stringify({ symbol }) }),
    removeSymbol: (id: string, symbol: string) =>
      jfetch<void>(`/api/themes/${id}/symbols/${symbol}`, { method: "DELETE" }),
    regime: (id: string) => jfetch<ThemeRegime>(`/api/themes/${id}/regime`),
  },
  runs: {
    list: () => jfetch<RunListItem[]>("/api/runs"),
    get: (id: string) => jfetch<Run>(`/api/runs/${id}`),
    start: (themeId: string) =>
      jfetch<Run>("/api/runs", { method: "POST", body: JSON.stringify({ theme_id: themeId }) }),
  },
  performance: {
    curve: () => jfetch<{ points: EquityPoint[]; starting_equity: number }>("/api/performance/curve"),
    today: () => jfetch<TodayPerf>("/api/performance/today"),
  },
  positions: () => jfetch<Position[]>("/api/positions"),
  tradeIntents: {
    listByRun: (runId: string) => jfetch<TradeIntent[]>(`/api/trade-intents/by-run/${runId}`),
    get: (intentId: string) => jfetch<TradeIntent>(`/api/trade-intents/${intentId}`),
    buildPmcc: (runId: string, symbol: string, body: { contracts: number; leap_delta_target?: number; short_delta_target?: number }) =>
      jfetch<TradeIntent>(`/api/trade-intents/${runId}/${symbol}/build-pmcc`, {
        method: "POST", body: JSON.stringify(body),
      }),
    submit: (intentId: string) =>
      jfetch<{ status: string; intent_id: string; execution?: any; gate?: string; reason?: string }>(`/api/trade-intents/${intentId}/submit`, {
        method: "POST",
      }),
    cancel: (intentId: string) =>
      jfetch<{ status: string; intent_id: string }>(`/api/trade-intents/${intentId}/cancel`, {
        method: "POST",
      }),
  },
  scheduler: {
    status: () => jfetch<SchedulerStatus>("/api/scheduler/status"),
  },
  managers: {
    list: () => jfetch<Manager[]>("/api/managers"),
    holdings: (slug: string, limit = 100) =>
      jfetch<Manager & { holdings: ManagerHolding[] }>(`/api/managers/${slug}/holdings?limit=${limit}`),
    overlap: (opts?: { theme?: string; minManagers?: number }) => {
      const p = new URLSearchParams();
      if (opts?.theme) p.set("theme", opts.theme);
      if (opts?.minManagers) p.set("min_managers", String(opts.minManagers));
      const qs = p.toString();
      return jfetch<OverlapRow[]>(`/api/overlap${qs ? `?${qs}` : ""}`);
    },
    forSymbol: (symbol: string) => jfetch<SmartMoney>(`/api/managers/symbol/${symbol}`),
    flow: (symbols: string[]) =>
      jfetch<Record<string, UwFlow>>(`/api/managers/flow?symbols=${encodeURIComponent(symbols.join(","))}`),
    mentions: (symbol?: string) =>
      jfetch<Mention[]>(`/api/mentions${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ""}`),
    rotation: () => jfetch<RotationRow[]>("/api/rotation"),
  },
  report: {
    morning: (opts?: { refresh?: boolean }) =>
      jfetch<MorningReport>(`/api/report/morning${opts?.refresh ? "?refresh=true" : ""}`),
    pulse: () => jfetch<IntradayPulse>("/api/report/pulse"),
  },
  research: {
    universeGraph: (limit = 100) =>
      jfetch<UniverseGraphNode[]>(`/api/research/universe-graph?limit=${limit}`),
    featuresLatest: (symbols?: string[]) =>
      jfetch<FeatureSnapshot[]>(`/api/research/features/latest${symbols?.length ? `?symbols=${encodeURIComponent(symbols.join(","))}` : ""}`),
    featureHistory: (symbol: string) =>
      jfetch<FeatureSnapshot[]>(`/api/research/features/${encodeURIComponent(symbol)}`),
    ranking: (horizon = 20, model: "ridge" | "gbm" = "ridge") =>
      jfetch<RankReport>(`/api/research/ranking?horizon=${horizon}&model=${model}`),
    eventStudy: () => jfetch<EventStudyReport>("/api/research/event-study"),
    monteCarlo: (symbol: string, horizon = 20) =>
      jfetch<MonteCarloReport>(`/api/research/montecarlo/${encodeURIComponent(symbol)}?horizon=${horizon}`),
    impactGraph: (lookbackDays = 14) =>
      jfetch<ImpactGraph>(`/api/research/impact-graph?lookback_days=${lookbackDays}`),
    personasMeta: () => jfetch<PersonaMeta[]>("/api/research/personas"),
    personas: (symbol: string) =>
      jfetch<PersonaScores>(`/api/research/personas/${encodeURIComponent(symbol)}`),
  },
  admin: {
    autotradeStatus: (token: string) =>
      jfetch<{
        env_autotrade_enabled: boolean;
        db_autotrade_enabled: boolean;
        effective_enabled: boolean;
        last_kill_at: string | null;
        kill_reason: string | null;
        updated_at: string | null;
        updated_by: string | null;
      }>("/api/admin/autotrade/status", { headers: { "X-Admin-Token": token } }),
    autotradeDisable: (token: string, body: { reason?: string; actor?: string }) =>
      jfetch<{ ok: boolean; autotrade_enabled: boolean; reason: string }>(
        "/api/admin/autotrade/disable",
        { method: "POST", headers: { "X-Admin-Token": token }, body: JSON.stringify(body) },
      ),
    autotradeEnable: (token: string, body: { reason?: string; actor?: string }) =>
      jfetch<{ ok: boolean; autotrade_enabled: boolean }>(
        "/api/admin/autotrade/enable",
        { method: "POST", headers: { "X-Admin-Token": token }, body: JSON.stringify(body) },
      ),
    listManagedPositions: (token: string) =>
      jfetch<Array<{
        id: string; symbol: string; structure: string; qty: number;
        status: string; position_state: string;
        leap_strike: number | null; leap_expiry: string | null;
        short_call_strike: number | null; short_call_expiry: string | null;
        net_debit_filled: number | null; created_at: string | null;
      }>>("/api/admin/positions/managed", { headers: { "X-Admin-Token": token } }),
    exitPosition: (token: string, intentId: string) =>
      jfetch<any>(`/api/admin/positions/exit/${intentId}`, {
        method: "POST", headers: { "X-Admin-Token": token },
      }),
  },
};
