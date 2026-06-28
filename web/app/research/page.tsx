"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Network, TrendingUp, Zap, Activity, AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Stat } from "@/components/Stat";
import {
  api, type UniverseGraphNode, type RankReport, type EventStudyReport, type ImpactGraph,
} from "@/lib/api";

export default function ResearchPage() {
  const [graph, setGraph] = useState<UniverseGraphNode[] | null>(null);
  const [rank, setRank] = useState<RankReport | null>(null);
  const [events, setEvents] = useState<EventStudyReport | null>(null);
  const [impact, setImpact] = useState<ImpactGraph | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Each loads independently — graph + ranking are instant (DB/heuristic);
    // event study touches the price feed, so it lands a beat later.
    api.research.universeGraph(200).then(setGraph).catch((e) => setError(String(e)));
    api.research.ranking().then(setRank).catch(() => {});
    api.research.eventStudy().then(setEvents).catch(() => {});
    api.research.impactGraph().then(setImpact).catch(() => {});
  }, []);

  const topRanked = rank?.ranking?.[0];
  const topCentral = graph?.[0];

  return (
    <div>
      <PageHeader
        eyebrow="Quantitative research"
        title="Research"
        subtitle="The universe scored as one supply-chain graph: chokepoint centrality, a pooled cross-sectional ranking, catalyst event-studies, manager archetypes, and shock propagation. Decision-support only — none of this places a trade."
      />

      {error && (
        <div className="glass p-4 mb-4 text-sm text-[var(--color-down)] border-[var(--color-down)]/30">
          {error}
        </div>
      )}

      {/* KPI row */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
        <Stat
          label="Universe"
          value={graph ? graph.length : "…"}
          sub="symbols across all themes"
          icon={<Network className="h-4 w-4" />}
        />
        <Stat
          label="Top ranked"
          value={topRanked ? topRanked.symbol : "…"}
          sub={topRanked ? `${(topRanked.predicted_return * 100).toFixed(1)}% predicted · ${rank?.model}` : "—"}
          tone="up"
          icon={<TrendingUp className="h-4 w-4" />}
        />
        <Stat
          label="Most central"
          value={topCentral ? topCentral.symbol : "…"}
          sub={topCentral ? `${topCentral.theme_count} themes · degree ${topCentral.co_membership_degree}` : "—"}
          icon={<Zap className="h-4 w-4" />}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <RankingCard rank={rank} />
        <CentralityCard graph={graph} />
        <EventStudyCard events={events} />
        <ImpactCard impact={impact} />
      </div>
    </div>
  );
}

function WarnBanner({ warnings }: { warnings?: string[] }) {
  if (!warnings?.length) return null;
  return (
    <div className="flex items-start gap-2 text-xs text-[var(--color-fg-dim)] mb-3">
      <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0 text-[var(--color-accent-2)]" />
      <span>{warnings[0]}</span>
    </div>
  );
}

function SymLink({ symbol }: { symbol: string }) {
  return (
    <Link href={`/research/${symbol}`} className="font-medium hover:text-[var(--color-accent)]">
      {symbol}
    </Link>
  );
}

