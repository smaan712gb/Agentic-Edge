"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowRight, CircleCheck, CircleDashed, CircleX, Activity } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { api, type Run, type RunListItem, type Theme } from "@/lib/api";

export default function RunsListPage() {
  const [runs, setRuns] = useState<RunListItem[] | null>(null);
  const [themes, setThemes] = useState<Record<string, Theme>>({});

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const [r, ts] = await Promise.all([api.runs.list(), api.themes.list()]);
        if (!alive) return;
        setRuns(r);
        setThemes(Object.fromEntries(ts.map((t) => [t.id, t])));
      } catch (e) { console.error(e); }
    };
    tick();
    const i = setInterval(tick, 3000);
    return () => { alive = false; clearInterval(i); };
  }, []);

  return (
    <div>
      <PageHeader
        eyebrow="Activity"
        title="Runs"
        subtitle="Each run sends every symbol in a theme through the agent team and produces a ranked scorecard."
      />
      <div className="glass overflow-hidden">
        {runs === null && <div className="p-6 text-sm text-[var(--color-fg-dim)]">Loading…</div>}
        {runs?.length === 0 && (
          <div className="p-10 text-center">
            <Activity className="h-8 w-8 mx-auto text-[var(--color-fg-dim)] mb-3" />
            <div className="font-medium">No runs yet</div>
            <div className="text-sm text-[var(--color-fg-muted)] mt-1">
              Open a theme and click <span className="text-[var(--color-fg)]">Run analysis</span>.
            </div>
          </div>
        )}
        {runs && runs.length > 0 && (
          <table className="w-full text-sm">
            <thead className="text-[var(--color-fg-dim)] text-left">
              <tr className="border-b border-[var(--color-border)]">
                <th className="font-medium px-5 py-3">Run</th>
                <th className="font-medium px-5 py-3">Theme</th>
                <th className="font-medium px-5 py-3">Status</th>
                <th className="font-medium px-5 py-3">Started</th>
                <th className="font-medium px-5 py-3">Best positioned</th>
                <th className="px-5 py-3"></th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-panel-2)]/40">
                  <td className="px-5 py-3 font-mono text-xs text-[var(--color-fg-muted)]">{r.id}</td>
                  <td className="px-5 py-3">{themes[r.theme_id]?.name ?? r.theme_id}</td>
                  <td className="px-5 py-3"><StatusPill status={r.status} progress={r.progress} /></td>
                  <td className="px-5 py-3 text-[var(--color-fg-muted)]">
                    {new Date(r.started_at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
                  </td>
                  <td className="px-5 py-3">
                    <div className="flex flex-wrap gap-1">
                      {r.best_positioned.length === 0
                        ? <span className="text-[var(--color-fg-dim)] text-xs">—</span>
                        : r.best_positioned.map((s) => <span key={s} className="chip">{s}</span>)}
                    </div>
                  </td>
                  <td className="px-5 py-3 text-right">
                    <Link href={`/runs/${r.id}`} className="btn btn-ghost">
                      Open <ArrowRight className="h-4 w-4" />
                    </Link>
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

function StatusPill({ status, progress }: { status: Run["status"]; progress: number }) {
  if (status === "running") {
    return (
      <span className="chip border-[var(--color-accent)]/40 text-[var(--color-accent-2)]">
        <CircleDashed className="h-3 w-3 animate-spin" /> Running · {Math.round(progress * 100)}%
      </span>
    );
  }
  if (status === "done") {
    return (
      <span className="chip border-[var(--color-up)]/30 text-[var(--color-up)]">
        <CircleCheck className="h-3 w-3" /> Done
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className="chip border-[var(--color-down)]/30 text-[var(--color-down)]">
        <CircleX className="h-3 w-3" /> Error
      </span>
    );
  }
  return <span className="chip">{status}</span>;
}
