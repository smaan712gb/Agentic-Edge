import "./globals.css";
import type { Metadata } from "next";
import Link from "next/link";
import { Activity, Home, Layers, LineChart, Sparkles } from "lucide-react";
import { KillSwitch } from "@/components/KillSwitch";

export const metadata: Metadata = {
  title: "Agentic Edge",
  description: "A team of AI analysts ranks the strongest names in every theme you care about.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="min-h-screen flex">
          <aside className="w-60 shrink-0 border-r border-[var(--color-border)] bg-[var(--color-bg-soft)] p-5 flex flex-col">
            <Link href="/" className="flex items-center gap-2 mb-8">
              <span className="grid place-items-center h-9 w-9 rounded-xl bg-gradient-to-br from-[var(--color-accent)] to-[var(--color-accent-2)] shadow-lg shadow-[var(--color-accent)]/20">
                <Sparkles className="h-5 w-5 text-white" />
              </span>
              <div>
                <div className="font-semibold tracking-tight">Agentic Edge</div>
                <div className="text-xs text-[var(--color-fg-dim)]">Multi-agent research</div>
              </div>
            </Link>
            <nav className="flex flex-col gap-1 text-sm">
              <NavLink href="/" icon={<Home className="h-4 w-4" />}>Home</NavLink>
              <NavLink href="/performance" icon={<LineChart className="h-4 w-4" />}>Performance</NavLink>
              <NavLink href="/themes" icon={<Layers className="h-4 w-4" />}>Themes</NavLink>
              <NavLink href="/runs" icon={<Activity className="h-4 w-4" />}>Runs</NavLink>
            </nav>
            <div className="mt-auto pt-6 border-t border-[var(--color-border)]">
              <KillSwitch />
              <div className="text-xs text-[var(--color-fg-dim)] mt-3">
                Paper trading · v0.1
              </div>
            </div>
          </aside>
          <main className="flex-1 p-8 overflow-y-auto scrollbar-thin">
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}

function NavLink({
  href, icon, children,
}: { href: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <Link
      href={href}
      className="group flex items-center gap-2 px-3 py-2 rounded-lg text-[var(--color-fg-muted)] hover:text-[var(--color-fg)] hover:bg-[var(--color-panel)] transition-colors"
    >
      <span className="text-[var(--color-fg-dim)] group-hover:text-[var(--color-accent)]">{icon}</span>
      {children}
    </Link>
  );
}
