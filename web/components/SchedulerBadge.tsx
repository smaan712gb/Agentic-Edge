"use client";

import { useEffect, useState } from "react";
import { CalendarClock, CheckCircle2, CircleDashed, CircleX, Pause } from "lucide-react";
import { api, type SchedulerStatus } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Small, low-noise indicator on the Themes page that tells the operator
 * whether the daily auto-run is alive and when it last fired.
 *
 * Refreshes once per minute (the cron only runs once a day, so polling
 * harder buys nothing). Falls back to a generic "scheduler offline"
 * label when the API can't be reached.
 */
export function SchedulerBadge() {
  const [s, setS] = useState<SchedulerStatus | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const v = await api.scheduler.status();
        if (alive) { setS(v); setErr(false); }
      } catch {
        if (alive) setErr(true);
      }
    };
    tick();
    const i = setInterval(tick, 60_000);
    return () => { alive = false; clearInterval(i); };
  }, []);

  if (err) {
    return (
      <span className="chip border-[var(--color-down)]/30 text-[var(--color-down)]">
        <CircleX className="h-3.5 w-3.5" /> Scheduler offline
      </span>
    );
  }

  if (s === null) {
    return (
      <span className="chip">
        <CircleDashed className="h-3.5 w-3.5 animate-spin" /> Loading…
      </span>
    );
  }

  if (!s.enabled) {
    return (
      <span className="chip" title={s.cron ? `Cron: ${s.cron}` : undefined}>
        <Pause className="h-3.5 w-3.5" /> Auto-run paused
      </span>
    );
  }

  const next = fmtNextRun(s.next_run_at);
  const lastTone =
    s.last_status === "ok"      ? "text-[var(--color-up)] border-[var(--color-up)]/30" :
    s.last_status === "partial" ? "text-amber-300 border-amber-400/40" :
    s.last_status === "error"   ? "text-[var(--color-down)] border-[var(--color-down)]/30" :
                                  "";
  const lastIcon =
    s.last_status === "ok"      ? <CheckCircle2 className="h-3.5 w-3.5" /> :
    s.last_status === "partial" ? <CircleDashed className="h-3.5 w-3.5" /> :
    s.last_status === "error"   ? <CircleX className="h-3.5 w-3.5" /> : null;

  return (
    <span
      className={cn("chip", lastTone)}
      title={[
        `Cron: ${s.cron ?? "—"}`,
        s.last_run_at ? `Last run: ${new Date(s.last_run_at).toLocaleString()}` : null,
        s.last_status ? `Last status: ${s.last_status}` : null,
      ].filter(Boolean).join("\n")}
    >
      {lastIcon ?? <CalendarClock className="h-3.5 w-3.5" />}
      Auto-run · next {next}
    </span>
  );
}

function fmtNextRun(iso: string | null): string {
  if (!iso) return "—";
  const next = new Date(iso);
  const now = new Date();
  const sameDay =
    next.getFullYear() === now.getFullYear() &&
    next.getMonth() === now.getMonth() &&
    next.getDate() === now.getDate();
  const time = next.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return sameDay ? `today ${time}` : next.toLocaleDateString([], { weekday: "short" }) + " " + time;
}
