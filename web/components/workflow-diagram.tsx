"use client";

/**
 * Agentic Edge — the digital trading floor.
 *
 * Illustrated workflow showing each agent as a digital employee at their
 * workstation: Market analyst at the candlestick monitor, Sentiment
 * analyst reading a newspaper, News analyst at the headline wall,
 * Fundamentals analyst with calculator + ledger, Options Flow analyst
 * at the gamma + chain workstation, Bull and Bear at podiums, Research
 * Manager on the bench, Trader at the order ticket, three Risk voices
 * around a round table, the Portfolio Manager at the executive desk,
 * the Theme Ranker at the whiteboard, and the paper-trade door.
 *
 * Lives on the Home tab. Click any employee to see their role and a
 * sample line of reasoning. Click "Run a theme" to walk through the
 * full sequence; the agent in flight pulses, finished agents go green.
 *
 * Inherits the existing app palette from globals.css (`--color-*`).
 * Honors `prefers-reduced-motion` — animations freeze gracefully.
 */

import * as React from "react";

// Agent ids are internal — the user-visible labels live in INFO below.
// Stable ids let the SVG and the run-sequence array reference each role
// independently of how it's labeled on screen.
type AgentId =
  | "market-feed" | "flow-feed" | "fundamentals-feed" | "macro-feed" | "broker-feed"
  | "market" | "sentiment" | "news" | "fundamentals" | "options-flow"
  | "bull" | "bear" | "research-mgr" | "trader"
  | "risk" | "pm"
  | "ranker" | "broker";

interface AgentInfo {
  title: string;
  role: string;
  desc: string;
  quote?: string;
}

const INFO: Record<AgentId, AgentInfo> = {
  "market-feed": {
    title: "Market data · live OHLCV + chains",
    role: "Data feed (equities + options)",
    desc: "The primary market-data feed. Streams the price aggregates the Market analyst reads and the full options chains the Options Flow analyst reads.",
  },
  "flow-feed": {
    title: "Options flow · institutional positioning",
    role: "Data feed",
    desc: "Sweeps, blocks above ask, gamma exposure curves with call and put walls, max-pain levels per expiry, dark-pool prints. Smart-money grounding for the Options Flow analyst.",
  },
  "fundamentals-feed": {
    title: "Fundamentals · statements + ratios",
    role: "Data feed",
    desc: "Income statement, balance sheet, cash flow, ratios, owner earnings. Aggressively cached (24h TTL) since fundamentals change slowly.",
  },
  "macro-feed": {
    title: "Macro · rates + economic series",
    role: "Data feed",
    desc: "Treasury yields, Fed funds, CPI, GDP, payrolls. The macro context the News analyst layers on top of sector reads.",
  },
  "broker-feed": {
    title: "Brokerage feed · live quotes + account",
    role: "Data feed (live)",
    desc: "The brokerage feed provides Level-1 quotes, the live position book, NAV and P&L. The same gateway is the execution endpoint at the bottom — but execution is double-gated to paper-only.",
    quote: "L1 CRDO bid 84.20 × 500 / ask 84.22 × 400. NAV $1.04M. Optical exposure 12.6%.",
  },
  market: {
    title: "Market analyst",
    role: "Quick reasoning · MTF technicals",
    desc: "Reads daily and weekly aggregates from the market data feed. Outputs the structured technical setup that feeds the MTF axis of the scorecard.",
    quote: "CRDO daily and weekly are aligned bullish; momentum confirmed by MACD cross above signal line, with RSI(14) at 58 — room to run.",
  },
  sentiment: {
    title: "Sentiment analyst",
    role: "Quick reasoning · social + retail mood",
    desc: "Reads social-media and retail-sentiment proxies. Calibrates short-term mood vs. price action — a counter-signal when retail piles in late.",
    quote: "Retail enthusiasm rising but lagging price; flow is institutional, not chase. Sentiment supports the Bull but not as the primary driver.",
  },
  news: {
    title: "News analyst",
    role: "Quick reasoning · sector + macro headlines",
    desc: "Reads sector and macro news through the lens of the active theme. Surfaces catalysts and disconfirming evidence for the researchers to debate.",
    quote: "Hyperscaler capex revised up 18% for 2026 reinforces the 800G demand thesis. No disconfirming signal in last-72h news flow.",
  },
  fundamentals: {
    title: "Fundamentals analyst",
    role: "Quick reasoning · margins, FCF, leverage",
    desc: "Quality screen at the workstation: gross / operating margins, ROIC, FCF yield, leverage, R&D intensity. Flags zombie tickers that ride a thesis without earning capital.",
    quote: "CRDO trailing GM 65.2%, FCF yield 3.4%, net cash. Quality-tilt allocation supported within the optical theme.",
  },
  "options-flow": {
    title: "Options flow analyst",
    role: "Quick reasoning · gamma, sweeps, max pain",
    desc: "Reads the institutional-flow feed and the options-chain feed together: gamma walls, max pain per expiry, unusual sweeps, GEX. Outputs the Options Sentiment axis (institutional positioning vs. retail).",
    quote: "Call wall at 95 reinforces upside magnet; aggregate sweep premium $4.2M biased above ask in 2026-06 expiries. Gamma flips negative below 78.",
  },
  bull: {
    title: "Bull researcher",
    role: "Deep reasoning · long thesis advocate",
    desc: "At the podium, building the strongest possible long case from all five analyst reports. Defends against the Bear in N rounds.",
    quote: "Institutional positioning + clean technicals + thesis-confirming fundamentals. The cleanest expression of the 800G ramp; rerate is in front of us.",
  },
  bear: {
    title: "Bear researcher",
    role: "Deep reasoning · short / skip thesis advocate",
    desc: "At the opposite podium, building the strongest short or skip case. Looks for crowded trades, negative gamma traps, fundamental softness behind a hot narrative.",
    quote: "Premium sweep volume can fade post-earnings; if the call wall breaks, gamma flips and stops cascade. Position sizing must reflect this asymmetric tail.",
  },
  "research-mgr": {
    title: "Research manager",
    role: "Deep reasoning · the judge",
    desc: "On the bench between the two podiums. Reads the full debate transcript, picks a side or Hold, assigns a 1–5 conviction score. The Conviction Gate auto-Holds anything below threshold.",
    quote: "Buy. Conviction 4/5. Bull case grounded in flow + fundamentals; Bear tail risk priced into sizing not into the thesis.",
  },
  trader: {
    title: "Trader",
    role: "Deep reasoning · order ticket",
    desc: "At the trading desk. Translates the research plan into a concrete order: action, size, entry zone, stop level, target, risk-reward.",
    quote: "Long 1.2% NAV. Entry 84–86, stop 76 (below gamma flip), target 102 (above call wall). Risk-reward 2.5:1.",
  },
  risk: {
    title: "Risk committee",
    role: "Deep reasoning · three-voice debate",
    desc: "Aggressive, Neutral and Conservative argue position size around the round table. Surfaces correlation, liquidity tails, and theme-overlap concerns before the PM signs.",
    quote: "Final consensus: 1.0% NAV (trimmed for theme overlap). Tighten stop to $76, honoring the gamma-flip level.",
  },
  pm: {
    title: "Portfolio manager",
    role: "Deep reasoning · final approve / reject",
    desc: "Executive office. Final approve / reject; final position size after rebalancing for theme overlap and correlation. Issues a TradeIntent or holds.",
    quote: "Approved. Final size 1.0% NAV. Stop 76, target 102. Trade routed to the paper account.",
  },
  ranker: {
    title: "Theme ranker",
    role: "Deep reasoning · cross-ticker prioritization",
    desc: "At the whiteboard, fans every TickerScore in the theme back in, ranks them, and writes the prose justification for the leaderboard.",
    quote: "Top names are the cleanest expressions of the thesis. Bottom of the list: execution-risk-heavy — underweight.",
  },
  broker: {
    title: "Brokerage · paper-trade gateway",
    role: "Execution",
    desc: "Routes approved TradeIntents to the paper-only brokerage. Hard-gated: the broker-mode flag must be set to paper AND the account ID prefix must match the paper convention — both must hold or the call raises. Live trading is intentionally not a path the agents can take.",
  },
};

