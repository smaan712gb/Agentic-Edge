"use client";

import { useEffect, useState } from "react";
import { OctagonAlert, ShieldCheck, ShieldOff, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Emergency kill switch — visible at all times in the layout sidebar.
 *
 * Reads the operator's admin token from localStorage (key
 * "agentic_edge_admin_token"). On first use, prompts for the token and
 * stores it. The token is required by the FastAPI admin endpoints, so
 * having it client-side here is operationally fine — the kill switch
 * exists *because* the operator is the one who needs to halt.
 *
 * Two states:
 *   armed — autotrade is on; click halts it (with reason prompt)
 *   halted — autotrade is off; click re-arms it (with reason prompt)
 */
export function KillSwitch() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState<string>("");

  useEffect(() => {
    if (typeof window === "undefined") return;
    setToken(window.localStorage.getItem("agentic_edge_admin_token") || "");
  }, []);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    const refresh = async () => {
      try {
        const s = await api.admin.autotradeStatus(token);
        if (alive) { setEnabled(s.effective_enabled); setError(null); }
      } catch (e: any) {
        if (alive) {
          setError(String(e?.message || e).slice(0, 80));
          setEnabled(null);
        }
      }
    };
    refresh();
    const id = setInterval(refresh, 15_000);
    return () => { alive = false; clearInterval(id); };
  }, [token]);

  const promptForToken = (): string | null => {
    const v = window.prompt("Admin token (saved locally for this browser):");
    if (v) {
      window.localStorage.setItem("agentic_edge_admin_token", v);
      setToken(v);
      return v;
    }
    return null;
  };

  const onHalt = async () => {
    let tok = token;
    if (!tok) {
      tok = promptForToken() ?? "";
      if (!tok) return;
    }
    const reason = window.prompt(
      "Reason for emergency halt? (e.g. unusual fill, market event, user pause):",
      "operator emergency halt",
    );
    if (reason === null) return;
    setBusy(true); setError(null);
    try {
      await api.admin.autotradeDisable(tok, { reason, actor: "ui-kill-switch" });
      setEnabled(false);
    } catch (e: any) {
      setError(String(e?.message || e).slice(0, 100));
    } finally {
      setBusy(false);
    }
  };

  const onArm = async () => {
    let tok = token;
    if (!tok) {
      tok = promptForToken() ?? "";
      if (!tok) return;
    }
    const reason = window.prompt("Reason for re-arming?", "operator re-arm");
    if (reason === null) return;
    setBusy(true); setError(null);
    try {
      await api.admin.autotradeEnable(tok, { reason, actor: "ui-kill-switch" });
      setEnabled(true);
    } catch (e: any) {
      setError(String(e?.message || e).slice(0, 100));
    } finally {
      setBusy(false);
    }
  };

  // Not yet loaded
  if (enabled === null && !error) {
    return (
      <div className="text-xs text-[var(--color-fg-dim)] flex items-center gap-2 mt-2">
        {token ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
        {token ? "checking…" : (
          <button onClick={() => promptForToken()} className="underline hover:text-[var(--color-fg)]">
            Set admin token
          </button>
        )}
      </div>
    );
  }

  if (error && !enabled) {
    return (
      <div className="text-xs text-[var(--color-down)] mt-2 max-w-full truncate" title={error}>
        Kill switch: API err
        <button onClick={() => promptForToken()} className="underline ml-2 hover:text-[var(--color-fg)]">
          token?
        </button>
      </div>
    );
  }

  return (
    <div className="mt-2">
      {enabled ? (
        <button
          onClick={onHalt}
          disabled={busy}
          className={cn(
            "w-full flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg text-sm font-semibold",
            "bg-[var(--color-down)]/10 border border-[var(--color-down)]/40 text-[var(--color-down)]",
            "hover:bg-[var(--color-down)]/20 disabled:opacity-60 transition-colors",
          )}
          title="Disable autotrade and halt every loop. Existing positions are NOT closed."
        >
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <OctagonAlert className="h-4 w-4" />}
          {busy ? "Halting…" : "EMERGENCY HALT"}
        </button>
      ) : (
        <button
          onClick={onArm}
          disabled={busy}
          className={cn(
            "w-full flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg text-sm font-medium",
            "bg-[var(--color-up)]/10 border border-[var(--color-up)]/40 text-[var(--color-up)]",
            "hover:bg-[var(--color-up)]/20 disabled:opacity-60 transition-colors",
          )}
          title="Re-arm autotrade. Both env and DB switches must be ON for trades to fire."
        >
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}
          {busy ? "Arming…" : "Re-arm autotrade"}
        </button>
      )}
      <div className={cn(
        "text-[10px] mt-1.5 flex items-center gap-1.5",
        enabled ? "text-[var(--color-up)]" : "text-[var(--color-fg-dim)]",
      )}>
        {enabled ? (
          <>
            <ShieldCheck className="h-3 w-3" /> Auto-trade armed
          </>
        ) : (
          <>
            <ShieldOff className="h-3 w-3" /> Auto-trade halted
          </>
        )}
      </div>
    </div>
  );
}
