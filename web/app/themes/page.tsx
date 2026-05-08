"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Plus, Trash2, X, Play, Sparkles, Target } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { SchedulerBadge } from "@/components/SchedulerBadge";
import { RegimeChip } from "@/components/RegimeChip";
import { api, type Theme } from "@/lib/api";

export default function ThemesPage() {
  const [themes, setThemes] = useState<Theme[] | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();

  const refresh = () => api.themes.list().then(setThemes).catch((e) => setError(String(e)));

  useEffect(() => { refresh(); }, []);

  const onCreate = async (name: string, thesis: string, chokepoint: string) => {
    setError(null);
    try {
      await api.themes.create({ name, thesis, chokepoint });
      setShowNew(false);
      refresh();
    } catch (e) { setError(String(e)); }
  };

  const onDeleteTheme = async (id: string) => {
    if (!confirm("Delete this theme?")) return;
    await api.themes.remove(id);
    refresh();
  };

  const onAddSymbol = async (id: string, sym: string) => {
    if (!sym.trim()) return;
    await api.themes.addSymbol(id, sym);
    refresh();
  };

  const onRemoveSymbol = async (id: string, sym: string) => {
    await api.themes.removeSymbol(id, sym);
    refresh();
  };

  const onRun = async (id: string) => {
    setError(null);
    try {
      const run = await api.runs.start(id);
      router.push(`/runs/${run.id}`);
    } catch (e) { setError(String(e)); }
  };

  return (
    <div>
      <PageHeader
        eyebrow="Investment universe"
        title="Themes"
        subtitle="Each theme is a thesis, a watch list, and the chokepoint these companies are positioned to win. The agent team scores every symbol against this picture."
        actions={
          <div className="flex items-center gap-3">
            <SchedulerBadge />
            <button className="btn btn-primary" onClick={() => setShowNew(true)}>
              <Plus className="h-4 w-4" /> New theme
            </button>
          </div>
        }
      />

      {error && (
        <div className="glass p-4 mb-4 text-sm text-[var(--color-down)] border-[var(--color-down)]/30">
          {error}
        </div>
      )}

      {showNew && <NewThemeForm onCreate={onCreate} onCancel={() => setShowNew(false)} />}

      <div className="flex flex-col gap-4">
        {themes === null && <div className="text-[var(--color-fg-dim)] text-sm">Loading…</div>}
        {themes?.length === 0 && (
          <div className="glass p-8 text-center">
            <Sparkles className="h-8 w-8 mx-auto text-[var(--color-accent)] mb-3" />
            <div className="font-medium">No themes yet</div>
            <div className="text-sm text-[var(--color-fg-muted)] mt-1">
              Create your first theme to start scoring stocks.
            </div>
          </div>
        )}
        {themes?.map((t) => (
          <ThemeCard
            key={t.id}
            theme={t}
            onDelete={() => onDeleteTheme(t.id)}
            onAddSymbol={(s) => onAddSymbol(t.id, s)}
            onRemoveSymbol={(s) => onRemoveSymbol(t.id, s)}
            onRun={() => onRun(t.id)}
          />
        ))}
      </div>
    </div>
  );
}

