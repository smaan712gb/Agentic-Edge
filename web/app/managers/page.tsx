"use client";

import { useEffect, useState } from "react";
import { Building2, ArrowUpRight, ArrowDownRight, Sparkles } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { api, type Manager, type OverlapRow, type UwFlow, type Mention, type RotationRow } from "@/lib/api";
import { Newspaper, AlertTriangle, ShieldCheck } from "lucide-react";
import { fmtMoney, cn } from "@/lib/utils";

const SIGNAL_LABEL: Record<string, string> = {
  rs_breakdown: "RS breakdown",
  flow_distribution: "bearish flow",
  breadth_deterioration: "weak breadth",
};

const TIER_BADGE: Record<string, { label: string; cls: string }> = {
  tier1: { label: "Tier 1 · conviction", cls: "border-[var(--color-accent)]/40 text-[var(--color-accent)]" },
  tier2: { label: "Tier 2 · confirm", cls: "text-[var(--color-fg-muted)]" },
  activist: { label: "Activist · 13D watch", cls: "border-[var(--color-down)]/40 text-[var(--color-down)]" },
};

const TILT_STYLE: Record<string, string> = {
  bullish: "border-[var(--color-up)]/30 text-[var(--color-up)]",
  bearish: "border-[var(--color-down)]/30 text-[var(--color-down)]",
  neutral: "text-[var(--color-fg-muted)]",
};

function FlowCell({ flow }: { flow?: UwFlow }) {
  if (!flow) return <span className="text-[var(--color-fg-dim)] text-xs">—</span>;
  const tilt = flow.flow_tilt;
  return (
    <span className="flex items-center gap-1.5 text-xs">
      {tilt ? (
        <span className={cn("chip text-[10px]", TILT_STYLE[tilt])}>{tilt}</span>
      ) : (
        <span className="text-[var(--color-fg-dim)]">no flow</span>
      )}
      {flow.gamma_sign && flow.gamma_sign !== "neutral" && (
        <span className="text-[var(--color-fg-dim)]">γ {flow.gamma_sign === "positive" ? "+" : "−"}</span>
      )}
    </span>
  );
}

const CHANGE_TONE: Record<string, string> = {
  new: "text-[var(--color-up)]",
  add: "text-[var(--color-up)]",
  trim: "text-[var(--color-down)]",
  exit: "text-[var(--color-down)]",
  hold: "text-[var(--color-fg-muted)]",
};

