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
  last_status: "ok" | "partial" | "error" | null;
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
    list: () => jfetch<Run[]>("/api/runs"),
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
