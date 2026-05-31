"use client";

import { useState } from "react";
import { Loader2, Play, X, Layers, AlertTriangle, CheckCircle2 } from "lucide-react";
import { api, type TradeIntent } from "@/lib/api";
import { fmtMoney } from "@/lib/utils";

/**
 * One-click PMCC builder that lives next to each Buy decision in the
 * Run scorecard. Three states:
 *
 *   1. "Build" — initial; click probes the option chain, picks legs, returns intent.
 *   2. Preview — shows the legs, max loss, walking config; user clicks Submit.
 *   3. Submitted — fires the executor; polls for fill or abandon; surfaces result.
 *
 * Errors surface inline (eligibility failure, gate rejection, broker not
 * reachable) so the operator sees the reason without leaving the page.
 */
export function PmccBuilder({
  runId, symbol,
}: {
  runId: string;
  symbol: string;
}) {
  const [intent, setIntent] = useState<TradeIntent | null>(null);
  const [building, setBuilding] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitResult, setSubmitResult] = useState<{ status: string; reason?: string; gate?: string; execution?: any } | null>(null);
  const [contracts, setContracts] = useState(1);

  const onBuild = async () => {
    setError(null);
    setSubmitResult(null);
    setBuilding(true);
    try {
      const i = await api.tradeIntents.buildPmcc(runId, symbol, { contracts });
      setIntent(i);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBuilding(false);
    }
  };

  const onCancel = async () => {
    if (!intent) return;
    setError(null);
    try {
      await api.tradeIntents.cancel(intent.id);
      setIntent(null);
      setSubmitResult(null);
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  };

  const onSubmit = async () => {
    if (!intent) return;
    if (!confirm(
      `Submit PMCC combo to the paper account?\n\n` +
      `${symbol} × ${contracts} spreads\n` +
      `LEAP $${intent.leap.strike} ${intent.leap.expiry}\n` +
      `Short $${intent.short_call.strike} ${intent.short_call.expiry}\n` +
      `Net debit target: $${intent.net_debit_target?.toFixed(2)} per spread\n` +
      `Max loss: ${fmtMoney(intent.max_loss ?? 0)}`
    )) return;

    setError(null);
    setSubmitResult(null);
    setSubmitting(true);
    try {
      const res = await api.tradeIntents.submit(intent.id);
      setSubmitResult(res);
      // Refresh intent state
      const refreshed = await api.tradeIntents.get(intent.id);
      setIntent(refreshed);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setSubmitting(false);
    }
  };

  // ----- Render: initial Build button --------------------------------
  if (!intent) {
    return (
      <div className="flex items-center gap-2 mt-3">
        <input
          type="number" min={1} max={20} value={contracts}
          onChange={(e) => setContracts(Math.max(1, Math.min(20, +e.target.value || 1)))}
          className="input !w-16 !py-1 text-sm"
          disabled={building}
        />
        <button
          className="btn btn-primary"
          disabled={building}
          onClick={onBuild}
          title="Probe the option chain and pick PMCC legs"
        >
          {building ? <Loader2 className="h-4 w-4 animate-spin" /> : <Layers className="h-4 w-4" />}
          {building ? "Probing chain…" : "Build PMCC"}
        </button>
        {error && <span className="chip border-[var(--color-down)]/40 text-[var(--color-down)] text-[10px]"><AlertTriangle className="h-3 w-3" /> {error.slice(0, 80)}</span>}
      </div>
    );
  }

  // ----- Render: preview drawer + submit/cancel ----------------------
  const filled = intent.status === "filled";
  const submitted = ["submitting", "filled", "abandoned", "rejected", "error", "cancelled"].includes(intent.status);

  return (
    <div className="mt-3 p-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-soft)] space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs text-[var(--color-fg-dim)] uppercase tracking-wider">PMCC · {intent.qty} spread{intent.qty === 1 ? "" : "s"}</div>
        <StatusPill status={intent.status} />
      </div>

      <div className="grid grid-cols-2 gap-3 text-xs">
        <LegPanel label="LEAP (long)" leg={intent.leap} />
        <LegPanel label="Short call" leg={intent.short_call} />
      </div>

      <div className="flex items-center justify-between text-xs pt-2 border-t border-[var(--color-border)]">
        <div className="flex gap-4">
          <Stat label="Net debit" value={intent.net_debit_target != null ? `$${intent.net_debit_target.toFixed(2)}` : "—"} />
          <Stat label="Max loss" value={intent.max_loss != null ? fmtMoney(intent.max_loss) : "—"} />
          {intent.net_debit_filled != null && (
            <Stat label="Filled @" value={`$${intent.net_debit_filled.toFixed(2)}`} tone="up" />
          )}
        </div>
        {!submitted && (
          <div className="flex gap-2">
            <button className="btn btn-ghost" onClick={onCancel} disabled={submitting}>
              <X className="h-3.5 w-3.5" /> Cancel
            </button>
            <button className="btn btn-primary" onClick={onSubmit} disabled={submitting}>
              {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {submitting ? "Walking the limit…" : "Submit to broker"}
            </button>
          </div>
        )}
      </div>

      {submitResult && (
        <div className={`text-xs p-2 rounded-lg ${
          submitResult.status === "filled"
            ? "bg-[var(--color-up)]/10 text-[var(--color-up)] border border-[var(--color-up)]/30"
            : submitResult.status === "abandoned" || submitResult.status === "rejected_pretrade"
            ? "bg-amber-400/10 text-amber-300 border border-amber-400/30"
            : submitResult.status === "gate_rejected"
            ? "bg-amber-400/10 text-amber-300 border border-amber-400/30"
            : "bg-[var(--color-down)]/10 text-[var(--color-down)] border border-[var(--color-down)]/30"
        }`}>
          {submitResult.status === "filled" && (
            <span className="flex items-center gap-2">
              <CheckCircle2 className="h-3.5 w-3.5" />
              Filled @ ${submitResult.execution?.fill_price?.toFixed(2)} after {submitResult.execution?.walk_steps} walk steps
              ({submitResult.execution?.elapsed_sec?.toFixed(1)}s)
            </span>
          )}
          {submitResult.status === "abandoned" && (
            <span>Abandoned: {submitResult.execution?.error || "did not fill within budget"}</span>
          )}
          {submitResult.status === "rejected_pretrade" && (
            <span>Pre-trade reject: {submitResult.execution?.error}</span>
          )}
          {submitResult.status === "gate_rejected" && (
            <span>Auto-gate blocked: <strong>{submitResult.gate}</strong> — {submitResult.reason}</span>
          )}
          {!["filled", "abandoned", "rejected_pretrade", "gate_rejected"].includes(submitResult.status) && (
            <span>Status: {submitResult.status} — {submitResult.execution?.error || ""}</span>
          )}
        </div>
      )}

      {error && (
        <div className="text-xs text-[var(--color-down)]">{error}</div>
      )}
    </div>
  );
}

function StatusPill({ status }: { status: TradeIntent["status"] }) {
  const map: Record<string, { cls: string; label: string }> = {
    pending_review: { cls: "border-[var(--color-accent)]/40 text-[var(--color-accent)]", label: "Pending review" },
    submitting:     { cls: "border-amber-400/40 text-amber-300",                          label: "Submitting…" },
    filled:         { cls: "border-[var(--color-up)]/40 text-[var(--color-up)]",          label: "Filled" },
    abandoned:      { cls: "text-[var(--color-fg-dim)]",                                  label: "Abandoned" },
    rejected:       { cls: "border-[var(--color-down)]/40 text-[var(--color-down)]",      label: "Rejected" },
    cancelled:      { cls: "text-[var(--color-fg-dim)]",                                  label: "Cancelled" },
    error:          { cls: "border-[var(--color-down)]/40 text-[var(--color-down)]",      label: "Error" },
  };
  const meta = map[status] ?? { cls: "", label: status };
  return <span className={`chip text-[10px] ${meta.cls}`}>{meta.label}</span>;
}

function LegPanel({ label, leg }: { label: string; leg: TradeIntent["leap"] }) {
  return (
    <div className="rounded-lg bg-[var(--color-panel-2)]/50 p-2.5 space-y-1">
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-fg-dim)]">{label}</div>
      <div className="font-mono text-xs">
        ${leg.strike} · {leg.expiry?.replace(/(\d{4})(\d{2})(\d{2})/, "$1-$2-$3")}
      </div>
      <div className="text-[10px] text-[var(--color-fg-muted)] flex flex-wrap gap-x-3 gap-y-0.5">
        <span>Δ {leg.delta_actual != null ? leg.delta_actual.toFixed(2) : "—"}</span>
        <span>IV {leg.iv != null ? `${(leg.iv * 100).toFixed(0)}%` : "—"}</span>
        <span>OI {leg.open_interest ?? "—"}</span>
        <span>×{leg.qty ?? "?"}</span>
      </div>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  const toneCls = tone === "up" ? "text-[var(--color-up)]" : tone === "down" ? "text-[var(--color-down)]" : "";
  return (
    <span>
      <span className="text-[var(--color-fg-dim)]">{label}: </span>
      <span className={`font-medium ${toneCls}`}>{value}</span>
    </span>
  );
}