function NewThemeForm({
  onCreate, onCancel,
}: {
  onCreate: (name: string, thesis: string, chokepoint: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [thesis, setThesis] = useState("");
  const [chokepoint, setChokepoint] = useState("");
  const canSave = name.trim().length > 0 && thesis.trim().length > 0;
  return (
    <div className="glass p-6 mb-6">
      <div className="flex items-start justify-between mb-4">
        <div>
          <div className="label-eyebrow">New theme</div>
          <div className="text-lg font-semibold mt-1">Define a thesis</div>
        </div>
        <button className="btn btn-ghost" onClick={onCancel}><X className="h-4 w-4" /></button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="md:col-span-1">
          <div className="text-xs text-[var(--color-fg-muted)] mb-1.5">Name</div>
          <input
            className="input mb-3"
            placeholder="e.g. AI memory wall"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={80}
          />
          <div className="text-xs text-[var(--color-fg-muted)] mb-1.5">Thesis</div>
          <textarea
            className="input"
            rows={4}
            placeholder="One or two sentences on what you believe."
            value={thesis}
            onChange={(e) => setThesis(e.target.value)}
          />
        </div>
        <div className="md:col-span-1">
          <div className="text-xs text-[var(--color-fg-muted)] mb-1.5">Stocks</div>
          <div className="text-xs text-[var(--color-fg-dim)] mb-2">
            Add tickers after creating the theme.
          </div>
        </div>
        <div className="md:col-span-1">
          <div className="text-xs text-[var(--color-fg-muted)] mb-1.5">Chokepoint summary</div>
          <textarea
            className="input"
            rows={6}
            placeholder="Why these companies are positioned to win — what bottleneck or chokepoint they capture."
            value={chokepoint}
            onChange={(e) => setChokepoint(e.target.value)}
          />
        </div>
      </div>
      <div className="flex justify-end gap-2 pt-4 mt-4 border-t border-[var(--color-border)]">
        <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
        <button
          className="btn btn-primary"
          disabled={!canSave}
          onClick={() => onCreate(name.trim(), thesis.trim(), chokepoint.trim())}
        >
          Create theme
        </button>
      </div>
    </div>
  );
}

function ThemeCard({
  theme, onDelete, onAddSymbol, onRemoveSymbol, onRun,
}: {
  theme: Theme;
  onDelete: () => void;
  onAddSymbol: (s: string) => void;
  onRemoveSymbol: (s: string) => void;
  onRun: () => void;
}) {
  const [sym, setSym] = useState("");
  const submit = () => {
    if (sym.trim()) {
      onAddSymbol(sym.trim().toUpperCase());
      setSym("");
    }
  };
  return (
    <div className="glass overflow-hidden">
      {/* Header strip — name + actions */}
      <div className="flex items-center justify-between gap-4 px-6 py-4 border-b border-[var(--color-border)] bg-gradient-to-r from-[var(--color-panel-2)]/50 to-transparent">
        <div className="flex items-center gap-3 min-w-0 flex-wrap">
          <div className="h-2 w-2 rounded-full bg-gradient-to-br from-[var(--color-accent)] to-[var(--color-accent-2)] shadow-[0_0_8px_var(--color-accent)]" />
          <div className="text-base font-semibold tracking-tight truncate">{theme.name}</div>
          <span className="chip text-[10px]">{theme.symbols.length} stock{theme.symbols.length === 1 ? "" : "s"}</span>
          <RegimeChip themeId={theme.id} />
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            className="btn btn-primary"
            onClick={onRun}
            disabled={theme.symbols.length === 0}
            title={theme.symbols.length === 0 ? "Add at least one stock" : "Run the agent team"}
          >
            <Play className="h-4 w-4" /> Run
          </button>
          <button className="btn btn-danger" onClick={onDelete} title="Delete theme">
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* 3-column body */}
      <div className="grid grid-cols-1 md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-[var(--color-border)]">
        {/* Col 1: Thesis */}
        <section className="p-6">
          <div className="label-eyebrow mb-2">Thesis</div>
          <p className="text-sm text-[var(--color-fg)] leading-relaxed">
            {theme.thesis}
          </p>
        </section>

        {/* Col 2: Stocks */}
        <section className="p-6">
          <div className="label-eyebrow mb-3">Stocks</div>
          <div className="flex flex-wrap gap-1.5 mb-3 min-h-[44px]">
            {theme.symbols.length === 0 ? (
              <span className="text-xs text-[var(--color-fg-dim)]">None yet — add one below.</span>
            ) : (
              theme.symbols.map((s) => (
                <span key={s} className="chip">
                  {s}
                  <button
                    onClick={() => onRemoveSymbol(s)}
                    className="ml-1 text-[var(--color-fg-dim)] hover:text-[var(--color-down)]"
                    title="Remove"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))
            )}
          </div>
          <div className="flex gap-2">
            <input
              className="input"
              placeholder="Add ticker (e.g. NVDA)"
              value={sym}
              onChange={(e) => setSym(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submit()}
              maxLength={10}
            />
            <button className="btn btn-ghost" onClick={submit} title="Add">
              <Plus className="h-4 w-4" />
            </button>
          </div>
        </section>

        {/* Col 3: Chokepoint */}
        <section className="p-6 bg-gradient-to-br from-[var(--color-accent)]/5 to-transparent">
          <div className="flex items-center gap-1.5 mb-2">
            <Target className="h-3.5 w-3.5 text-[var(--color-accent)]" />
            <div className="label-eyebrow">Chokepoint</div>
          </div>
          {theme.chokepoint ? (
            <p className="text-sm text-[var(--color-fg)] leading-relaxed">
              {theme.chokepoint}
            </p>
          ) : (
            <p className="text-xs text-[var(--color-fg-dim)] italic">
              No chokepoint summary yet.
            </p>
          )}
        </section>
      </div>
    </div>
  );
}
