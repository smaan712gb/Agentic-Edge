import { cn } from "@/lib/utils";

export function Stat({
  label, value, sub, tone = "neutral", icon,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: "neutral" | "up" | "down";
  icon?: React.ReactNode;
}) {
  const toneClass =
    tone === "up" ? "text-[var(--color-up)]" :
    tone === "down" ? "text-[var(--color-down)]" :
    "text-[var(--color-fg)]";
  return (
    <div className="glass p-5">
      <div className="flex items-center justify-between">
        <div className="label-eyebrow">{label}</div>
        {icon && <div className="text-[var(--color-fg-dim)]">{icon}</div>}
      </div>
      <div className={cn("text-3xl font-semibold tracking-tight mt-3", toneClass)}>{value}</div>
      {sub && <div className="text-sm text-[var(--color-fg-muted)] mt-1">{sub}</div>}
    </div>
  );
}