function RankingCard({ rank }: { rank: RankReport | null }) {
  return (
    <div className="glass overflow-hidden">
      <div className="px-6 py-4 border-b border-[var(--color-border)] flex items-center justify-between">
        <div className="flex items-center gap-2">
          <TrendingUp className="h-4 w-4 text-[var(--color-accent)]" />
          <div className="font-semibold">ML ranking</div>
        </div>
        {rank && (
          <span className="chip text-[10px]">
            {rank.model} · {rank.horizon_days}d · {rank.n_train_rows} train rows
          </span>
        )}
      </div>
      <div className="p-6">
        {!rank ? <Loading /> : (
          <>
            <WarnBanner warnings={rank.warnings} />
            <table className="w-full text-sm">
              <thead>
                <tr className="text-[var(--color-fg-dim)] text-xs border-b border-[var(--color-border)]">
                  <th className="text-left font-normal pb-2">#</th>
                  <th className="text-left font-normal pb-2">Symbol</th>
                  <th className="text-right font-normal pb-2">Predicted</th>
                  <th className="text-right font-normal pb-2">Pctile</th>
                </tr>
              </thead>
              <tbody>
                {rank.ranking.slice(0, 12).map((r) => (
                  <tr key={r.symbol} className="hover:bg-[var(--color-panel-2)]/40">
                    <td className="py-1.5 text-[var(--color-fg-dim)]">{r.rank}</td>
                    <td className="py-1.5"><SymLink symbol={r.symbol} /></td>
                    <td className="py-1.5 text-right tabular-nums">{(r.predicted_return * 100).toFixed(2)}%</td>
                    <td className="py-1.5 text-right tabular-nums text-[var(--color-fg-muted)]">{(r.percentile * 100).toFixed(0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}

function CentralityCard({ graph }: { graph: UniverseGraphNode[] | null }) {
  return (
    <div className="glass overflow-hidden">
      <div className="px-6 py-4 border-b border-[var(--color-border)] flex items-center gap-2">
        <Zap className="h-4 w-4 text-[var(--color-accent)]" />
        <div className="font-semibold">Chokepoint centrality</div>
        <span className="chip text-[10px]">one graph, not silos</span>
      </div>
      <div className="p-6">
        {!graph ? <Loading /> : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[var(--color-fg-dim)] text-xs border-b border-[var(--color-border)]">
                <th className="text-left font-normal pb-2">Symbol</th>
                <th className="text-right font-normal pb-2">Themes</th>
                <th className="text-right font-normal pb-2">Degree</th>
                <th className="text-left font-normal pb-2 pl-3">Sits in</th>
              </tr>
            </thead>
            <tbody>
              {graph.slice(0, 12).map((n) => (
                <tr key={n.symbol} className="hover:bg-[var(--color-panel-2)]/40">
                  <td className="py-1.5"><SymLink symbol={n.symbol} /></td>
                  <td className="py-1.5 text-right tabular-nums">{n.theme_count}</td>
                  <td className="py-1.5 text-right tabular-nums text-[var(--color-fg-muted)]">{n.co_membership_degree}</td>
                  <td className="py-1.5 pl-3 text-xs text-[var(--color-fg-dim)] truncate max-w-[180px]">
                    {n.themes.slice(0, 3).join(", ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function EventStudyCard({ events }: { events: EventStudyReport | null }) {
  const rows = events
    ? Object.entries(events.by_event_type)
        .map(([etype, byW]) => ({ etype, ...byW["20"] }))
        .filter((r) => r.n > 0)
        .sort((a, b) => (b.mean_car ?? 0) - (a.mean_car ?? 0))
    : [];
  return (
    <div className="glass overflow-hidden">
      <div className="px-6 py-4 border-b border-[var(--color-border)] flex items-center gap-2">
        <Activity className="h-4 w-4 text-[var(--color-accent)]" />
        <div className="font-semibold">Event study · CAR@20d</div>
        {events && <span className="chip text-[10px]">{events.n_events} events</span>}
      </div>
      <div className="p-6">
        {!events ? <Loading /> : (
          <>
            <WarnBanner warnings={events.warnings} />
            {rows.length === 0 ? (
              <div className="text-xs text-[var(--color-fg-dim)]">No measurable events yet.</div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-[var(--color-fg-dim)] text-xs border-b border-[var(--color-border)]">
                    <th className="text-left font-normal pb-2">Catalyst</th>
                    <th className="text-right font-normal pb-2">n</th>
                    <th className="text-right font-normal pb-2">Mean CAR</th>
                    <th className="text-right font-normal pb-2">Win rate</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.etype} className="hover:bg-[var(--color-panel-2)]/40">
                      <td className="py-1.5 font-mono text-xs">{r.etype}</td>
                      <td className="py-1.5 text-right tabular-nums text-[var(--color-fg-muted)]">{r.n}</td>
                      <td className={`py-1.5 text-right tabular-nums ${(r.mean_car ?? 0) >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
                        {((r.mean_car ?? 0) * 100).toFixed(2)}%
                      </td>
                      <td className="py-1.5 text-right tabular-nums text-[var(--color-fg-muted)]">{((r.win_rate ?? 0) * 100).toFixed(0)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function ImpactCard({ impact }: { impact: ImpactGraph | null }) {
  const movers = impact ? impact.nodes.filter((n) => Math.abs(n.impact) > 0.001).slice(0, 12) : [];
  return (
    <div className="glass overflow-hidden">
      <div className="px-6 py-4 border-b border-[var(--color-border)] flex items-center gap-2">
        <Network className="h-4 w-4 text-[var(--color-accent)]" />
        <div className="font-semibold">Supply-chain impact</div>
        <span className="chip text-[10px]">news shock → neighbours</span>
      </div>
      <div className="p-6">
        {!impact ? <Loading /> : movers.length === 0 ? (
          <div className="text-xs text-[var(--color-fg-dim)]">
            No active shocks — impact propagates from recent chokepoint news. {impact.seeded_from}.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[var(--color-fg-dim)] text-xs border-b border-[var(--color-border)]">
                <th className="text-left font-normal pb-2">Symbol</th>
                <th className="text-right font-normal pb-2">Impact</th>
                <th className="text-right font-normal pb-2">Direct seed</th>
              </tr>
            </thead>
            <tbody>
              {movers.map((n) => (
                <tr key={n.symbol} className="hover:bg-[var(--color-panel-2)]/40">
                  <td className="py-1.5"><SymLink symbol={n.symbol} /></td>
                  <td className={`py-1.5 text-right tabular-nums ${n.impact >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
                    {n.impact >= 0 ? "+" : ""}{n.impact.toFixed(3)}
                  </td>
                  <td className="py-1.5 text-right tabular-nums text-[var(--color-fg-dim)]">
                    {n.seed !== 0 ? n.seed.toFixed(2) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function Loading() {
  return <div className="text-sm text-[var(--color-fg-dim)]">Loading…</div>;
}
