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

  // The control must NEVER disappear.
  //
  // An emergency halt that is only visible once the operator has
  // authenticated is not an emergency control. Two of the three states here
  // rendered as ~12px dim text — "Set admin token" when localStorage held no
  // token, "Kill switch: API err" when the status read failed — so on
  // 2026-08-19 the sidebar looked like it simply had no kill switch. The
  // token lives in per-origin localStorage, so merely reaching the dashboard
  // on http://127.0.0.1:3001 instead of http://localhost:3001 is enough to
  // lose it, and with it every visible trace of the halt.
  //
  // Now the block always renders, always names the state, and always keeps
  // HALT one click away — the click prompts for the token if it needs one.
  //
  // When the state is UNKNOWN the primary action is HALT, never ARM. Halting
  // is idempotent and is the safe direction to move blind; arming a system
  // whose state you cannot read is the one action that must require knowing
  // that state first.
  const loading = enabled === null && !error && !!token;
  const unknown = enabled === null;
  const showHalt = enabled === true || unknown;

  return (
    <div className="mt-2">
      {showHalt ? (
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

      <div
        className={cn(
          "text-[10px] mt-1.5 flex items-center gap-1.5",
          enabled === true && "text-[var(--color-up)]",
          enabled === false && "text-[var(--color-fg-dim)]",
          unknown && "text-[var(--color-down)]",
        )}
        title={error || undefined}
      >
        {enabled === true && (<><ShieldCheck className="h-3 w-3" /> Auto-trade armed</>)}
        {enabled === false && (<><ShieldOff className="h-3 w-3" /> Auto-trade halted</>)}
        {unknown && (
          loading
            ? (<><Loader2 className="h-3 w-3 animate-spin" /> checking state…</>)
            : (<><OctagonAlert className="h-3 w-3" /> State unknown — {error ? "API error" : "admin token needed"}</>)
        )}
      </div>

      {unknown && !loading && (
        <button
          onClick={() => promptForToken()}
          className="text-[10px] underline mt-1 text-[var(--color-fg-dim)] hover:text-[var(--color-fg)]"
        >
          {token ? "re-enter admin token" : "set admin token"}
        </button>
      )}
    </div>
  );
}
