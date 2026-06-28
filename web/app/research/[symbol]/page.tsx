"use client";

import { useEffect, useState } from "react";
import { use } from "react";
import Link from "next/link";
import { ArrowLeft, Dice5, Gauge, Users } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Stat } from "@/components/Stat";
import {
  api, type FeatureSnapshot, type MonteCarloReport, type PersonaScores, type PersonaMeta,
} from "@/lib/api";

export default function SymbolResearchPage({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol: raw } = use(params);
  const symbol = decodeURIComponent(raw).toUpperCase();

  const [snap, setSnap] = useState<FeatureSnapshot | null>(null);
  const [mc, setMc] = useState<MonteCarloReport | null>(null);
  const [personas, setPersonas] = useState<PersonaScores | null>(null);
  const [meta, setMeta] = useState<PersonaMeta[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.research.featuresLatest([symbol]).then((r) => setSnap(r[0] ?? null)).catch((e) => setError(String(e)));
    api.research.personas(symbol).then(setPersonas).catch(() => {});
    api.research.personasMeta().then(setMeta).catch(() => {});
    api.research.monteCarlo(symbol).then(setMc).catch(() => {});
  }, [symbol]);

  return (
    <div>
      <Link href="/research" className="inline-flex items-center gap-1.5 text-sm text-[var(--color-fg-muted)] hover:text-[var(--color-fg)] mb-4">
        <ArrowLeft className="h-4 w-4" /> Research
      </Link>
      <PageHeader
        eyebrow="Symbol deep-dive"
        title={symbol}
        subtitle="Point-in-time features, manager-archetype lenses, and a Monte-Carlo outcome distribution with a suggested (never-executed) size."
      />

      {error && (
        <div className="glass p-4 mb-4 text-sm text-[var(--color-down)] border-[var(--color-down)]/30">{error}</div>
      )}

      {/* Monte-Carlo KPI row */}
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4 mb-6">
        <Stat label="Spot" value={mc?.spot != null ? `$${mc.spot.toFixed(2)}` : "…"}
          sub={mc ? `${(mc.annualised_vol * 100).toFixed(0)}% ann. vol` : "—"} icon={<Gauge className="h-4 w-4" />} />
        <Stat label="MC mean (20d)" value={mc?.terminal?.mean_return != null ? `${(mc.terminal.mean_return * 100).toFixed(1)}%` : "…"}
          tone={(mc?.terminal?.mean_return ?? 0) >= 0 ? "up" : "down"}
          sub={mc?.terminal?.prob_loss != null ? `${(mc.terminal.prob_loss * 100).toFixed(0)}% prob. loss` : "—"} icon={<Dice5 className="h-4 w-4" />} />
        <Stat label="Suggested size" value={mc?.sizing?.suggested != null ? `${(mc.sizing.suggested * 100).toFixed(1)}%` : "…"}
          sub="of NAV · research only" />
        <Stat label="CVaR (5%)" value={mc?.terminal?.cvar05 != null ? `${(mc.terminal.cvar05 * 100).toFixed(1)}%` : "…"}
          tone="down" sub="expected shortfall" />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <PersonasCard personas={personas} meta={meta} />
        <div className="lg:col-span-2">
          <FeaturesCard snap={snap} />
        </div>
      </div>

      {mc?.exit_stress && (
        <div className="glass p-6 mt-6">
          <div className="label-eyebrow mb-3">Exit stress · probability of breaching a drawdown within {mc.horizon_days}d</div>
          <div className="flex flex-wrap gap-3">
            {Object.entries(mc.exit_stress).map(([k, v]) => (
              <div key={k} className="chip">
                {k.replace("pct", "%")}: <span className="ml-1 tabular-nums text-[var(--color-fg)]">{(v * 100).toFixed(0)}%</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function PersonasCard({ personas, meta }: { personas: PersonaScores | null; meta: PersonaMeta[] | null }) {
  const labelFor = (key: string) => meta?.find((m) => m.key === key)?.label ?? key;
  const entries = personas ? Object.entries(personas.archetypes).sort((a, b) => b[1] - a[1]) : [];
  return (
    <div className="glass overflow-hidden">
      <div className="px-6 py-4 border-b border-[var(--color-border)] flex items-center gap-2">
        <Users className="h-4 w-4 text-[var(--color-accent)]" />
        <div className="font-semibold">Manager archetypes</div>
      </div>
      <div className="p-6">
        {!personas ? <div className="text-sm text-[var(--color-fg-dim)]">Loading…</div> : entries.length === 0 ? (
          <div className="text-xs text-[var(--color-fg-dim)]">No persona scores — run a feature snapshot first.</div>
        ) : (
          <div className="flex flex-col gap-3">
            {entries.map(([key, score]) => (
              <div key={key}>
                <div className="flex items-center justify-between text-sm mb-1">
                  <span>{labelFor(key)}</span>
                  <span className="tabular-nums font-medium">{score.toFixed(0)}</span>
                </div>
                <div className="h-1.5 rounded-full bg-[var(--color-panel-2)] overflow-hidden">
                  <div className="h-full rounded-full bg-gradient-to-r from-[var(--color-accent)] to-[var(--color-accent-2)]"
                    style={{ width: `${Math.max(0, Math.min(100, score))}%` }} />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function FeaturesCard({ snap }: { snap: FeatureSnapshot | null }) {
  if (snap === null) {
    return <div className="glass p-6"><div className="text-sm text-[var(--color-fg-dim)]">Loading features…</div></div>;
  }
  const f = snap.features;
  // Split graph/cross-theme vs market/flow vs z-scores for readability.
  const numeric = Object.entries(f).filter(([, v]) => typeof v === "number") as [string, number][];
  const groups: { title: string; keys: (k: string) => boolean }[] = [
    { title: "Graph & cross-theme", keys: (k) => ["theme_count", "theme_centrality", "co_membership_degree", "avg_theme_size", "smartmoney_theme_confirm"].includes(k) },
    { title: "Market & flow", keys: (k) => ["momentum_20d", "momentum_60d", "dist_50dma", "rvol", "flow_imbalance", "gamma_sign_num", "dark_pool_notional"].includes(k) },
    { title: "Cross-sectional Z", keys: (k) => k.startsWith("z_") },
    { title: "Personas", keys: (k) => k.startsWith("persona_") },
  ];
  return (
    <div className="glass overflow-hidden">
      <div className="px-6 py-4 border-b border-[var(--color-border)] flex items-center justify-between">
        <div className="font-semibold">Point-in-time features</div>
        <span className="chip text-[10px]">{snap.as_of?.slice(0, 10)} · {snap.label_status}</span>
      </div>
      <div className="p-6 grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-6">
        {groups.map((g) => {
          const items = numeric.filter(([k]) => g.keys(k));
          if (items.length === 0) return null;
          return (
            <div key={g.title}>
              <div className="label-eyebrow mb-2">{g.title}</div>
              <div className="flex flex-col gap-1">
                {items.map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between text-sm">
                    <span className="text-[var(--color-fg-muted)] font-mono text-xs">{k.replace(/^(z_|persona_)/, "")}</span>
                    <span className="tabular-nums">{v == null ? "—" : Number.isInteger(v) ? v : v.toFixed(3)}</span>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
