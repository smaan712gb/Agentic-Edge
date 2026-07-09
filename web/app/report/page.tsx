"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle, ArrowDownRight, ArrowUpRight, Building2, FileText,
  RefreshCw, ShieldCheck, Sun, Wallet,
} from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Stat } from "@/components/Stat";
import {
  api, type MorningReport, type ReportBrief, type ReportHolding, type ReportIdea,
} from "@/lib/api";
import { cn, fmtDateShort, fmtMoney, fmtMoneyDelta } from "@/lib/utils";

// The report reads persisted signals only (no live broker calls), so a slow
// poll keeps it current across the morning sweeps without any load concern.
const REFRESH_MS = 120_000;

const BAND_STYLE: Record<string, string> = {
  hold:       "border-[var(--color-up)]/30 text-[var(--color-up)]",
  trim_light: "border-yellow-500/40 text-yellow-500",
  trim_heavy: "border-orange-500/40 text-orange-500",
  aggressive: "border-[var(--color-down)]/40 text-[var(--color-down)]",
};

const BAND_LABEL: Record<string, string> = {
  hold: "hold",
  trim_light: "trim light",
  trim_heavy: "trim heavy",
  aggressive: "exit",
};

const CHANGE_TONE: Record<string, string> = {
  new: "text-[var(--color-up)]",
  add: "text-[var(--color-up)]",
  increase: "text-[var(--color-up)]",
  initial: "text-[var(--color-up)]",
  trim: "text-[var(--color-down)]",
  reduce: "text-[var(--color-down)]",
  exit: "text-[var(--color-down)]",
  hold: "text-[var(--color-fg-muted)]",
};

function actionTone(action: string): string {
  if (action.startsWith("Exit")) return "border-[var(--color-down)]/40 text-[var(--color-down)]";
  if (action.startsWith("Trim")) return "border-orange-500/40 text-orange-500";
  if (action.includes("rotation")) return "border-yellow-500/40 text-yellow-500";
  return "border-[var(--color-up)]/30 text-[var(--color-up)]";
}

function isErr<T>(v: T[] | { error: string } | undefined | null): v is { error: string } {
  return !!v && !Array.isArray(v);
}

