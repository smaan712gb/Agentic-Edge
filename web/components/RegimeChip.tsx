"use client";

import { useEffect, useState } from "react";
import { TrendingUp, TrendingDown, ArrowDownRight, Minus, HelpCircle } from "lucide-react";
import { api, type ThemeRegime } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Regime indicator on each theme card.
 *
 * Reads the theme's mapped sector ETFs, classifies the regime based on
 * MTF + UW gamma/flow context, and renders a small chip:
 *
 *   ↑ Uptrend       — long entries unrestricted
 *   ↘ Pullback      — long entries permitted, but tighter sizing recommended
 *   ↔ Range         — long entries permitted, neutral
 *   ↓ Downtrend     — auto-gate blocks NEW long entries (existing positions unaffected)
 *
 * The chip is informational on the UI; the same regime is what the
 * auto-gate consults when approving auto-entries. The hover tooltip
 * shows the per-ETF rationale (e.g., "SMH: above 50/200, 20d momentum
 * 25% — UW: gamma positive, flow bullish").
 */
export function RegimeChip({ themeId }: { themeId: string }) {
  const [r, setR] = useState<ThemeRegime | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api.themes.regime(themeId)
      .then((v) => { if (alive) setR(v); })
      .catch((e) => { if (alive) setErr(String(e)); });
    return () => { alive = false; };
  }, [themeId]);

  if (err) {
    return (
      <span className="chip text-[var(--color-fg-dim)] text-[10px]">
        <HelpCircle className="h-3 w-3" /> regime —
      </span>
    );
  }
  if (r === null) {
    return <span className="chip text-[10px]">…</span>;
  }

  const meta = TONE[r.regime] ?? TONE.unknown;
  const etfList = r.etfs.length ? r.etfs.join(" + ") : "—";
  const tooltip = [
    `Regime: ${r.regime}`,
    `ETFs: ${etfList}`,
    r.rationale,
    "",
    ...r.etfs.map((etf) => {
      const uw = r.uw[etf];
      const vs50 = r.vs_50ma_pct[etf];
      const mom = r.momentum_20d_pct[etf];
      const dd = r.dd_from_52wh_pct[etf];
      const src = r.price_source[etf];
      const lines = [`${etf} (${src})`];
      if (vs50 !== undefined) lines.push(`  vs 50ma: ${vs50 > 0 ? "+" : ""}${vs50}%`);
      if (mom !== undefined) lines.push(`  20d mom: ${mom > 0 ? "+" : ""}${mom}%`);
      if (dd !== undefined) lines.push(`  52w DD: ${dd}%`);
      if (uw?.gamma_sign) lines.push(`  UW gamma: ${uw.gamma_sign}`);
      if (uw?.flow_tilt) lines.push(`  UW flow: ${uw.flow_tilt}`);
      return lines.join("\n");
    }),
  ].filter(Boolean).join("\n");

  return (
    <span
      className={cn("chip text-[10px]", meta.cls)}
      title={tooltip}
    >
      {meta.icon}
      {meta.label} · {etfList}
    </span>
  );
}

const TONE: Record<string, { cls: string; icon: React.ReactNode; label: string }> = {
  uptrend: {
    cls: "border-[var(--color-up)]/40 text-[var(--color-up)]",
    icon: <TrendingUp className="h-3 w-3" />, label: "Uptrend",
  },
  pullback: {
    cls: "border-amber-400/40 text-amber-300",
    icon: <ArrowDownRight className="h-3 w-3" />, label: "Pullback",
  },
  range: {
    cls: "border-[var(--color-fg-dim)]/40 text-[var(--color-fg-muted)]",
    icon: <Minus className="h-3 w-3" />, label: "Range",
  },
  downtrend: {
    cls: "border-[var(--color-down)]/40 text-[var(--color-down)]",
    icon: <TrendingDown className="h-3 w-3" />, label: "Downtrend",
  },
  unknown: {
    cls: "text-[var(--color-fg-dim)]",
    icon: <HelpCircle className="h-3 w-3" />, label: "—",
  },
};