const RUN_SEQUENCE: AgentId[] = [
  "market-feed", "flow-feed", "fundamentals-feed", "macro-feed", "broker-feed",
  "market", "sentiment", "news", "fundamentals", "options-flow",
  "bull", "bear", "research-mgr", "trader",
  "risk", "pm",
  "ranker", "broker",
];

type Status = "idle" | "running" | "done";

export default function WorkflowDiagram() {
  const [selected, setSelected] = React.useState<AgentId | null>(null);
  const [statuses, setStatuses] = React.useState<Partial<Record<AgentId, Status>>>({});
  const [running, setRunning] = React.useState(false);
  const timerRef = React.useRef<ReturnType<typeof setInterval> | null>(null);

  const stop = React.useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setRunning(false);
  }, []);

  const reset = React.useCallback(() => {
    stop();
    setStatuses({});
    setSelected(null);
  }, [stop]);

  const play = React.useCallback(() => {
    stop();
    setStatuses({});
    setRunning(true);
    let i = 0;
    timerRef.current = setInterval(() => {
      if (i >= RUN_SEQUENCE.length) {
        stop();
        return;
      }
      const cur = RUN_SEQUENCE[i];
      const prev = i > 0 ? RUN_SEQUENCE[i - 1] : null;
      setStatuses((s) => ({
        ...s,
        ...(prev ? { [prev]: "done" } : {}),
        [cur]: "running",
      }));
      i++;
    }, 600);
  }, [stop]);

  React.useEffect(() => () => stop(), [stop]);

  const info = selected ? INFO[selected] : null;
  const onSelect = (id: AgentId) => setSelected(id);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={play}
          disabled={running}
          className="btn btn-primary disabled:opacity-60 disabled:cursor-not-allowed"
        >
          {running ? "Running…" : "Run a theme"}
        </button>
        <button onClick={reset} className="btn btn-ghost">Reset</button>
        <span className="ml-auto chip">
          Theme · <strong className="ml-1 text-[var(--color-fg)] font-medium">Optical networking — 800G/1.6T transceivers</strong>
        </span>
      </div>

      <div className="glass overflow-hidden">
        <div className="overflow-x-auto">
          <FloorSvg statuses={statuses} selected={selected} onSelect={onSelect} />
        </div>
      </div>

      <div className="glass p-5">
        {info ? (
          <>
            <div className="flex items-center justify-between mb-1">
              <h3 className="text-base font-semibold tracking-tight text-[var(--color-fg)]">{info.title}</h3>
              <button
                onClick={() => setSelected(null)}
                className="text-xs text-[var(--color-fg-dim)] hover:text-[var(--color-fg)]"
              >
                Close
              </button>
            </div>
            <div className="label-eyebrow mb-3">{info.role}</div>
            <p className="text-sm text-[var(--color-fg-muted)] leading-relaxed">{info.desc}</p>
            {info.quote && (
              <blockquote className="mt-3 border-l-2 border-[var(--color-accent)] bg-[color-mix(in_srgb,var(--color-accent)_8%,transparent)] rounded-r px-4 py-2.5 text-sm italic text-[var(--color-fg)]">
                {info.quote}
              </blockquote>
            )}
          </>
        ) : (
          <>
            <h3 className="text-base font-semibold tracking-tight text-[var(--color-fg)] mb-1">
              Click any digital employee above
            </h3>
            <div className="label-eyebrow mb-3">How the team works on every theme</div>
            <p className="text-sm text-[var(--color-fg-muted)] leading-relaxed">
              The full hedge-fund team runs in parallel for every ticker in the theme. Five analysts feed reports
              into the Bull–Bear debate; the Research Manager picks a side and a conviction score; the Trader
              writes a concrete order ticket; three Risk voices argue position size; the Portfolio Manager signs
              off; the Theme Ranker writes the leaderboard; the paper-trade gateway logs and blocks anything
              outside paper mode.
            </p>
          </>
        )}
      </div>

      {/* Animations honour prefers-reduced-motion */}
      <style jsx global>{`
        @media (prefers-reduced-motion: reduce) {
          .ae-anim { animation: none !important; }
        }
      `}</style>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SVG floor — every group with [data-id] is interactive.
// ---------------------------------------------------------------------------