export default function ReportPage() {
  const [report, setReport] = useState<MorningReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await api.report.morning();
        if (!alive) return;
        setReport(r);
        setError(null);
        setLastUpdate(new Date());
      } catch (e) {
        if (alive) setError(String(e));
      }
    };
    tick();
    const t = setInterval(tick, REFRESH_MS);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const ideas: ReportIdea[] = !report || isErr(report.top_ideas) ? [] : report.top_ideas;
  const holdings: ReportHolding[] = !report || isErr(report.holdings) ? [] : report.holdings;
  const activity = report && !("error" in (report.institutional_activity as any))
    ? (report.institutional_activity as Exclude<MorningReport["institutional_activity"], { error: string }>)
    : null;
  const alerts = report && !("error" in (report.alerts as any))
    ? (report.alerts as Exclude<MorningReport["alerts"], { error: string }>)
    : null;
  const themeHealth = !report || isErr(report.theme_health) ? [] : report.theme_health;

  const nonHoldActions = holdings.filter((h) => !h.suggested_action.startsWith("Hold"));

  return (
    <div>
      <PageHeader
        eyebrow="Daily brief"
        title="Morning Report"
        subtitle="Everything the overnight sweeps found, in one read: top ideas, what to do with each holding, fresh institutional activity, and anything that needs attention."
        actions={
          <span className="chip text-[10px] text-[var(--color-fg-muted)]">
            <RefreshCw className="h-3 w-3" />
            {report ? fmtDateShort(report.as_of) : "…"}
            {lastUpdate ? ` · updated ${lastUpdate.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}
          </span>
        }
      />

      {error && (
        <div className="glass p-4 mb-4 text-sm text-[var(--color-down)] border-[var(--color-down)]/30">
          {error}
        </div>
      )}
      {!report && !error && <div className="text-[var(--color-fg-muted)]">Assembling the brief…</div>}

      {report && (
        <>
          {/* Attention banner */}
          {alerts && (alerts.entry_breaker.tripped || alerts.rotation_flagged.length > 0 || alerts.holding_stake_reductions.length > 0) ? (
            <div className="glass p-4 mb-6 border-[var(--color-down)]/40">
              <div className="flex items-center gap-2 mb-2">
                <AlertTriangle className="h-4 w-4 text-[var(--color-down)]" />
                <span className="font-semibold text-sm">Needs attention</span>
              </div>
              <ul className="text-sm space-y-1 text-[var(--color-fg-muted)]">
                {alerts.entry_breaker.tripped && (
                  <li>
                    Entry breaker tripped — new entries halted
                    {alerts.entry_breaker.reason ? ` (${alerts.entry_breaker.reason})` : ""}. Positions are managed on their own signals.
                  </li>
                )}
                {alerts.rotation_flagged.map((r) => (
                  <li key={r.theme_id}>
                    Rotation flagged on <span className="text-[var(--color-fg)]">{r.theme_id}</span> — {r.signals_tripped.length} leading signals; no adds, exits tightened.
                  </li>
                ))}
                {alerts.holding_stake_reductions.map((r, i) => (
                  <li key={i}>
                    Institutional stake {r.change_type} on <span className="text-[var(--color-fg)]">{r.ticker}</span>
                    {r.prior_percent != null && r.percent_of_class != null &&
                      ` (${r.prior_percent.toFixed(1)}% → ${r.percent_of_class.toFixed(1)}%)`} — a name in the book.
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="glass p-4 mb-6 flex items-center gap-2 text-sm">
              <ShieldCheck className="h-4 w-4 text-[var(--color-up)]" />
              <span className="text-[var(--color-fg-muted)]">
                Nothing needs attention — no breaker, no rotation flags, no adverse stake filings on held names.
              </span>
            </div>
          )}

          {/* Plain-words brief + risk-appetite gauge */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
            <div className="lg:col-span-2 glass p-6">
              <div className="label-eyebrow mb-3">Today in plain words</div>
              <BriefText brief={report.brief} />
            </div>
            <PostureGauge posture={report.posture} />
          </div>

          {/* Account tiles */}
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
            <Stat
              label="Account value"
              value={report.account ? fmtMoney(report.account.equity) : "—"}
              sub={report.account ? `as of ${fmtDateShort(report.account.as_of)}` : "no snapshot yet"}
              icon={<Wallet className="h-4 w-4" />}
            />
            <Stat
              label="Cash"
              value={report.account?.cash != null ? fmtMoney(report.account.cash) : "—"}
              sub={report.account?.cash_pct != null ? `${report.account.cash_pct}% of account` : undefined}
            />
            <Stat
              label="Buy ideas"
              value={ideas.length}
              sub={ideas.length ? `top: ${ideas[0].symbol} ${ideas[0].composite.toFixed(1)}` : "none above the bar"}
              icon={<Sun className="h-4 w-4" />}
            />
            <Stat
              label="Suggested actions"
              value={nonHoldActions.length}
              sub={nonHoldActions.length
                ? nonHoldActions.slice(0, 3).map((h) => h.symbol).join(", ")
                : "all holdings: hold"}
              tone={nonHoldActions.length ? "down" : "up"}
            />
          </div>

          {/* Top buy ideas */}
          <SectionTitle icon={<ArrowUpRight className="h-4 w-4 text-[var(--color-accent)]" />} title="Top buy ideas"
            hint="Buy decisions on the latest analysis run per theme, ranked by composite — with setup, street coverage, and the institutional read" />
          {ideas.length === 0 && (
            <div className="glass p-5 mb-8 text-sm text-[var(--color-fg-muted)]">
              No Buy decisions in the latest runs — the next scheduled run refreshes this list.
            </div>
          )}
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 mb-8">
            {ideas.map((i) => <IdeaCard key={i.symbol} idea={i} />)}
          </div>

          {/* Portfolio risk engine */}
          {report.portfolio_risk && !("error" in report.portfolio_risk) && (
            <RiskPanel risk={report.portfolio_risk} />
          )}

          {/* Holdings */}
          <SectionTitle icon={<Building2 className="h-4 w-4 text-[var(--color-accent)]" />} title="Portfolio review"
            hint="Latest exit pressure, scorecard, and smart-money read per managed holding" />
          <div className="glass overflow-hidden mb-8">
            <table className="w-full text-sm">
              <thead className="text-[var(--color-fg-dim)]">
                <tr className="text-left border-b border-[var(--color-border)]">
                  <th className="font-medium px-5 py-3">Symbol</th>
                  <th className="font-medium px-5 py-3">Structure</th>
                  <th className="font-medium px-5 py-3 text-right">Exit pressure</th>
                  <th className="font-medium px-5 py-3 text-right">Score</th>
                  <th className="font-medium px-5 py-3 text-right">Conviction</th>
                  <th className="font-medium px-5 py-3">Smart money</th>
                  <th className="font-medium px-5 py-3">Suggested</th>
                </tr>
              </thead>
              <tbody>
                {holdings.length === 0 && (
                  <tr><td colSpan={7} className="px-5 py-4 text-[var(--color-fg-muted)]">
                    No managed positions.
                  </td></tr>
                )}
                {holdings.map((h) => {
                  const band = h.exit_pressure?.band ?? null;
                  return (
                    <tr key={h.symbol} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-panel-2)]/40">
                      <td className="px-5 py-3">
                        <div className="font-medium">{h.symbol}</div>
                        <div className="text-[10px] text-[var(--color-fg-dim)]">{h.themes.slice(0, 2).join(" · ")}</div>
                      </td>
                      <td className="px-5 py-3 text-[var(--color-fg-muted)] text-xs uppercase tracking-wide">
                        {h.structure === "stock" ? "stock" : "LEAPS"}
                        {h.pnl != null && (
                          <span className={cn("ml-2 normal-case tabular-nums", h.pnl >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]")}>
                            {fmtMoneyDelta(h.pnl)}
                          </span>
                        )}
                      </td>
                      <td className="px-5 py-3 text-right">
                        {band ? (
                          <span className={cn("chip text-[10px]", BAND_STYLE[band])} title={h.exit_pressure?.rationale ?? undefined}>
                            {h.exit_pressure?.score != null ? `${Math.round(h.exit_pressure.score)} · ` : ""}{BAND_LABEL[band]}
                          </span>
                        ) : (
                          <span className="text-[var(--color-fg-dim)] text-xs">—</span>
                        )}
                      </td>
                      <td className="px-5 py-3 text-right tabular-nums">
                        {h.latest_score ? h.latest_score.composite.toFixed(1) : "—"}
                        {h.latest_score && (
                          <span className="text-[10px] text-[var(--color-fg-dim)] ml-1">{h.latest_score.decision}</span>
                        )}
                      </td>
                      <td className="px-5 py-3 text-right tabular-nums">
                        {h.latest_score?.conviction != null ? `${h.latest_score.conviction}/5` : "—"}
                      </td>
                      <td className="px-5 py-3">
                        {h.smart_money ? (
                          <span className={cn("chip text-[10px]",
                            h.smart_money.confirmation ? "border-[var(--color-up)]/30 text-[var(--color-up)]" : "text-[var(--color-fg-muted)]")}>
                            {h.smart_money.manager_count}× held{h.smart_money.confirmation ? " · confirm" : ""}
                          </span>
                        ) : (
                          <span className="text-[var(--color-fg-dim)] text-xs">—</span>
                        )}
                      </td>
                      <td className="px-5 py-3">
                        <span className={cn("chip text-[10px]", actionTone(h.suggested_action))}>{h.suggested_action}</span>
                        {h.exit_pressure?.rationale && (
                          <div className="text-[10px] text-[var(--color-fg-dim)] mt-1 max-w-[220px]">
                            {h.exit_pressure.rationale}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {(report.off_universe_positions?.length ?? 0) > 0 && (
            <div className="text-xs text-[var(--color-fg-dim)] -mt-6 mb-8 px-1">
              {report.off_universe_positions.length} account position{report.off_universe_positions.length === 1 ? "" : "s"} outside
              the theme universe not reviewed (managed elsewhere):{" "}
              {report.off_universe_positions.map((p) => p.symbol).join(", ")}
            </div>
          )}

          {/* Institutional activity + theme health, side by side */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
            <div>
              <SectionTitle icon={<FileText className="h-4 w-4 text-[var(--color-accent)]" />} title="New institutional activity"
                hint="Fresh stake filings, fund moves, insider and material-filing alerts" />
              <div className="glass divide-y divide-[var(--color-border)]">
                {activity && activity.stake_filings.length === 0 && activity.fund_moves.length === 0 &&
                  activity.insider_alerts.length === 0 && activity.filing_alerts.length === 0 && (
                  <div className="p-5 text-sm text-[var(--color-fg-muted)]">Quiet — nothing new in the last 48 hours.</div>
                )}
                {activity?.stake_filings.map((f, i) => (
                  <ActivityRow key={`s${i}`}
                    symbol={f.ticker ?? "?"}
                    label={`${f.is_activist ? "Activist stake" : "Stake"} ${f.change_type}`}
                    tone={CHANGE_TONE[f.change_type]}
                    detail={[
                      f.percent_of_class != null ? `${f.percent_of_class.toFixed(1)}% of class` : null,
                      f.prior_percent != null ? `was ${f.prior_percent.toFixed(1)}%` : null,
                      f.is_holding ? "in the book" : null,
                    ].filter(Boolean).join(" · ")}
                  />
                ))}
                {activity?.fund_moves.map((m, i) => (
                  <ActivityRow key={`m${i}`}
                    symbol={m.ticker ?? m.issuer_name ?? "?"}
                    label={`${m.manager.split("—")[0].trim()} · ${m.change_type}`}
                    tone={CHANGE_TONE[m.change_type]}
                    detail={m.change_pct != null ? `${(m.change_pct * 100).toFixed(0)}%` : ""}
                  />
                ))}
                {activity?.insider_alerts.map((a, i) => (
                  <div key={`i${i}`} className="p-4 flex items-start gap-3">
                    <span className="font-medium text-sm w-14 shrink-0">{a.symbol ?? "?"}</span>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm text-[var(--color-fg)]">
                        {a.headline}
                        {a.owner_type ? <span className="text-[var(--color-fg-dim)]"> · {a.owner_type}</span> : null}
                      </div>
                      <div className="text-xs text-[var(--color-fg-muted)] mt-0.5">{a.impact_note}</div>
                    </div>
                    <ImpactChip impact={a.impact} />
                  </div>
                ))}
                {activity?.filing_alerts.map((a, i) => (
                  <div key={`f${i}`} className="p-4 flex items-start gap-3">
                    <span className="font-medium text-sm w-14 shrink-0">{a.symbol ?? "?"}</span>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm text-[var(--color-fg)]">
                        {a.form_type ?? "Filing"} — {a.meaning}
                        {a.is_held && <span className="chip text-[10px] ml-2 border-yellow-500/40 text-yellow-500">we own this</span>}
                      </div>
                      {a.rationale && (
                        <div className="text-xs text-[var(--color-fg-muted)] mt-0.5">{a.rationale}</div>
                      )}
                      <div className="text-xs text-[var(--color-fg-dim)] mt-0.5">
                        {a.impact_note}
                        {a.link && (
                          <>
                            {" · "}
                            <a href={a.link} target="_blank" rel="noreferrer"
                              className="underline hover:text-[var(--color-fg)]">read the filing</a>
                          </>
                        )}
                      </div>
                    </div>
                    <ImpactChip impact={a.impact} />
                  </div>
                ))}
              </div>
            </div>

            <div>
              <SectionTitle icon={<ArrowDownRight className="h-4 w-4 text-[var(--color-accent)]" />} title="Theme health"
                hint="Composite of each theme's latest run — deteriorating themes pressure their holdings' exits" />
              <div className="glass divide-y divide-[var(--color-border)]">
                {themeHealth.length === 0 && (
                  <div className="p-5 text-sm text-[var(--color-fg-muted)]">No completed runs in the last 7 days.</div>
                )}
                {themeHealth.map((t) => (
                  <div key={t.theme_id} className="p-4 flex items-center gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium truncate">{t.theme}</div>
                      <div className="text-[10px] text-[var(--color-fg-dim)]">
                        {t.n_buys} buy{t.n_buys === 1 ? "" : "s"} of {t.n_scored} scored
                      </div>
                    </div>
                    {t.rotation_flagged && (
                      <span className="chip text-[10px] border-yellow-500/40 text-yellow-500">rotation</span>
                    )}
                    <div className="w-28 h-1.5 rounded-full bg-[var(--color-panel-2)] overflow-hidden">
                      <div
                        className={cn("h-full rounded-full",
                          t.composite >= 7.5 ? "bg-[var(--color-up)]" :
                          t.composite >= 6.0 ? "bg-[var(--color-accent)]" : "bg-[var(--color-down)]")}
                        style={{ width: `${Math.min(100, Math.max(0, t.composite * 10))}%` }}
                      />
                    </div>
                    <span className="tabular-nums text-sm w-10 text-right">{t.composite.toFixed(1)}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function BriefText({ brief }: { brief: MorningReport["brief"] }) {
  if (!brief || "error" in brief) {
    return <div className="text-sm text-[var(--color-fg-muted)]">The plain-words summary isn't available right now.</div>;
  }
  const lines = (brief as ReportBrief).text.split("\n").filter((l) => l.trim().length > 0);
  return (
    <div className="space-y-1.5">
      {lines.map((l, i) =>
        l.startsWith("## ") ? (
          <div key={i} className="text-xs font-semibold uppercase tracking-wide text-[var(--color-accent)] pt-2 first:pt-0">
            {l.slice(3)}
          </div>
        ) : (
          <p key={i} className="text-sm leading-relaxed text-[var(--color-fg)]">{l}</p>
        )
      )}
    </div>
  );
}

// Semicircular fear/greed dial. Diverging encoding: defensive pole in the
// "down" hue, risk-on pole in the "up" hue, neutral gray at the midpoint.
// The number + label carry the value; color is reinforcement, never alone.
function PostureGauge({ posture }: { posture: MorningReport["posture"] }) {
  if (!posture) return null;
  const { score, label, components } = posture;

  const cx = 100, cy = 92, r = 74;
  const pt = (angle: number, radius = r): [number, number] => {
    const rad = ((180 - angle) * Math.PI) / 180;
    return [cx + radius * Math.cos(rad), cy - radius * Math.sin(rad)];
  };
  const arc = (a0: number, a1: number): string => {
    const [x0, y0] = pt(a0);
    const [x1, y1] = pt(a1);
    return `M ${x0.toFixed(1)} ${y0.toFixed(1)} A ${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`;
  };
  const needleAngle = Math.min(100, Math.max(0, score)) * 1.8;
  const [nx, ny] = pt(needleAngle, r - 12);

  return (
    <div className="glass p-6 flex flex-col items-center">
      <div className="label-eyebrow self-start mb-1">Risk appetite</div>
      <svg viewBox="0 0 200 108" className="w-full max-w-[240px]">
        {/* defensive → neutral → risk-on, with 2-unit gaps between segments */}
        <path d={arc(0, 43)} stroke="var(--color-down)" strokeWidth="10" fill="none" strokeLinecap="round" opacity="0.75" />
        <path d={arc(47, 63)} stroke="#6c7388" strokeWidth="10" fill="none" strokeLinecap="round" opacity="0.6" />
        <path d={arc(67, 180)} stroke="var(--color-up)" strokeWidth="10" fill="none" strokeLinecap="round" opacity="0.75" />
        <line x1={cx} y1={cy} x2={nx.toFixed(1)} y2={ny.toFixed(1)}
          stroke="var(--color-fg)" strokeWidth="2.5" strokeLinecap="round" />
        <circle cx={cx} cy={cy} r="4.5" fill="var(--color-fg)" />
        <text x="12" y="106" fontSize="8" fill="#6c7388">defensive</text>
        <text x="188" y="106" fontSize="8" fill="#6c7388" textAnchor="end">risk-on</text>
      </svg>
      <div className="text-3xl font-semibold tracking-tight tabular-nums -mt-1">{Math.round(score)}</div>
      <div className="text-sm text-[var(--color-fg-muted)] capitalize">{label}</div>
      {posture.breaker_capped && (
        <div className="chip text-[10px] border-[var(--color-down)]/40 text-[var(--color-down)] mt-2">
          capped — safety brake is on
        </div>
      )}
      <div className="w-full mt-4 space-y-1.5 text-[11px] text-[var(--color-fg-dim)]">
        <GaugeComponent label="Theme strength" value={components.theme_health} />
        <GaugeComponent label={`Money staying put (${posture.themes_total - posture.themes_flagged}/${posture.themes_total} areas calm)`} value={components.rotation_calm} />
        <GaugeComponent label="Buy signals breadth" value={components.buy_breadth} />
      </div>
    </div>
  );
}

function GaugeComponent({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center gap-2">
      <span className="flex-1 truncate">{label}</span>
      <div className="w-16 h-1 rounded-full bg-[var(--color-panel-2)] overflow-hidden shrink-0">
        <div className="h-full rounded-full bg-[var(--color-accent)]" style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
      </div>
      <span className="tabular-nums w-7 text-right text-[var(--color-fg-muted)]">{Math.round(value)}</span>
    </div>
  );
}

const READ_STYLE: Record<string, string> = {
  constructive: "border-[var(--color-up)]/40 text-[var(--color-up)]",
  cautious: "border-[var(--color-down)]/40 text-[var(--color-down)]",
  mixed: "border-yellow-500/40 text-yellow-500",
  "limited data": "text-[var(--color-fg-dim)]",
};

function fmtSignedPct(frac: number | null | undefined, digits = 1): string | null {
  if (frac == null) return null;
  const pct = frac * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(digits)}%`;
}

function IdeaCard({ idea: i }: { idea: ReportIdea }) {
  const read = i.institutional_read;
  const tech = i.technical;
  const analyst = i.analyst;
  const research = i.research ?? [];

  const techFacts: string[] = [];
  if (tech) {
    const m20 = fmtSignedPct(tech.momentum_20d); if (m20) techFacts.push(`20d ${m20}`);
    const m60 = fmtSignedPct(tech.momentum_60d); if (m60) techFacts.push(`60d ${m60}`);
    const d50 = fmtSignedPct(tech.dist_50dma); if (d50) techFacts.push(`vs 50DMA ${d50}`);
    if (tech.rvol != null) techFacts.push(`RVOL ${tech.rvol.toFixed(1)}×`);
    if (tech.gamma_sign && tech.gamma_sign !== "neutral")
      techFacts.push(`dealer γ ${tech.gamma_sign === "positive" ? "+" : "−"}`);
    if (tech.flow_imbalance != null && Math.abs(tech.flow_imbalance) >= 0.2)
      techFacts.push(`flow ${tech.flow_imbalance > 0 ? "bullish" : "bearish"}`);
  }

  return (
    <div className="glass p-5 flex flex-col gap-3">
      {/* Header: identity + hero score */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-xl font-semibold tracking-tight">{i.symbol}</span>
            <span className="chip text-[10px] text-[var(--color-fg-muted)]">{i.theme}</span>
            {i.held && <span className="chip text-[10px] text-[var(--color-fg-dim)]">held</span>}
            {i.rotation_flagged && (
              <span className="chip text-[10px] border-yellow-500/40 text-yellow-500">rotation — no adds</span>
            )}
          </div>
          <div className="text-xs text-[var(--color-fg-dim)] mt-1">
            conviction {i.conviction != null ? `${i.conviction}/5` : "—"}
            {i.smart_money && ` · ${i.smart_money.manager_count} tracked fund${i.smart_money.manager_count === 1 ? "" : "s"} hold`}
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className="text-3xl font-semibold tracking-tight tabular-nums">{i.composite.toFixed(1)}</div>
          <div className="text-[10px] text-[var(--color-fg-dim)]">composite / 10</div>
          {i.entry && (
            <div className={cn("chip text-[10px] mt-1.5",
              i.entry.score >= 85 ? "border-[var(--color-up)]/40 text-[var(--color-up)]" :
              i.entry.score >= 70 ? "border-[var(--color-up)]/25 text-[var(--color-up)]" :
              i.entry.score >= 50 ? "border-yellow-500/40 text-yellow-500" :
              "text-[var(--color-fg-muted)]")}>
              Entry {Math.round(i.entry.score)}/100 · {i.entry.label}
            </div>
          )}
        </div>
      </div>

      {/* Entry checklist — the "92/100, reasons" view */}
      {i.entry && i.entry.reasons.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-0.5">
          {i.entry.reasons.map((r, idx) => (
            <div key={idx} className="text-[11px] text-[var(--color-fg-muted)] flex gap-1.5">
              <span className={r.ok ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}>
                {r.ok ? "✔" : "✘"}
              </span>
              <span className="min-w-0">{r.text}</span>
            </div>
          ))}
        </div>
      )}

      {/* Sub-score meters */}
      <div className="grid grid-cols-3 gap-3">
        <Meter label="Setup" value={i.setup} />
        <Meter label="Options" value={i.options} />
        <Meter label="Thesis fit" value={i.thesis_fit} />
      </div>

      {/* Trend — the 8/21/50/100/200 EMA stack */}
      {i.trend && (
        <CardRow label="Trend · EMA stack">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={cn("chip text-[10px]",
              i.trend.score >= 100 ? "border-[var(--color-up)]/40 text-[var(--color-up)]" :
              i.trend.score >= 75 ? "border-[var(--color-up)]/25 text-[var(--color-up)]" :
              i.trend.score >= 50 ? "border-yellow-500/40 text-yellow-500" :
              "border-[var(--color-down)]/40 text-[var(--color-down)]")}>
              {Math.round(i.trend.score)}/100 · {i.trend.label}
            </span>
            <span className="text-xs tabular-nums text-[var(--color-fg-muted)]">
              {i.trend.pairs.map((p) => (
                <span key={p.pair} className="mr-2 whitespace-nowrap">
                  {p.pair}
                  <span className={p.ok ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}> {p.ok ? "✓" : "✗"}</span>
                </span>
              ))}
            </span>
            {!i.trend.price_above_8ema && i.trend.score >= 75 && (
              <span className="text-[10px] text-[var(--color-fg-dim)]">price below 8 EMA — pullback</span>
            )}
          </div>
        </CardRow>
      )}

      {/* Patterns */}
      {(i.patterns?.length ?? 0) > 0 && (
        <CardRow label="Patterns">
          <div className="flex flex-wrap gap-1.5">
            {i.patterns!.map((p) => (
              <span key={p.key} title={p.detail}
                className={cn("chip text-[10px] cursor-help",
                  p.direction === "bullish"
                    ? "border-[var(--color-up)]/30 text-[var(--color-up)]"
                    : "border-[var(--color-down)]/40 text-[var(--color-down)]")}>
                {p.name}{p.confidence === "high" ? " ●" : ""}
              </span>
            ))}
          </div>
        </CardRow>
      )}

      {/* Market structure */}
      {i.structure && (
        <CardRow label="Market structure">
          <div className="text-xs text-[var(--color-fg-muted)] space-y-0.5">
            <div>
              <span className="capitalize font-medium text-[var(--color-fg)]">{i.structure.structure}</span>
              {" — higher highs "}
              <Tick ok={i.structure.higher_highs} />
              {" · higher lows "}
              <Tick ok={i.structure.higher_lows} />
              {" · "}
              <span className={i.structure.distribution_flag ? "text-[var(--color-down)]" : ""}>
                {i.structure.accumulation_days_25} accum / {i.structure.distribution_days_25} dist days
                {i.structure.distribution_flag ? " ⚠ distribution" : ""}
              </span>
            </div>
            {(i.structure.demand_zone || i.structure.supply_zone) && (
              <div className="tabular-nums">
                {i.structure.demand_zone &&
                  `demand $${i.structure.demand_zone.low}–$${i.structure.demand_zone.high} (${(i.structure.demand_zone.dist_pct * 100).toFixed(0)}%)`}
                {i.structure.demand_zone && i.structure.supply_zone && " · "}
                {i.structure.supply_zone &&
                  `supply $${i.structure.supply_zone.low}–$${i.structure.supply_zone.high} (+${(i.structure.supply_zone.dist_pct * 100).toFixed(0)}%)`}
              </div>
            )}
            {i.structure.liquidity_sweep && (
              <div className="text-[var(--color-fg-dim)]">{i.structure.liquidity_sweep.detail}</div>
            )}
          </div>
        </CardRow>
      )}

      {/* Technical setup */}
      {techFacts.length > 0 && (
        <CardRow label="Technical setup">
          <span className="tabular-nums text-[var(--color-fg-muted)]">{techFacts.join(" · ")}</span>
        </CardRow>
      )}

      {/* Street coverage */}
      {analyst && (
        <CardRow label="Street">
          <div className="space-y-1">
            <div className="text-[var(--color-fg-muted)] tabular-nums">
              {analyst.pt_consensus != null ? (
                <>
                  PT {fmtMoney(analyst.pt_consensus)}
                  {analyst.upside_pct != null && (
                    <span className={analyst.upside_pct >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}>
                      {" "}({analyst.upside_pct >= 0 ? "+" : ""}{analyst.upside_pct.toFixed(0)}%)
                    </span>
                  )}
                </>
              ) : "no consensus target"}
              {(analyst.n_up_30d > 0 || analyst.n_down_30d > 0) &&
                ` · ${analyst.n_up_30d}↑ / ${analyst.n_down_30d}↓ in 30d`}
            </div>
            {analyst.grades_30d.slice(0, 2).map((g, idx) => (
              <div key={idx} className="text-xs text-[var(--color-fg-dim)]">
                <span className={g.direction === "up" ? "text-[var(--color-up)]" : g.direction === "down" ? "text-[var(--color-down)]" : ""}>
                  {g.direction === "up" ? "▲" : g.direction === "down" ? "▼" : "•"}
                </span>{" "}
                {g.firm}{g.from && g.to ? `: ${g.from} → ${g.to}` : ""}{g.date ? ` (${fmtDateShort(g.date)})` : ""}
              </div>
            ))}
          </div>
        </CardRow>
      )}

      {/* Published research */}
      {research.length > 0 && (
        <CardRow label="Research">
          <div className="space-y-1.5">
            {research.slice(0, 2).map((r, idx) => (
              <div key={idx} className="flex items-start gap-2">
                {r.sentiment && (
                  <span className={cn("chip text-[10px] shrink-0",
                    r.sentiment === "bullish" ? "border-[var(--color-up)]/30 text-[var(--color-up)]" :
                    r.sentiment === "bearish" ? "border-[var(--color-down)]/30 text-[var(--color-down)]" :
                    "text-[var(--color-fg-muted)]")}>
                    {r.sentiment}
                  </span>
                )}
                <div className="min-w-0">
                  <div className="text-xs text-[var(--color-fg)] truncate" title={r.headline}>{r.headline}</div>
                  {r.summary && <div className="text-[11px] text-[var(--color-fg-dim)] line-clamp-2">{r.summary}</div>}
                </div>
              </div>
            ))}
          </div>
        </CardRow>
      )}

      {/* Institutional read */}
      {read && (
        <CardRow label="Institutional read">
          <div>
            <span className={cn("chip text-[10px]", READ_STYLE[read.label] ?? "")}>{read.label}</span>
            <ul className="mt-1.5 space-y-0.5">
              {read.bull.map((r, idx) => (
                <li key={`b${idx}`} className="text-[11px] text-[var(--color-fg-muted)]">
                  <span className="text-[var(--color-up)]">+</span> {r}
                </li>
              ))}
              {read.bear.map((r, idx) => (
                <li key={`r${idx}`} className="text-[11px] text-[var(--color-fg-muted)]">
                  <span className="text-[var(--color-down)]">−</span> {r}
                </li>
              ))}
              {read.bull.length === 0 && read.bear.length === 0 && (
                <li className="text-[11px] text-[var(--color-fg-dim)]">no institutional signals captured yet</li>
              )}
            </ul>
          </div>
        </CardRow>
      )}

      {/* Drivers / risks */}
      <div className="flex flex-wrap gap-1.5 pt-1 border-t border-[var(--color-border)]">
        {i.drivers.slice(0, 3).map((d) => (
          <span key={d} className="chip text-[10px] border-[var(--color-up)]/30 text-[var(--color-up)]">{d}</span>
        ))}
        {i.risks.slice(0, 2).map((r) => (
          <span key={r} className="chip text-[10px] border-[var(--color-down)]/30 text-[var(--color-down)]">{r}</span>
        ))}
      </div>
    </div>
  );
}

function Meter({ label, value }: { label: string; value: number }) {
  const pct = Math.min(100, Math.max(0, value * 10));
  return (
    <div>
      <div className="flex items-center justify-between text-[10px] text-[var(--color-fg-dim)] mb-1">
        <span>{label}</span>
        <span className="tabular-nums text-[var(--color-fg-muted)]">{value.toFixed(1)}</span>
      </div>
      <div className="h-1.5 rounded-full bg-[var(--color-panel-2)] overflow-hidden">
        <div className="h-full rounded-full bg-[var(--color-accent)]" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function CardRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="text-sm">
      <div className="label-eyebrow mb-1">{label}</div>
      {children}
    </div>
  );
}

function SectionTitle({ icon, title, hint }: { icon: React.ReactNode; title: string; hint?: string }) {
  return (
    <div className="flex items-center gap-2 mb-3">
      {icon}
      <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
      {hint && <span className="text-xs text-[var(--color-fg-dim)]">{hint}</span>}
    </div>
  );
}

function Tick({ ok }: { ok: boolean }) {
  return <span className={ok ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}>{ok ? "✔" : "✘"}</span>;
}

function RiskPanel({ risk }: { risk: Exclude<MorningReport["portfolio_risk"], { error: string }> }) {
  return (
    <div className="mb-8">
      <SectionTitle icon={<ShieldCheck className="h-4 w-4 text-[var(--color-accent)]" />} title="Portfolio risk"
        hint="Health, correlation, real diversification, and where the money is concentrated" />
      <div className="glass p-5">
        {risk.empty ? (
          <div className="text-sm text-[var(--color-fg-muted)]">
            No positions — the account is {risk.cash_pct != null ? `${Math.round(risk.cash_pct)}%` : "fully"} in cash.
            Risk of loss is minimal; so is market exposure.
          </div>
        ) : (
          <>
            <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-4">
              <MiniStat label="Portfolio health"
                value={risk.health_pct != null ? `${risk.health_pct}%` : "—"}
                tone={risk.health_pct != null ? (risk.health_pct >= 70 ? "up" : risk.health_pct >= 55 ? undefined : "down") : undefined} />
              <MiniStat label="Risk" value={risk.risk_label}
                tone={risk.risk_label.startsWith("high") ? "down" : risk.risk_label === "low" ? "up" : undefined} />
              <MiniStat label="Avg correlation"
                value={risk.avg_correlation != null ? `${Math.round(risk.avg_correlation * 100)}%` : "—"}
                tone={risk.avg_correlation != null && risk.avg_correlation >= 0.75 ? "down" : undefined} />
              <MiniStat label="Independent bets"
                value={risk.effective_bets != null ? `${risk.effective_bets} of ${risk.n_holdings}` : "—"} />
              <MiniStat label="Cash" value={risk.cash_pct != null ? `${Math.round(risk.cash_pct)}%` : "—"} />
            </div>
            {risk.theme_exposure.length > 0 && (
              <div className="space-y-1.5">
                {risk.theme_exposure.map((t) => (
                  <div key={t.theme} className="flex items-center gap-2 text-[11px]">
                    <span className="w-52 truncate text-[var(--color-fg-muted)]">{t.theme}</span>
                    <div className="flex-1 h-1.5 rounded-full bg-[var(--color-panel-2)] overflow-hidden">
                      <div className={cn("h-full rounded-full",
                        t.pct >= 50 ? "bg-[var(--color-down)]" : t.pct >= 35 ? "bg-yellow-500" : "bg-[var(--color-accent)]")}
                        style={{ width: `${Math.min(100, t.pct)}%` }} />
                    </div>
                    <span className="tabular-nums w-10 text-right text-[var(--color-fg-muted)]">{t.pct}%</span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function MiniStat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: "up" | "down" }) {
  return (
    <div>
      <div className="label-eyebrow">{label}</div>
      <div className={cn("text-lg font-semibold tracking-tight mt-0.5",
        tone === "up" ? "text-[var(--color-up)]" : tone === "down" ? "text-[var(--color-down)]" : "")}>
        {value}
      </div>
    </div>
  );
}

function ImpactChip({ impact }: { impact: "positive" | "negative" | "neutral" | "unclear" }) {
  const style =
    impact === "positive" ? "border-[var(--color-up)]/40 text-[var(--color-up)]" :
    impact === "negative" ? "border-[var(--color-down)]/40 text-[var(--color-down)]" :
    impact === "neutral" ? "text-[var(--color-fg-muted)]" :
    "text-[var(--color-fg-dim)]";
  return <span className={cn("chip text-[10px] shrink-0", style)}>{impact}</span>;
}

function ActivityRow({ symbol, label, detail, tone }: {
  symbol: string; label: string; detail?: string; tone?: string;
}) {
  return (
    <div className="p-4 flex items-start gap-3">
      <span className="font-medium text-sm w-14 shrink-0">{symbol}</span>
      <div className="min-w-0">
        <div className={cn("text-sm", tone ?? "text-[var(--color-fg)]")}>{label}</div>
        {detail && <div className="text-xs text-[var(--color-fg-muted)] mt-0.5">{detail}</div>}
      </div>
    </div>
  );
}