export default function ManagersPage() {
  const [managers, setManagers] = useState<Manager[] | null>(null);
  const [overlap, setOverlap] = useState<OverlapRow[] | null>(null);
  const [flow, setFlow] = useState<Record<string, UwFlow>>({});
  const [mentions, setMentions] = useState<Mention[] | null>(null);
  const [rotation, setRotation] = useState<RotationRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.managers.list().then(setManagers).catch((e) => setError(String(e)));
    api.managers.mentions().then(setMentions).catch(() => setMentions([]));
    api.managers.rotation().then(setRotation).catch(() => setRotation([]));
    api.managers.overlap({ minManagers: 2 }).then((rows) => {
      setOverlap(rows);
      // Overlay live flow on the names that resolved to a ticker.
      const tickers = rows.map((r) => r.ticker).filter((t): t is string => !!t);
      if (tickers.length) {
        api.managers.flow(tickers).then(setFlow).catch(() => setFlow({}));
      }
    }).catch(() => setOverlap([]));
  }, []);

  return (
    <div>
      <PageHeader
        eyebrow="Smart-money positioning"
        title="Managers"
        subtitle="Named legendary investors tracked from their SEC EDGAR filings — quarterly 13F holdings, activist stakes, and the names where conviction overlaps. Decision support; nothing here trades."
      />

      {error && (
        <div className="glass p-4 mb-4 text-sm text-[var(--color-down)] border-[var(--color-down)]/30">
          {error}
        </div>
      )}

      {/* Theme rotation panel — the act-now signal */}
      {(() => {
        const flagged = (rotation ?? []).filter((r) => r.flagged);
        const monitored = rotation?.length ?? 0;
        if (rotation === null) return null;
        if (flagged.length === 0) {
          return (
            <div className="glass p-4 mb-6 flex items-center gap-2 text-sm">
              <ShieldCheck className="h-4 w-4 text-[var(--color-up)]" />
              <span className="text-[var(--color-fg-muted)]">
                No theme rotation flagged — {monitored} theme{monitored === 1 ? "" : "s"} monitored. New entries open; exits on normal signals.
              </span>
            </div>
          );
        }
        return (
          <div className="mb-6">
            <div className="flex items-center gap-2 mb-2">
              <AlertTriangle className="h-4 w-4 text-[var(--color-down)]" />
              <h2 className="text-lg font-semibold tracking-tight">Rotation alert</h2>
              <span className="text-xs text-[var(--color-fg-dim)]">
                institutions leaving these sectors — new entries halted, profit taken on winners, exits tightened
              </span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {flagged.map((r) => (
                <div key={r.theme_id} className="glass p-4 border-[var(--color-down)]/40">
                  <div className="flex items-center justify-between">
                    <span className="font-medium">{r.theme_id}</span>
                    <span className="chip text-[10px] border-[var(--color-down)]/40 text-[var(--color-down)]">
                      {r.signals_tripped.length} signals
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-1.5 mt-2">
                    {r.signals_tripped.map((sig) => (
                      <span key={sig} className="chip text-[10px] text-[var(--color-down)]">
                        {SIGNAL_LABEL[sig] ?? sig}
                      </span>
                    ))}
                  </div>
                  {r.evidence?.breadth && (
                    <div className="text-xs text-[var(--color-fg-dim)] mt-2">
                      breadth {Math.round((r.evidence.breadth.fraction ?? 0) * 100)}% below 20d MA
                      {r.evidence?.regime?.mean_momentum_20d_pct != null &&
                        ` · momentum ${r.evidence.regime.mean_momentum_20d_pct.toFixed(1)}%`}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        );
      })()}

      {/* Manager tiles */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
        {managers === null && <div className="text-[var(--color-fg-muted)]">Loading managers…</div>}
        {managers?.length === 0 && (
          <div className="glass p-5 text-sm text-[var(--color-fg-muted)]">
            No tracked investors yet — they appear after the first filing sweep.
          </div>
        )}
        {managers?.map((m) => (
          <div key={m.slug} className="glass p-5">
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-2">
                <span className="grid place-items-center h-9 w-9 rounded-xl bg-[var(--color-panel-2)]">
                  <Building2 className="h-4 w-4 text-[var(--color-accent)]" />
                </span>
                <div>
                  <div className="font-medium tracking-tight">{m.name}</div>
                  <div className="text-xs text-[var(--color-fg-dim)]">
                    {m.macro_only ? "macro context only" : `${m.ciks.length} CIK${m.ciks.length === 1 ? "" : "s"}`}
                    {m.last_filing_at ? ` · last filing ${m.last_filing_at.slice(0, 10)}` : " · no filings yet"}
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-1.5">
                {TIER_BADGE[m.tier] && (
                  <span className={cn("chip text-[10px]", TIER_BADGE[m.tier].cls)}>
                    {TIER_BADGE[m.tier].label}
                  </span>
                )}
                {!m.active && <span className="chip text-[10px] text-[var(--color-fg-dim)]">inactive</span>}
              </div>
            </div>

            {m.primary_themes.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-3">
                {m.primary_themes.map((t) => (
                  <span key={t} className="chip text-[10px] text-[var(--color-fg-muted)]">{t}</span>
                ))}
              </div>
            )}

            <div className="mt-4">
              <div className="label-eyebrow mb-2">Latest 13F moves</div>
              {m.top_changes.length === 0 ? (
                <div className="text-xs text-[var(--color-fg-dim)]">No quarter-over-quarter changes yet.</div>
              ) : (
                <ul className="space-y-1 text-sm">
                  {m.top_changes.map((c) => (
                    <li key={c.cusip} className="flex items-center justify-between">
                      <span className="text-[var(--color-fg)]">{c.ticker || c.issuer_name || c.cusip}</span>
                      <span className={cn("text-xs font-medium uppercase tracking-wide", CHANGE_TONE[c.change_type])}>
                        {(c.change_type === "add" || c.change_type === "new") && <ArrowUpRight className="inline h-3 w-3" />}
                        {(c.change_type === "trim" || c.change_type === "exit") && <ArrowDownRight className="inline h-3 w-3" />}
                        {c.change_type}
                        {c.change_pct != null && ` ${(c.change_pct * 100).toFixed(0)}%`}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Cross-fund overlap */}
      <div className="flex items-center gap-2 mb-3">
        <Sparkles className="h-4 w-4 text-[var(--color-accent)]" />
        <h2 className="text-lg font-semibold tracking-tight">Cross-fund overlap</h2>
        <span className="text-xs text-[var(--color-fg-dim)]">names held by 2+ tracked managers</span>
      </div>
      <div className="glass overflow-hidden">
        <table className="w-full text-sm">
          <thead className="text-[var(--color-fg-dim)]">
            <tr className="text-left border-b border-[var(--color-border)]">
              <th className="font-medium px-5 py-3">Name</th>
              <th className="font-medium px-5 py-3">Managers</th>
              <th className="font-medium px-5 py-3">Flow</th>
              <th className="font-medium px-5 py-3 text-right">Aggregate value</th>
              <th className="font-medium px-5 py-3">Held by</th>
            </tr>
          </thead>
          <tbody className="text-[var(--color-fg)]">
            {overlap === null && (
              <tr><td colSpan={4} className="px-5 py-4 text-[var(--color-fg-muted)]">Loading overlap…</td></tr>
            )}
            {overlap?.length === 0 && (
              <tr><td colSpan={5} className="px-5 py-4 text-[var(--color-fg-muted)]">
                No overlapping positions yet — they appear after the first filing sweep.
              </td></tr>
            )}
            {overlap?.map((r) => (
              <tr key={r.cusip} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-panel-2)]/40">
                <td className="px-5 py-3">
                  <span className="font-medium">{r.ticker || r.issuer_name}</span>
                  {r.ticker && <span className="text-[var(--color-fg-dim)] text-xs ml-2">{r.issuer_name}</span>}
                </td>
                <td className="px-5 py-3">
                  {r.confirmation && <span className="chip text-[10px] border-[var(--color-up)]/30 text-[var(--color-up)] mr-2">{r.manager_count}× confirm</span>}
                  {!r.confirmation && r.manager_count}
                </td>
                <td className="px-5 py-3">{r.ticker ? <FlowCell flow={flow[r.ticker]} /> : <span className="text-[var(--color-fg-dim)] text-xs">—</span>}</td>
                <td className="px-5 py-3 text-right tabular-nums">{fmtMoney(r.aggregate_value_usd)}</td>
                <td className="px-5 py-3 text-[var(--color-fg-muted)] text-xs">
                  {r.managers.map((mm) => mm.name.split("—")[0].trim()).join(", ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Chokepoint news (Phase 3) */}
      <div className="flex items-center gap-2 mt-8 mb-3">
        <Newspaper className="h-4 w-4 text-[var(--color-accent)]" />
        <h2 className="text-lg font-semibold tracking-tight">Chokepoint news</h2>
        <span className="text-xs text-[var(--color-fg-dim)]">supply-chain headlines on your names · live news feed</span>
      </div>
      <div className="glass divide-y divide-[var(--color-border)]">
        {mentions === null && <div className="p-5 text-[var(--color-fg-muted)]">Loading…</div>}
        {mentions?.length === 0 && (
          <div className="p-5 text-sm text-[var(--color-fg-muted)]">
            No chokepoint headlines yet — the news sweep runs hourly during market hours.
          </div>
        )}
        {mentions?.map((m, i) => (
          <div key={i} className="p-4 flex items-start gap-3">
            <span className="font-medium text-sm w-14 shrink-0">{m.ticker}</span>
            <div className="min-w-0">
              <div className="text-sm text-[var(--color-fg)]">{m.headline}</div>
              {m.summary && <div className="text-xs text-[var(--color-fg-muted)] mt-1">{m.summary}</div>}
              <div className="flex flex-wrap items-center gap-1.5 mt-2">
                {m.sentiment && (
                  <span className={cn("chip text-[10px]", TILT_STYLE[m.sentiment] ?? "")}>{m.sentiment}</span>
                )}
                {m.chokepoint_hits.slice(0, 4).map((h) => (
                  <span key={h} className="chip text-[10px] text-[var(--color-fg-muted)]">{h}</span>
                ))}
                {m.published_at && (
                  <span className="text-[10px] text-[var(--color-fg-dim)] ml-1">
                    {new Date(m.published_at).toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                  </span>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