function FloorSvg({
  statuses,
  selected,
  onSelect,
}: {
  statuses: Partial<Record<AgentId, Status>>;
  selected: AgentId | null;
  onSelect: (id: AgentId) => void;
}) {
  const click = (id: AgentId) => () => onSelect(id);
  const cls = (id: AgentId) => {
    const status = statuses[id];
    return [
      "ae-agent",
      status === "running" ? "ae-running" : "",
      status === "done" ? "ae-done" : "",
      selected === id ? "ae-active" : "",
    ].filter(Boolean).join(" ");
  };

  return (
    <svg
      viewBox="0 0 1400 1180"
      xmlns="http://www.w3.org/2000/svg"
      className="block w-full h-auto min-w-[1100px]"
      role="img"
      aria-label="Agentic Edge — digital trading floor"
    >
      <defs>
        <filter id="ae-shadow" x="-20%" y="-20%" width="140%" height="140%">
          <feDropShadow dx="0" dy="2" stdDeviation="4" floodOpacity="0.45" floodColor="#000000" />
        </filter>
        <linearGradient id="ae-grad-up" x1="0" x2="0" y1="1" y2="0">
          <stop offset="0%" stopColor="#34d399" stopOpacity="0" />
          <stop offset="100%" stopColor="#34d399" stopOpacity="0.6" />
        </linearGradient>
        <linearGradient id="ae-grad-down" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor="#f87171" stopOpacity="0" />
          <stop offset="100%" stopColor="#f87171" stopOpacity="0.6" />
        </linearGradient>
        <linearGradient id="ae-grad-rack" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor="#11141b" />
          <stop offset="100%" stopColor="#0b0d12" />
        </linearGradient>
        <linearGradient id="ae-grad-brand" x1="0" x2="1" y1="0" y2="0">
          <stop offset="0%" stopColor="#7c5cff" />
          <stop offset="100%" stopColor="#38bdf8" />
        </linearGradient>
        <clipPath id="ae-clip-poly"><rect x="10" y="42" width="220" height="14" rx="2" /></clipPath>
        <clipPath id="ae-clip-uw"><rect x="10" y="42" width="220" height="14" rx="2" /></clipPath>
        <clipPath id="ae-clip-fmp"><rect x="10" y="42" width="220" height="14" rx="2" /></clipPath>
        <clipPath id="ae-clip-av"><rect x="10" y="42" width="220" height="14" rx="2" /></clipPath>
        <clipPath id="ae-clip-ib"><rect x="10" y="42" width="220" height="14" rx="2" /></clipPath>
        <clipPath id="ae-clip-news"><rect x="6" y="6" width="184" height="66" rx="2" /></clipPath>
      </defs>

      <style>{`
        .ae-agent { cursor: pointer; transition: transform 220ms ease; }
        .ae-agent:hover { transform: translateY(-3px); }
        .ae-agent .ae-bg { transition: fill 220ms ease, stroke 220ms ease; }
        .ae-agent:hover .ae-bg { stroke: #7c5cff; }
        .ae-agent.ae-active .ae-bg { stroke: #7c5cff; fill: #1c2230; }
        .ae-agent.ae-running .ae-pulse { animation: ae-pulse 1.1s ease-in-out infinite; transform-origin: center; transform-box: fill-box; }
        .ae-agent.ae-done .ae-pulse { fill: #34d399; }
        @keyframes ae-pulse { 0%,100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.5; transform: scale(1.6); } }
        @keyframes ae-draw { 0% { stroke-dashoffset: 200; } 100% { stroke-dashoffset: 0; } }
        .ae-chart-line { stroke-dasharray: 200; stroke-dashoffset: 200; animation: ae-draw 2.4s ease-out infinite alternate; }
        @keyframes ae-tick { from { transform: translateX(0); } to { transform: translateX(-50%); } }
        .ae-ticker { animation: ae-tick 22s linear infinite; }
        @keyframes ae-flutter { 0%,100% { transform: rotate(-2deg); } 50% { transform: rotate(2deg); } }
        .ae-newspaper { animation: ae-flutter 4s ease-in-out infinite; transform-origin: bottom center; transform-box: fill-box; }
        @keyframes ae-write { 0% { stroke-dashoffset: 90; } 100% { stroke-dashoffset: 0; } }
        .ae-rank-bar { stroke-dasharray: 90; stroke-dashoffset: 90; animation: ae-write 1.8s ease-out forwards; }
        @keyframes ae-blink { 50% { opacity: 0.4; } }
        .ae-blink { animation: ae-blink 1.6s ease-in-out infinite; }
        @keyframes ae-flip { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
        .ae-flip { animation: ae-flip 1.4s ease-in-out infinite; }
        @keyframes ae-scroll-up { from { transform: translateY(0); } to { transform: translateY(-50%); } }
        .ae-scroll-up { animation: ae-scroll-up 18s linear infinite; }
        @media (prefers-reduced-motion: reduce) {
          .ae-ticker, .ae-newspaper, .ae-chart-line, .ae-rank-bar, .ae-blink, .ae-flip, .ae-scroll-up { animation: none !important; }
        }
      `}</style>

      {/* Title */}
      <text x="40" y="36" fontSize="16" fontWeight="500" fill="#e7eaf2">Agentic Edge — the digital trading floor</text>
      <text x="40" y="56" fontSize="11" fill="#98a0b3">Each agent is a digital employee at their workstation. Hover · click to read what they're saying about CRDO right now.</text>

      {/* DATA ROOM */}
      <g transform="translate(40, 80)">
        <rect width="1320" height="120" rx="12" fill="url(#ae-grad-rack)" stroke="#232a3a" />
        <text x="20" y="26" fontSize="11" fontWeight="600" fill="#6c7388" letterSpacing="0.08em">DATA ROOM · LIVE FEEDS</text>

        <g className={cls("market-feed")} onClick={click("market-feed")} transform="translate(20, 42)">
          <rect className="ae-bg" width="240" height="62" rx="8" fill="#0b0d12" stroke="#232a3a" />
          <circle className="ae-pulse" cx="14" cy="14" r="4" fill="#38bdf8" />
          <text x="26" y="18" fontSize="12" fontWeight="500" fill="#e7eaf2">Market data</text>
          <text x="26" y="32" fontSize="9" fill="#6c7388">OHLCV · options chains</text>
          <g clipPath="url(#ae-clip-poly)">
            <g className="ae-ticker" fontSize="9" fill="#7dd3fc" fontFamily="ui-monospace, monospace">
              <text x="10" y="52">CRDO 84.21 ▲ · LITE 102.4 ▲ · COHR 91.6 ▲ · FN 188.3 ▼ · AAOI 19.2 ▲ · CRDO 84.21 ▲ · LITE 102.4 ▲ · COHR 91.6 ▲ · FN 188.3 ▼ · AAOI 19.2 ▲ · </text>
            </g>
          </g>
        </g>

        <g className={cls("flow-feed")} onClick={click("flow-feed")} transform="translate(280, 42)">
          <rect className="ae-bg" width="240" height="62" rx="8" fill="#0b0d12" stroke="#232a3a" />
          <circle className="ae-pulse" cx="14" cy="14" r="4" fill="#7c5cff" />
          <text x="26" y="18" fontSize="12" fontWeight="500" fill="#e7eaf2">Options flow</text>
          <text x="26" y="32" fontSize="9" fill="#6c7388">Flow · gamma · max pain</text>
          <g clipPath="url(#ae-clip-uw)">
            <g className="ae-ticker" fontSize="9" fill="#c4b5fd" fontFamily="ui-monospace, monospace">
              <text x="10" y="52">SWEEP CRDO C95 +$1.2M · WALL LITE C100 · MAX-PAIN COHR 90 · GEX +$420M · SWEEP CRDO C95 +$1.2M · WALL LITE C100 · </text>
            </g>
          </g>
        </g>

        <g className={cls("fundamentals-feed")} onClick={click("fundamentals-feed")} transform="translate(540, 42)">
          <rect className="ae-bg" width="240" height="62" rx="8" fill="#0b0d12" stroke="#232a3a" />
          <circle className="ae-pulse" cx="14" cy="14" r="4" fill="#34d399" />
          <text x="26" y="18" fontSize="12" fontWeight="500" fill="#e7eaf2">Fundamentals</text>
          <text x="26" y="32" fontSize="9" fill="#6c7388">Income · balance · cash flow</text>
          <g clipPath="url(#ae-clip-fmp)">
            <g className="ae-ticker" fontSize="9" fill="#6ee7b7" fontFamily="ui-monospace, monospace">
              <text x="10" y="52">CRDO GM 65.2% · ROIC 18% · FCF $124M · NET CASH $310M · CRDO GM 65.2% · ROIC 18% · FCF $124M · NET CASH $310M · </text>
            </g>
          </g>
        </g>

        <g className={cls("macro-feed")} onClick={click("macro-feed")} transform="translate(800, 42)">
          <rect className="ae-bg" width="240" height="62" rx="8" fill="#0b0d12" stroke="#232a3a" />
          <circle className="ae-pulse" cx="14" cy="14" r="4" fill="#fbbf24" />
          <text x="26" y="18" fontSize="12" fontWeight="500" fill="#e7eaf2">Macro</text>
          <text x="26" y="32" fontSize="9" fill="#6c7388">Macro · global indicators</text>
          <g clipPath="url(#ae-clip-av)">
            <g className="ae-ticker" fontSize="9" fill="#fcd34d" fontFamily="ui-monospace, monospace">
              <text x="10" y="52">10Y 4.32% · FFR 4.50 · CPI 2.8% · NFP +212K · 10Y 4.32% · FFR 4.50 · CPI 2.8% · NFP +212K · 10Y 4.32% · FFR 4.50 · </text>
            </g>
          </g>
        </g>

        <g className={cls("broker-feed")} onClick={click("broker-feed")} transform="translate(1060, 42)">
          <rect className="ae-bg" width="240" height="62" rx="8" fill="#0b0d12" stroke="#f87171" strokeWidth="1.5" />
          <circle className="ae-pulse ae-blink" cx="14" cy="14" r="4" fill="#f87171" />
          <text x="26" y="18" fontSize="12" fontWeight="500" fill="#e7eaf2">Brokerage <tspan fontSize="9" fill="#fca5a5">live</tspan></text>
          <text x="26" y="32" fontSize="9" fill="#6c7388">Market data · positions · P&amp;L</text>
          <g clipPath="url(#ae-clip-ib)">
            <g className="ae-ticker" fontSize="9" fill="#fca5a5" fontFamily="ui-monospace, monospace">
              <text x="10" y="52">L1 CRDO BID 84.20×500 ASK 84.22×400 · NAV $1.04M · CASH $312K · L1 CRDO BID 84.20×500 ASK 84.22×400 · </text>
            </g>
          </g>
        </g>
      </g>

      {/* Cables down to analysts */}
      <g stroke="#232a3a" strokeWidth="1" fill="none" opacity="0.7">
        <path d="M 160,210 C 160,225 175,230 175,245" />
        <path d="M 420,210 C 420,225 445,230 445,245" />
        <path d="M 680,210 C 680,225 715,230 715,245" />
        <path d="M 940,210 C 940,225 985,230 985,245" />
        <path d="M 1200,210 C 1200,225 1255,230 1255,245" />
      </g>

      {/* ANALYST POD */}
      <g transform="translate(40, 240)">
        <text x="0" y="0" fontSize="11" fontWeight="600" fill="#6c7388" letterSpacing="0.08em">ANALYST POD · QUICK REASONING</text>

        {/* Market */}
        <g className={cls("market")} onClick={click("market")} transform="translate(0, 14)">
          <rect className="ae-bg" width="252" height="220" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <rect x="14" y="160" width="224" height="46" rx="3" fill="#1c2230" />
          <g transform="translate(28, 56)">
            <rect width="92" height="64" rx="5" fill="#0b0d12" />
            <g strokeWidth="2">
              <line x1="14" y1="42" x2="14" y2="55" stroke="#34d399" /><rect x="11" y="44" width="6" height="8" fill="#34d399" />
              <line x1="26" y1="36" x2="26" y2="50" stroke="#34d399" /><rect x="23" y="38" width="6" height="10" fill="#34d399" />
              <line x1="38" y1="40" x2="38" y2="58" stroke="#f87171" /><rect x="35" y="42" width="6" height="14" fill="#f87171" />
              <line x1="50" y1="32" x2="50" y2="46" stroke="#34d399" /><rect x="47" y="34" width="6" height="10" fill="#34d399" />
              <line x1="62" y1="26" x2="62" y2="42" stroke="#34d399" /><rect x="59" y="28" width="6" height="12" fill="#34d399" />
              <line x1="74" y1="22" x2="74" y2="38" stroke="#34d399" /><rect x="71" y="24" width="6" height="12" fill="#34d399" />
            </g>
          </g>
          <g transform="translate(132, 56)">
            <rect width="92" height="64" rx="5" fill="#0b0d12" />
            <g>
              <rect x="14" y="40" width="6" height="12" fill="#38bdf8" /><rect x="24" y="36" width="6" height="16" fill="#38bdf8" />
              <rect x="34" y="30" width="6" height="22" fill="#38bdf8" /><rect x="44" y="34" width="6" height="18" fill="#38bdf8" />
              <rect x="54" y="28" width="6" height="24" fill="#38bdf8" /><rect x="64" y="20" width="6" height="32" fill="#38bdf8" />
              <rect x="74" y="16" width="6" height="36" fill="#38bdf8" />
            </g>
            <text x="46" y="60" textAnchor="middle" fontSize="7" fill="#6c7388">MACD</text>
          </g>
          <g transform="translate(95, 122)">
            <rect x="10" y="22" width="42" height="6" rx="2" fill="#1c2230" />
            <path d="M 8,32 Q 8,18 31,18 Q 54,18 54,32 L 54,42 L 8,42 Z" fill="#7c5cff" />
            <circle cx="31" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
            <circle cx="27" cy="10" r="3" fill="none" stroke="#0b0d12" strokeWidth="0.8" />
            <circle cx="35" cy="10" r="3" fill="none" stroke="#0b0d12" strokeWidth="0.8" />
            <line x1="30" y1="10" x2="32" y2="10" stroke="#0b0d12" strokeWidth="0.8" />
          </g>
          <text x="126" y="186" textAnchor="middle" fontSize="13" fontWeight="500" fill="#e7eaf2">Market analyst</text>
          <text x="126" y="200" textAnchor="middle" fontSize="10" fill="#98a0b3">Daily/weekly MTF · indicators</text>
          <circle className="ae-pulse" cx="232" cy="20" r="4" fill="#6c7388" />
        </g>

        {/* Sentiment */}
        <g className={cls("sentiment")} onClick={click("sentiment")} transform="translate(270, 14)">
          <rect className="ae-bg" width="252" height="220" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <rect x="14" y="160" width="224" height="46" rx="3" fill="#1c2230" />
          <g className="ae-newspaper" transform="translate(70, 60)">
            <rect width="96" height="84" rx="2" fill="#f5f5f0" stroke="#1c2230" />
            <text x="48" y="14" textAnchor="middle" fontSize="9" fontWeight="600" fill="#11141b" fontFamily="serif">THE MARKET TIMES</text>
            <line x1="6" y1="20" x2="90" y2="20" stroke="#1c2230" strokeWidth="0.5" />
            <text x="48" y="32" textAnchor="middle" fontSize="6" fontWeight="600" fill="#11141b">800G demand surges</text>
            <g stroke="#6c7388" strokeWidth="0.6">
              <line x1="8" y1="40" x2="88" y2="40" />
              <line x1="8" y1="46" x2="88" y2="46" />
              <line x1="8" y1="52" x2="88" y2="52" />
              <line x1="8" y1="58" x2="60" y2="58" />
              <line x1="50" y1="64" x2="88" y2="64" />
              <line x1="8" y1="70" x2="88" y2="70" />
              <line x1="8" y1="76" x2="70" y2="76" />
            </g>
            <rect x="50" y="40" width="30" height="18" fill="#e5e7eb" />
            <polyline points="52,55 58,52 64,48 70,44 76,42" fill="none" stroke="#34d399" strokeWidth="1" />
          </g>
          <g transform="translate(95, 122)">
            <rect x="10" y="22" width="42" height="6" rx="2" fill="#1c2230" />
            <path d="M 8,32 Q 8,18 31,18 Q 54,18 54,32 L 54,42 L 8,42 Z" fill="#f472b6" />
            <circle cx="31" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
            <rect x="-5" y="12" width="9" height="10" rx="1" fill="#6c7388" />
            <path d="M 4,15 Q 8,15 8,18 Q 8,21 4,21" fill="none" stroke="#6c7388" strokeWidth="1" />
          </g>
          <text x="126" y="186" textAnchor="middle" fontSize="13" fontWeight="500" fill="#e7eaf2">Sentiment analyst</text>
          <text x="126" y="200" textAnchor="middle" fontSize="10" fill="#98a0b3">Newspaper · social · retail mood</text>
          <circle className="ae-pulse" cx="232" cy="20" r="4" fill="#6c7388" />
        </g>

        {/* News */}
        <g className={cls("news")} onClick={click("news")} transform="translate(540, 14)">
          <rect className="ae-bg" width="252" height="220" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <rect x="14" y="160" width="224" height="46" rx="3" fill="#1c2230" />
          <g transform="translate(28, 50)">
            <rect width="196" height="78" rx="5" fill="#0b0d12" />
            <g clipPath="url(#ae-clip-news)">
              <g className="ae-scroll-up" fontSize="7.5" fill="#cbd5e1" fontFamily="ui-monospace, monospace">
                <text x="10" y="16">▶ NVDA Q1 beat — AI demand outpacing supply</text>
                <text x="10" y="28" fill="#fbbf24">⚠ Hyperscaler capex revised up 18% for 2026</text>
                <text x="10" y="40">▶ Marvell announces 1.6T DSP roadmap</text>
                <text x="10" y="52" fill="#86efac">✓ Coherent: optical orders +42% YoY</text>
                <text x="10" y="64">▶ Lumentum guides above consensus on AI mix</text>
                <text x="10" y="76">▶ Fed minutes signal patience on cuts</text>
                <text x="10" y="88">▶ Astera Labs adds CXL design wins</text>
                <text x="10" y="100">▶ Credo guides Q2: optical DSP ramp</text>
                <text x="10" y="112">▶ NVDA Q1 beat — AI demand outpacing supply</text>
                <text x="10" y="124" fill="#fbbf24">⚠ Hyperscaler capex revised up 18% for 2026</text>
                <text x="10" y="136">▶ Marvell announces 1.6T DSP roadmap</text>
                <text x="10" y="148" fill="#86efac">✓ Coherent: optical orders +42% YoY</text>
              </g>
            </g>
          </g>
          <g transform="translate(95, 122)">
            <rect x="10" y="22" width="42" height="6" rx="2" fill="#1c2230" />
            <path d="M 8,32 Q 8,18 31,18 Q 54,18 54,32 L 54,42 L 8,42 Z" fill="#38bdf8" />
            <circle cx="31" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
            <path d="M 22,8 Q 22,2 31,2 Q 40,2 40,8" stroke="#0b0d12" strokeWidth="1" fill="none" />
            <rect x="20" y="8" width="3" height="5" rx="1" fill="#0b0d12" />
            <rect x="39" y="8" width="3" height="5" rx="1" fill="#0b0d12" />
          </g>
          <text x="126" y="186" textAnchor="middle" fontSize="13" fontWeight="500" fill="#e7eaf2">News analyst</text>
          <text x="126" y="200" textAnchor="middle" fontSize="10" fill="#98a0b3">Sector + macro headlines</text>
          <circle className="ae-pulse" cx="232" cy="20" r="4" fill="#6c7388" />
        </g>

        {/* Fundamentals */}
        <g className={cls("fundamentals")} onClick={click("fundamentals")} transform="translate(810, 14)">
          <rect className="ae-bg" width="252" height="220" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <rect x="14" y="160" width="224" height="46" rx="3" fill="#1c2230" />
          <g transform="translate(40, 60)">
            <rect width="88" height="84" rx="2" fill="#fdf6e3" stroke="#a16207" />
            <line x1="44" y1="0" x2="44" y2="84" stroke="#a16207" strokeWidth="0.5" />
            <text x="6" y="12" fontSize="6" fill="#11141b" fontWeight="600">INCOME</text>
            <text x="48" y="12" fontSize="6" fill="#11141b" fontWeight="600">CASH FLOW</text>
            <g fontSize="6" fill="#11141b" fontFamily="ui-monospace, monospace">
              <text x="6" y="24">Rev   $620M</text><text x="6" y="34">GM    65.2%</text><text x="6" y="44">Op    $98M</text><text x="6" y="54">NI    $74M</text>
              <text x="48" y="24">CFO   $148M</text><text x="48" y="34">Capx  -$24M</text><text x="48" y="44">FCF   $124M</text>
              <text x="48" y="54" className="ae-flip" fill="#16a34a">FCF Y 3.4%</text>
            </g>
            <g transform="translate(58,58)">
              <circle cx="0" cy="0" r="9" fill="rgba(56,189,248,0.18)" stroke="#38bdf8" strokeWidth="1.4" />
              <line x1="6" y1="6" x2="14" y2="14" stroke="#38bdf8" strokeWidth="1.6" strokeLinecap="round" />
            </g>
          </g>
          <g transform="translate(140, 70)">
            <rect width="60" height="74" rx="4" fill="#0b0d12" />
            <rect x="6" y="6" width="48" height="14" rx="2" fill="#11141b" stroke="#34d399" />
            <text x="50" y="17" textAnchor="end" fontSize="9" fontWeight="500" fill="#34d399" fontFamily="ui-monospace, monospace" className="ae-flip">12,400</text>
            <g fill="#1c2230">
              <rect x="6" y="24" width="10" height="10" rx="1.5" /><rect x="18" y="24" width="10" height="10" rx="1.5" /><rect x="30" y="24" width="10" height="10" rx="1.5" /><rect x="42" y="24" width="12" height="10" rx="1.5" fill="#7c5cff" />
              <rect x="6" y="36" width="10" height="10" rx="1.5" /><rect x="18" y="36" width="10" height="10" rx="1.5" /><rect x="30" y="36" width="10" height="10" rx="1.5" /><rect x="42" y="36" width="12" height="10" rx="1.5" fill="#7c5cff" />
              <rect x="6" y="48" width="10" height="10" rx="1.5" /><rect x="18" y="48" width="10" height="10" rx="1.5" /><rect x="30" y="48" width="10" height="10" rx="1.5" /><rect x="42" y="48" width="12" height="22" rx="1.5" fill="#34d399" />
              <rect x="6" y="60" width="22" height="10" rx="1.5" /><rect x="30" y="60" width="10" height="10" rx="1.5" />
            </g>
          </g>
          <g transform="translate(95, 122)">
            <rect x="10" y="22" width="42" height="6" rx="2" fill="#1c2230" />
            <path d="M 8,32 Q 8,18 31,18 Q 54,18 54,32 L 54,42 L 8,42 Z" fill="#34d399" />
            <circle cx="31" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
            <rect x="22" y="8" width="18" height="3" rx="1" fill="#0b0d12" />
          </g>
          <text x="126" y="186" textAnchor="middle" fontSize="13" fontWeight="500" fill="#e7eaf2">Fundamentals analyst</text>
          <text x="126" y="200" textAnchor="middle" fontSize="10" fill="#98a0b3">Margins · FCF · leverage</text>
          <circle className="ae-pulse" cx="232" cy="20" r="4" fill="#6c7388" />
        </g>

        {/* Options Flow */}
        <g className={cls("options-flow")} onClick={click("options-flow")} transform="translate(1080, 14)">
          <rect className="ae-bg" width="252" height="220" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <rect x="14" y="160" width="224" height="46" rx="3" fill="#1c2230" />
          <g transform="translate(28, 50)">
            <rect width="92" height="78" rx="5" fill="#0b0d12" />
            <text x="6" y="12" fontSize="7" fill="#c4b5fd">GEX curve</text>
            <line x1="6" y1="40" x2="86" y2="40" stroke="#1c2230" strokeWidth="0.5" />
            <path className="ae-chart-line" d="M 6,60 Q 16,58 22,52 Q 28,38 36,28 Q 44,20 52,28 Q 60,38 66,28 Q 72,20 80,42 Q 84,58 86,68" fill="none" stroke="#7c5cff" strokeWidth="1.6" />
            <line x1="52" y1="14" x2="52" y2="68" stroke="#fbbf24" strokeWidth="0.6" strokeDasharray="2,2" />
            <text x="52" y="74" textAnchor="middle" fontSize="6" fill="#fbbf24">95</text>
          </g>
          <g transform="translate(132, 50)">
            <rect width="92" height="78" rx="5" fill="#0b0d12" />
            <text x="6" y="10" fontSize="6" fill="#6c7388">Strike   IV   OI   Vol</text>
            <g fontSize="6" fill="#cbd5e1" fontFamily="ui-monospace, monospace">
              <text x="6" y="20">82C  38%  4.1k  912</text>
              <text x="6" y="30">85C  41%  6.3k  1.4k</text>
              <text x="6" y="40" fill="#fbbf24">90C  44%  9.8k  2.1k</text>
              <text x="6" y="50" fill="#fb923c">95C  48%  12k   3.4k ★</text>
              <text x="6" y="60">100C 52%  7.2k  890</text>
              <text x="6" y="70">105C 56%  3.4k  220</text>
            </g>
          </g>
          <g transform="translate(95, 122)">
            <rect x="10" y="22" width="42" height="6" rx="2" fill="#1c2230" />
            <path d="M 8,32 Q 8,18 31,18 Q 54,18 54,32 L 54,42 L 8,42 Z" fill="#a78bfa" />
            <circle cx="31" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
            <path d="M 42,10 L 44,8 L 43,12 L 45,10" fill="#fbbf24" stroke="#fbbf24" strokeWidth="0.5" />
          </g>
          <text x="126" y="186" textAnchor="middle" fontSize="13" fontWeight="500" fill="#e7eaf2">Options flow analyst</text>
          <text x="126" y="200" textAnchor="middle" fontSize="10" fill="#98a0b3">Gamma · max pain · sweeps</text>
          <circle className="ae-pulse" cx="232" cy="20" r="4" fill="#6c7388" />
        </g>
      </g>

      {/* DEBATE */}
      <g transform="translate(40, 510)">
        <text x="0" y="0" fontSize="11" fontWeight="600" fill="#6c7388" letterSpacing="0.08em">DEBATE STAGE · DEEP REASONING · DIALECTICAL COLLABORATION</text>

        <g className={cls("bull")} onClick={click("bull")} transform="translate(0, 14)">
          <rect className="ae-bg" width="380" height="220" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <g transform="translate(14, 14)" opacity="0.22">
            <path d="M 0,180 L 70,150 L 140,120 L 210,100 L 280,70 L 352,40" stroke="#34d399" strokeWidth="3" fill="none" />
            <path d="M 0,180 L 70,150 L 140,120 L 210,100 L 280,70 L 352,40 L 352,180 Z" fill="url(#ae-grad-up)" />
          </g>
          <g transform="translate(150, 70)">
            <rect x="-30" y="38" width="80" height="60" rx="4" fill="#1c2230" />
            <rect x="-26" y="42" width="72" height="54" rx="3" fill="#232a3a" />
            <rect x="0" y="56" width="40" height="20" rx="2" fill="#34d399" />
            <text x="20" y="70" textAnchor="middle" fontSize="9" fontWeight="600" fill="#0b0d12">BULL</text>
            <g transform="translate(-2, -8)">
              <path d="M 0,32 Q 0,18 24,18 Q 48,18 48,32 L 48,42 L 0,42 Z" fill="#34d399" />
              <circle cx="24" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
              <path d="M 38,25 L 56,8 L 60,12 L 42,29 Z" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
              <circle cx="58" cy="6" r="4" fill="#34d399" />
              <text x="58" y="9" textAnchor="middle" fontSize="6" fill="#0b0d12">↑</text>
            </g>
            <g transform="translate(60, -38)">
              <rect width="180" height="38" rx="6" fill="#11141b" stroke="#34d399" />
              <path d="M 8,38 L 4,46 L 18,38 Z" fill="#11141b" stroke="#34d399" />
              <text x="10" y="14" fontSize="8.5" fill="#6ee7b7">"CRDO is the cleanest 800G play."</text>
              <text x="10" y="26" fontSize="8.5" fill="#6ee7b7">Flow + clean chart + quality FCF.</text>
            </g>
          </g>
          <text x="190" y="186" textAnchor="middle" fontSize="13" fontWeight="500" fill="#e7eaf2">Bull researcher</text>
          <text x="190" y="200" textAnchor="middle" fontSize="10" fill="#98a0b3">Long thesis advocate · deep reasoning</text>
          <circle className="ae-pulse" cx="360" cy="20" r="4" fill="#6c7388" />
        </g>

        <g className={cls("research-mgr")} onClick={click("research-mgr")} transform="translate(395, 14)">
          <rect className="ae-bg" width="240" height="220" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <g transform="translate(50, 60)">
            <rect x="0" y="50" width="140" height="40" rx="3" fill="#0b0d12" />
            <rect x="6" y="56" width="128" height="28" rx="2" fill="#1c2230" />
            <rect x="40" y="64" width="60" height="14" rx="1" fill="#7c5cff" />
            <text x="70" y="74" textAnchor="middle" fontSize="8" fontWeight="600" fill="#0b0d12">RESEARCH MGR</text>
            <g transform="translate(46, 0)">
              <path d="M 0,32 Q 0,18 24,18 Q 48,18 48,32 L 48,50 L 0,50 Z" fill="#7c5cff" />
              <circle cx="24" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
              <g transform="translate(50, 24)">
                <rect width="14" height="6" rx="1" fill="#a16207" />
                <rect x="14" y="-1" width="3" height="8" rx="1" fill="#a16207" />
              </g>
            </g>
            <g transform="translate(-30, 96)">
              <rect width="200" height="32" rx="4" fill="#11141b" stroke="#7c5cff" />
              <text x="10" y="14" fontSize="9" fontWeight="600" fill="#c4b5fd">DECISION: BUY · Conviction 4/5</text>
              <text x="10" y="25" fontSize="8" fill="#a78bfa">Bull case carries; Bear tail in sizing.</text>
            </g>
          </g>
          <text x="120" y="186" textAnchor="middle" fontSize="13" fontWeight="500" fill="#e7eaf2">Research manager</text>
          <text x="120" y="200" textAnchor="middle" fontSize="10" fill="#98a0b3">Synthesis + conviction · deep reasoning</text>
          <circle className="ae-pulse" cx="220" cy="20" r="4" fill="#6c7388" />
        </g>

        <g className={cls("bear")} onClick={click("bear")} transform="translate(650, 14)">
          <rect className="ae-bg" width="380" height="220" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <g transform="translate(14, 14)" opacity="0.22">
            <path d="M 0,40 L 70,70 L 140,90 L 210,110 L 280,140 L 352,170" stroke="#f87171" strokeWidth="3" fill="none" />
            <path d="M 0,40 L 70,70 L 140,90 L 210,110 L 280,140 L 352,170 L 352,180 L 0,180 Z" fill="url(#ae-grad-down)" />
          </g>
          <g transform="translate(170, 70)">
            <rect x="-30" y="38" width="80" height="60" rx="4" fill="#1c2230" />
            <rect x="-26" y="42" width="72" height="54" rx="3" fill="#232a3a" />
            <rect x="0" y="56" width="40" height="20" rx="2" fill="#f87171" />
            <text x="20" y="70" textAnchor="middle" fontSize="9" fontWeight="600" fill="#0b0d12">BEAR</text>
            <g transform="translate(-2, -8)">
              <path d="M 0,32 Q 0,18 24,18 Q 48,18 48,32 L 48,42 L 0,42 Z" fill="#f87171" />
              <circle cx="24" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
              <path d="M 38,25 L 56,42 L 60,38 L 42,21 Z" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
              <circle cx="58" cy="44" r="4" fill="#f87171" />
              <text x="58" y="47" textAnchor="middle" fontSize="6" fill="#0b0d12">↓</text>
            </g>
            <g transform="translate(-220, -38)">
              <rect width="200" height="38" rx="6" fill="#11141b" stroke="#f87171" />
              <path d="M 184,38 L 188,46 L 174,38 Z" fill="#11141b" stroke="#f87171" />
              <text x="10" y="14" fontSize="8.5" fill="#fca5a5">"Crowded. Gamma flips below 78."</text>
              <text x="10" y="26" fontSize="8.5" fill="#fca5a5">Stops cascade if call wall breaks.</text>
            </g>
          </g>
          <text x="190" y="186" textAnchor="middle" fontSize="13" fontWeight="500" fill="#e7eaf2">Bear researcher</text>
          <text x="190" y="200" textAnchor="middle" fontSize="10" fill="#98a0b3">Short thesis advocate · deep reasoning</text>
          <circle className="ae-pulse" cx="360" cy="20" r="4" fill="#6c7388" />
        </g>

        <g className={cls("trader")} onClick={click("trader")} transform="translate(1050, 14)">
          <rect className="ae-bg" width="280" height="220" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <g transform="translate(40, 60)">
            <rect width="200" height="84" rx="4" fill="#0b0d12" stroke="#7c5cff" />
            <text x="100" y="14" textAnchor="middle" fontSize="9" fontWeight="600" fill="#c4b5fd" letterSpacing="0.06em">ORDER TICKET</text>
            <line x1="6" y1="20" x2="194" y2="20" stroke="#7c5cff" strokeWidth="0.5" strokeDasharray="2,2" />
            <g fontSize="8" fill="#cbd5e1" fontFamily="ui-monospace, monospace">
              <text x="10" y="32">Symbol</text> <text x="190" y="32" textAnchor="end" fontWeight="600" fill="#e7eaf2">CRDO</text>
              <text x="10" y="44">Side</text>   <text x="190" y="44" textAnchor="end" fill="#34d399" fontWeight="600">BUY 1.2% NAV</text>
              <text x="10" y="56">Entry</text>  <text x="190" y="56" textAnchor="end">$84 – $86</text>
              <text x="10" y="68">Stop</text>   <text x="190" y="68" textAnchor="end" fill="#f87171">$76</text>
              <text x="10" y="80">Target</text> <text x="190" y="80" textAnchor="end" fill="#34d399">$102 (R:R 2.5:1)</text>
            </g>
          </g>
          <g transform="translate(116, 122)">
            <rect x="10" y="22" width="42" height="6" rx="2" fill="#1c2230" />
            <path d="M 8,32 Q 8,18 31,18 Q 54,18 54,32 L 54,42 L 8,42 Z" fill="#fbbf24" />
            <circle cx="31" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
          </g>
          <text x="140" y="186" textAnchor="middle" fontSize="13" fontWeight="500" fill="#e7eaf2">Trader</text>
          <text x="140" y="200" textAnchor="middle" fontSize="10" fill="#98a0b3">Sizing · entry · stop · target</text>
          <circle className="ae-pulse" cx="260" cy="20" r="4" fill="#6c7388" />
        </g>
      </g>

      {/* RISK + PM */}
      <g transform="translate(40, 770)">
        <text x="0" y="0" fontSize="11" fontWeight="600" fill="#6c7388" letterSpacing="0.08em">RISK COMMITTEE + PORTFOLIO MANAGER · DEEP REASONING</text>

        <g className={cls("risk")} onClick={click("risk")} transform="translate(0, 14)">
          <rect className="ae-bg" width="800" height="200" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <ellipse cx="400" cy="110" rx="180" ry="50" fill="#1c2230" />
          <ellipse cx="400" cy="105" rx="180" ry="50" fill="#232a3a" stroke="#384155" />
          <g transform="translate(180, 80)">
            <path d="M 0,32 Q 0,18 24,18 Q 48,18 48,32 L 48,46 L 0,46 Z" fill="#fb7185" />
            <circle cx="24" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
            <text x="24" y="58" textAnchor="middle" fontSize="9" fontWeight="600" fill="#e7eaf2">Aggressive</text>
            <g transform="translate(-50, -42)">
              <rect width="120" height="30" rx="6" fill="#11141b" stroke="#fb7185" />
              <path d="M 56,30 L 60,38 L 50,30 Z" fill="#11141b" stroke="#fb7185" />
              <text x="8" y="13" fontSize="8" fill="#fda4af">"Push to 1.5%, alignment is</text>
              <text x="8" y="24" fontSize="8" fill="#fda4af">strong across all axes."</text>
            </g>
          </g>
          <g transform="translate(376, 60)">
            <path d="M 0,32 Q 0,18 24,18 Q 48,18 48,32 L 48,46 L 0,46 Z" fill="#a78bfa" />
            <circle cx="24" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
            <text x="24" y="58" textAnchor="middle" fontSize="9" fontWeight="600" fill="#e7eaf2">Neutral</text>
            <g transform="translate(-50, -42)">
              <rect width="148" height="30" rx="6" fill="#11141b" stroke="#a78bfa" />
              <path d="M 70,30 L 74,38 L 64,30 Z" fill="#11141b" stroke="#a78bfa" />
              <text x="8" y="13" fontSize="8" fill="#c4b5fd">"1.0%. Theme overlap with LITE</text>
              <text x="8" y="24" fontSize="8" fill="#c4b5fd">already in the book — trim."</text>
            </g>
          </g>
          <g transform="translate(572, 80)">
            <path d="M 0,32 Q 0,18 24,18 Q 48,18 48,32 L 48,46 L 0,46 Z" fill="#38bdf8" />
            <circle cx="24" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
            <text x="24" y="58" textAnchor="middle" fontSize="9" fontWeight="600" fill="#e7eaf2">Conservative</text>
            <g transform="translate(-50, -42)">
              <rect width="124" height="30" rx="6" fill="#11141b" stroke="#38bdf8" />
              <path d="M 56,30 L 60,38 L 50,30 Z" fill="#11141b" stroke="#38bdf8" />
              <text x="8" y="13" fontSize="8" fill="#7dd3fc">"Liquidity tail at $76. Tighten</text>
              <text x="8" y="24" fontSize="8" fill="#7dd3fc">stop, size to 0.8% max."</text>
            </g>
          </g>
          <text x="400" y="186" textAnchor="middle" fontSize="11" fill="#98a0b3">Risk debate · three voices argue position size before the PM signs</text>
          <circle className="ae-pulse" cx="780" cy="20" r="4" fill="#6c7388" />
        </g>

        <g className={cls("pm")} onClick={click("pm")} transform="translate(820, 14)">
          <rect className="ae-bg" width="500" height="200" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <rect x="60" y="120" width="380" height="40" rx="3" fill="#0b0d12" />
          <rect x="64" y="124" width="372" height="32" rx="2" fill="#1c2230" />
          <g transform="translate(60, 30)">
            <rect width="200" height="80" rx="6" fill="#0b0d12" stroke="#1c2230" />
            <text x="10" y="14" fontSize="8" fill="#a3e635" letterSpacing="0.05em">PORTFOLIO · NAV</text>
            <text x="10" y="34" fontSize="20" fontWeight="500" fill="#a3e635" fontFamily="ui-monospace, monospace">$1.04M</text>
            <text x="10" y="48" fontSize="8" fill="#86efac">+18.4% YTD</text>
            <text x="10" y="62" fontSize="7" fill="#6c7388">Theme exposure: 12.6% optical</text>
            <text x="10" y="74" fontSize="7" fill="#6c7388">Cash: $312K · Margin: 0%</text>
          </g>
          <g transform="translate(280, 40)">
            <rect width="160" height="70" rx="6" fill="#11141b" stroke="#34d399" strokeWidth="1.5" />
            <text x="80" y="22" textAnchor="middle" fontSize="11" fontWeight="600" fill="#34d399">APPROVED</text>
            <text x="80" y="38" textAnchor="middle" fontSize="9" fill="#6ee7b7">CRDO long · 1.0% NAV</text>
            <text x="80" y="52" textAnchor="middle" fontSize="8" fill="#86efac">stop $76 · target $102</text>
            <text x="80" y="64" textAnchor="middle" fontSize="7" fill="#a7f3d0">— Portfolio Manager</text>
          </g>
          <g transform="translate(220, 88)">
            <path d="M 0,32 Q 0,18 24,18 Q 48,18 48,32 L 48,42 L 0,42 Z" fill="#7c5cff" />
            <circle cx="24" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
            <path d="M 22,18 L 26,18 L 28,32 L 24,38 L 20,32 Z" fill="#0b0d12" />
          </g>
          <text x="250" y="186" textAnchor="middle" fontSize="13" fontWeight="500" fill="#e7eaf2">Portfolio manager</text>
          <text x="250" y="200" textAnchor="middle" fontSize="10" fill="#98a0b3">Final approve · size · book</text>
          <circle className="ae-pulse" cx="480" cy="20" r="4" fill="#6c7388" />
        </g>
      </g>

      {/* OUTPUT */}
      <g transform="translate(40, 1010)">
        <text x="0" y="0" fontSize="11" fontWeight="600" fill="#6c7388" letterSpacing="0.08em">OUTPUT · RANKED CANDIDATES + EXECUTION</text>

        <g className={cls("ranker")} onClick={click("ranker")} transform="translate(0, 14)">
          <rect className="ae-bg" width="800" height="150" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <rect x="120" y="14" width="660" height="124" rx="5" fill="#0b0d12" stroke="#232a3a" />
          <text x="138" y="32" fontSize="11" fontWeight="600" fill="#e7eaf2">"BEST POSITIONED" — Optical 800G/1.6T</text>
          <text x="138" y="46" fontSize="8" fill="#98a0b3">Composite weights: 40% MTF · 30% options · 30% thesis fit</text>
          <g fontFamily="ui-monospace, monospace" fontSize="11">
            <g transform="translate(138, 60)">
              <text x="0" y="0" fill="#e7eaf2" fontWeight="500">1.</text>
              <text x="20" y="0" fill="#e7eaf2" fontWeight="500">CRDO</text>
              <rect x="80" y="-9" width="380" height="11" rx="3" fill="#1c2230" />
              <rect x="80" y="-9" width="350" height="11" rx="3" fill="url(#ae-grad-brand)" className="ae-rank-bar" />
              <text x="466" y="0" fill="#c4b5fd" fontWeight="500">9.2</text>
              <text x="510" y="0" fontSize="8" fill="#98a0b3">MTF 9.1 · OPT 9.4 · FIT 9.0</text>
            </g>
            <g transform="translate(138, 78)">
              <text x="0" y="0" fill="#e7eaf2" fontWeight="500">2.</text>
              <text x="20" y="0" fill="#e7eaf2" fontWeight="500">LITE</text>
              <rect x="80" y="-9" width="380" height="11" rx="3" fill="#1c2230" />
              <rect x="80" y="-9" width="330" height="11" rx="3" fill="url(#ae-grad-brand)" className="ae-rank-bar" />
              <text x="466" y="0" fill="#c4b5fd" fontWeight="500">8.7</text>
              <text x="510" y="0" fontSize="8" fill="#98a0b3">MTF 8.6 · OPT 9.0 · FIT 8.7</text>
            </g>
            <g transform="translate(138, 96)">
              <text x="0" y="0" fill="#e7eaf2" fontWeight="500">3.</text>
              <text x="20" y="0" fill="#e7eaf2" fontWeight="500">COHR</text>
              <rect x="80" y="-9" width="380" height="11" rx="3" fill="#1c2230" />
              <rect x="80" y="-9" width="308" height="11" rx="3" fill="url(#ae-grad-brand)" className="ae-rank-bar" />
              <text x="466" y="0" fill="#c4b5fd" fontWeight="500">8.1</text>
              <text x="510" y="0" fontSize="8" fill="#98a0b3">MTF 7.9 · OPT 8.4 · FIT 8.2</text>
            </g>
            <g transform="translate(138, 114)">
              <text x="0" y="0" fill="#98a0b3">4.</text>
              <text x="20" y="0" fill="#98a0b3">FN</text>
              <rect x="80" y="-9" width="380" height="11" rx="3" fill="#1c2230" />
              <rect x="80" y="-9" width="280" height="11" rx="3" fill="#a78bfa" className="ae-rank-bar" />
              <text x="466" y="0" fill="#a78bfa">7.4</text>
              <text x="510" y="0" fontSize="8" fill="#6c7388">MTF 7.0 · OPT 7.6 · FIT 7.7</text>
            </g>
            <g transform="translate(138, 132)">
              <text x="0" y="0" fill="#6c7388">5.</text>
              <text x="20" y="0" fill="#6c7388">AAOI</text>
              <rect x="80" y="-9" width="380" height="11" rx="3" fill="#1c2230" />
              <rect x="80" y="-9" width="234" height="11" rx="3" fill="#384155" className="ae-rank-bar" />
              <text x="466" y="0" fill="#6c7388">6.2</text>
              <text x="510" y="0" fontSize="8" fill="#6c7388">underweight — execution risk</text>
            </g>
          </g>
          <g transform="translate(40, 50)">
            <path d="M 0,32 Q 0,18 24,18 Q 48,18 48,32 L 48,42 L 0,42 Z" fill="#34d399" />
            <circle cx="24" cy="10" r="9" fill="#fef3c7" stroke="#0b0d12" strokeWidth="0.5" />
            <rect x="46" y="12" width="14" height="3" rx="0.5" fill="#0b0d12" transform="rotate(-25 53 13)" />
            <text x="24" y="62" textAnchor="middle" fontSize="9" fill="#e7eaf2">Theme ranker</text>
          </g>
          <circle className="ae-pulse" cx="780" cy="20" r="4" fill="#6c7388" />
        </g>

        <g className={cls("broker")} onClick={click("broker")} transform="translate(820, 14)">
          <rect className="ae-bg" width="500" height="150" rx="10" fill="#161a23" stroke="#232a3a" filter="url(#ae-shadow)" />
          <g transform="translate(60, 14)">
            <rect width="180" height="124" rx="4" fill="#0b0d12" stroke="#232a3a" />
            <rect x="6" y="6" width="168" height="112" rx="3" fill="#11141b" />
            <rect x="22" y="20" width="136" height="60" rx="2" fill="#1c2230" opacity="0.6" />
            <text x="90" y="44" textAnchor="middle" fontSize="11" fontWeight="600" fill="#e7eaf2">PAPER</text>
            <text x="90" y="60" textAnchor="middle" fontSize="9" fontWeight="500" fill="#98a0b3">TRADING ONLY</text>
            <rect x="158" y="68" width="12" height="4" rx="2" fill="#fbbf24" />
            <rect x="36" y="92" width="108" height="20" rx="3" fill="#f87171" />
            <text x="90" y="106" textAnchor="middle" fontSize="11" fontWeight="600" fill="#0b0d12">BROKER</text>
            <circle cx="22" cy="100" r="4" fill="#34d399" className="ae-blink" />
            <text x="22" y="120" textAnchor="middle" fontSize="6" fill="#6c7388">READY</text>
          </g>
          <g transform="translate(260, 30)">
            <rect width="220" height="100" rx="4" fill="#0b0d12" stroke="#232a3a" />
            <text x="10" y="14" fontSize="9" fontWeight="600" fill="#e7eaf2" letterSpacing="0.04em">EXECUTION AUDIT LOG</text>
            <line x1="6" y1="20" x2="214" y2="20" stroke="#232a3a" strokeDasharray="2,2" />
            <g fontSize="7.5" fill="#cbd5e1" fontFamily="ui-monospace, monospace">
              <text x="10" y="32">12:14:08  TradeIntent received</text>
              <text x="10" y="44">12:14:08  whatIfOrder previewed ✓</text>
              <text x="10" y="56" fill="#34d399">12:14:08  Order placed: BUY CRDO 100</text>
              <text x="10" y="68">12:14:09  Broker order_id 8821, perm 4419</text>
              <text x="10" y="80">12:14:09  Paper-prefix verified</text>
              <text x="10" y="92" fill="#f87171" fontWeight="600">      Live mode → BLOCKED</text>
            </g>
          </g>
          <text x="250" y="186" textAnchor="middle" fontSize="13" fontWeight="500" fill="#e7eaf2">Brokerage · paper-trade gateway</text>
          <text x="250" y="200" textAnchor="middle" fontSize="10" fill="#98a0b3">Hard-gated: env=paper AND paper account prefix</text>
          <circle className="ae-pulse" cx="480" cy="20" r="4" fill="#6c7388" />
        </g>
      </g>
    </svg>
  );
}
