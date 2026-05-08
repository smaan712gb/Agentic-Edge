"use client";

import { use, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, CheckCircle2, ChevronRight, Sparkles, Trophy } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { AgentDiagram, type AgentStatus } from "@/components/AgentDiagram";
import { PmccBuilder } from "@/components/PmccBuilder";
import { api, type AgentDef, type AgentEdge, type AgentEvent, type Run, type SymbolScore } from "@/lib/api";
import { cn } from "@/lib/utils";

type RouteParams = { id: string };

export default function RunDetailPage({ params }: { params: Promise<RouteParams> }) {
  const { id } = use(params);
  const router = useRouter();

  const [agents, setAgents] = useState<AgentDef[]>([]);
  const [edges, setEdges] = useState<AgentEdge[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const eventsByAgent = useMemo(() => {
    const m: Record<string, AgentEvent[]> = {};
    if (run) for (const e of run.events) (m[e.agent_id] ??= []).push(e);
    return m;
  }, [run]);

  // Status of each agent — computed across the union of all symbols.
  // running = at least one symbol started but not finished; done = all events finished.
  const statuses = useMemo<Record<string, AgentStatus>>(() => {
    const out: Record<string, AgentStatus> = {};
    for (const a of agents) {
      const evs = eventsByAgent[a.id] ?? [];
      if (evs.length === 0) { out[a.id] = "idle"; continue; }
      const startedSymbols = new Set(evs.filter((e) => e.status === "started").map((e) => e.symbol ?? "*"));
      const finishedSymbols = new Set(evs.filter((e) => e.status === "finished").map((e) => e.symbol ?? "*"));
      const isRunning = [...startedSymbols].some((s) => !finishedSymbols.has(s));
      out[a.id] = isRunning ? "running" : "done";
    }
    return out;
  }, [agents, eventsByAgent]);

  // Load agents catalog once
  useEffect(() => {
    api.agents().then((r) => { setAgents(r.agents); setEdges(r.edges); }).catch((e) => setError(String(e)));
  }, []);

  // Stream the run via SSE; fall back to polling if EventSource isn't available
  useEffect(() => {
    let closed = false;
    let es: EventSource | null = null;
    let poll: ReturnType<typeof setInterval> | null = null;

    const apply = (data: any) => {
      setRun((prev) => {
        const base = prev ?? data.run ?? null;
        if (!base) return data.run ?? null;
        if (data.type === "snapshot" || data.type === "done") {
          return { ...base, ...data.run };
        }
        const next: Run = { ...base, events: [...(base.events ?? [])], scores: [...(base.scores ?? [])] };
        if (data.type === "event") {
          next.events.push(data.event);
          if (typeof data.progress === "number") next.progress = data.progress;
        }
        if (data.type === "score") {
          next.scores.push(data.score);
          if (typeof data.progress === "number") next.progress = data.progress;
        }
        return next;
      });
    };

    try {
      es = new EventSource(`/api/runs/${id}/stream`);
      es.onmessage = (e) => {
        if (closed) return;
        try { apply(JSON.parse(e.data)); } catch {}
      };
      es.onerror = () => {
        // Stream may have closed naturally on completion. Fetch once to confirm final state.
        if (!closed) api.runs.get(id).then((r) => setRun(r)).catch(() => {});
      };
    } catch {
      // Polling fallback
      poll = setInterval(() => {
        api.runs.get(id).then(setRun).catch(() => {});
      }, 1500);
    }

    return () => { closed = true; if (es) es.close(); if (poll) clearInterval(poll); };
  }, [id]);

  const selectedAgent = agents.find((a) => a.id === selected) ?? null;
  const selectedEvents = selected ? eventsByAgent[selected] ?? [] : [];

  return (
    <div>
      <div className="mb-6">
        <button onClick={() => router.push("/runs")} className="text-sm text-[var(--color-fg-muted)] hover:text-[var(--color-fg)] inline-flex items-center gap-1">
          <ArrowLeft className="h-3.5 w-3.5" /> All runs
        </button>
      </div>
      <PageHeader
        eyebrow={`Run ${id}`}
        title="Agent Workflow"
        subtitle="A team of specialized agents reviews each symbol in parallel, debates the case, sizes risk, and ranks the strongest names."
        actions={<RunStatus run={run} />}
      />

      {error && (
        <div className="glass p-4 mb-4 text-sm text-[var(--color-down)]">{error}</div>
      )}

      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12 lg:col-span-8">
          {agents.length > 0 && (
            <AgentDiagram
              agents={agents}
              edges={edges}
              statuses={statuses}
              selectedId={selected}
              onSelect={(id) => setSelected((cur) => (cur === id ? null : id))}
            />
          )}
        </div>

        <aside className="col-span-12 lg:col-span-4 glass p-5 min-h-[560px] flex flex-col">
          {selectedAgent ? (
            <AgentInspector agent={selectedAgent} events={selectedEvents} status={statuses[selectedAgent.id] ?? "idle"} />
          ) : (
            <DefaultInspector run={run} />
          )}
        </aside>
      </div>

      {(run?.summary || (run?.scores?.length ?? 0) > 0) && (
        <div className="grid grid-cols-12 gap-4 mt-4">
          <div className="col-span-12 lg:col-span-5">
            <FinalSummary run={run!} />
          </div>
          <div className="col-span-12 lg:col-span-7">
            <Scorecard scores={run?.scores ?? []} bestPositioned={run?.best_positioned ?? []} runId={id} />
          </div>
        </div>
      )}
    </div>
  );
}

function RunStatus({ run }: { run: Run | null }) {
  if (!run) return null;
  const pct = Math.round(run.progress * 100);
  if (run.status === "done") {
    return (
      <span className="chip border-[var(--color-up)]/30 text-[var(--color-up)]">
        <CheckCircle2 className="h-3.5 w-3.5" /> Complete
      </span>
    );
  }
  return (
    <div className="flex items-center gap-3">
      <div className="text-sm text-[var(--color-fg-muted)]">{pct}%</div>
      <div className="w-40 h-1.5 rounded-full bg-[var(--color-panel-2)] overflow-hidden">
        <div
          className="h-full bg-gradient-to-r from-[var(--color-accent)] to-[var(--color-accent-2)] transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function DefaultInspector({ run }: { run: Run | null }) {
  return (
    <div className="flex flex-col h-full">
      <div className="label-eyebrow mb-2">Inspector</div>
      <div className="text-lg font-semibold mb-3">Click any agent</div>
      <p className="text-sm text-[var(--color-fg-muted)]">
        Each agent does a specific job and writes a short report. Select one to read what it does and what it found in this run.
      </p>
      <div className="mt-auto pt-4 border-t border-[var(--color-border)] text-xs text-[var(--color-fg-dim)] space-y-2">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-[var(--color-border)]" /> Idle — has not started
        </div>
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-[var(--color-accent-2)] animate-pulse" /> Running
        </div>
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-[var(--color-up)]" /> Done
        </div>
      </div>
    </div>
  );
}

function AgentInspector({
  agent, events, status,
}: { agent: AgentDef; events: AgentEvent[]; status: AgentStatus }) {
  const finished = events.filter((e) => e.status === "finished" && e.summary);
  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="label-eyebrow mb-2">{agent.role}</div>
      <div className="text-lg font-semibold leading-tight">{agent.name}</div>
      <div className="mt-1">
        <span className={cn(
          "chip text-xs",
          status === "running" && "border-[var(--color-accent-2)]/40 text-[var(--color-accent-2)]",
          status === "done" && "border-[var(--color-up)]/30 text-[var(--color-up)]",
        )}>
          {status === "idle" ? "Waiting" : status === "running" ? "Working…" : "Done"}
        </span>
      </div>
      <p className="text-sm text-[var(--color-fg-muted)] mt-3 leading-relaxed">
        {agent.explanation}
      </p>

      <div className="mt-5 pt-4 border-t border-[var(--color-border)] flex-1 overflow-y-auto scrollbar-thin">
        <div className="label-eyebrow mb-2">Reports ({finished.length})</div>
        {finished.length === 0 ? (
          <div className="text-xs text-[var(--color-fg-dim)]">No reports yet.</div>
        ) : (
          <div className="space-y-3">
            {finished.map((e, i) => (
              <div key={i} className="p-3 rounded-lg bg-[var(--color-bg-soft)] border border-[var(--color-border)]">
                {e.symbol && <div className="text-xs font-mono text-[var(--color-accent-2)] mb-1">{e.symbol}</div>}
                <div className="text-sm text-[var(--color-fg)] leading-snug">{e.summary}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function FinalSummary({ run }: { run: Run }) {
  return (
    <div className="glass p-6 h-full">
      <div className="flex items-center gap-2 mb-3">
        <Sparkles className="h-4 w-4 text-[var(--color-accent)]" />
        <div className="label-eyebrow">Summary</div>
      </div>
      <div className="text-lg font-semibold mb-2">What the team concluded</div>
      <p className="text-sm text-[var(--color-fg)] leading-relaxed">
        {run.summary ?? "The team is still working — final summary will appear here once the run finishes."}
      </p>
      {run.best_positioned.length > 0 && (
        <div className="mt-5 pt-4 border-t border-[var(--color-border)]">
          <div className="flex items-center gap-2 mb-2">
            <Trophy className="h-4 w-4 text-amber-400" />
            <div className="label-eyebrow">Best positioned</div>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {run.best_positioned.map((s, i) => (
              <span key={s} className={cn(
                "chip",
                i === 0 && "border-amber-400/40 text-amber-300",
              )}>
                {i === 0 && "★ "}{s}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Scorecard({ scores, bestPositioned, runId }: { scores: SymbolScore[]; bestPositioned: string[]; runId: string }) {
  if (scores.length === 0) return null;
  const ordered = [...scores].sort((a, b) => b.composite - a.composite);
  return (
    <div className="glass p-6 h-full">
      <div className="label-eyebrow mb-3">Scorecard</div>
      <div className="space-y-3">
        {ordered.map((s) => {
          const isTop = bestPositioned[0] === s.symbol;
          return (
            <div
              key={s.symbol}
              className={cn(
                "p-4 rounded-xl border",
                isTop
                  ? "bg-gradient-to-br from-amber-500/10 to-transparent border-amber-400/30"
                  : "bg-[var(--color-bg-soft)] border-[var(--color-border)]",
              )}
            >
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-3">
                  <div className="font-mono font-semibold text-base">{s.symbol}</div>
                  <DecisionPill decision={s.decision} />
                </div>
                <div className="text-right">
                  <div className="text-2xl font-semibold tracking-tight">{s.composite.toFixed(1)}</div>
                  <div className="text-xs text-[var(--color-fg-dim)]">composite</div>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-2 mt-3 text-xs">
                <SubScore label="Setup" value={s.setup} />
                <SubScore label="Options" value={s.options} />
                <SubScore label="Thesis fit" value={s.thesis_fit} />
              </div>
              <div className="mt-3 text-xs">
                <div className="text-[var(--color-fg-dim)]">Drivers</div>
                <div className="flex flex-wrap gap-1.5 mt-1">
                  {s.drivers.map((d) => (
                    <span key={d} className="chip text-[10px]">
                      <ChevronRight className="h-3 w-3 text-[var(--color-up)]" /> {d}
                    </span>
                  ))}
                </div>
              </div>
              {s.risks.length > 0 && (
                <div className="mt-2 text-xs">
                  <div className="text-[var(--color-fg-dim)]">Risks</div>
                  <div className="flex flex-wrap gap-1.5 mt-1">
                    {s.risks.map((r) => (
                      <span key={r} className="chip text-[10px] border-[var(--color-down)]/30 text-[var(--color-down)]">
                        {r}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {s.decision === "Buy" && (
                <PmccBuilder runId={runId} symbol={s.symbol} />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SubScore({ label, value }: { label: string; value: number }) {
  const pct = Math.max(0, Math.min(100, (value / 10) * 100));
  return (
    <div>
      <div className="flex justify-between text-[var(--color-fg-muted)]">
        <span>{label}</span>
        <span className="text-[var(--color-fg)]">{value.toFixed(1)}</span>
      </div>
      <div className="h-1 mt-1 rounded-full bg-[var(--color-panel-2)] overflow-hidden">
        <div className="h-full bg-gradient-to-r from-[var(--color-accent)] to-[var(--color-accent-2)]" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function DecisionPill({ decision }: { decision: SymbolScore["decision"] }) {
  const cls =
    decision === "Buy" ? "border-[var(--color-up)]/40 text-[var(--color-up)]" :
    decision === "Avoid" ? "border-[var(--color-down)]/40 text-[var(--color-down)]" :
    "border-[var(--color-fg-dim)]/40 text-[var(--color-fg-muted)]";
  return <span className={cn("chip text-[10px]", cls)}>{decision}</span>;
}
