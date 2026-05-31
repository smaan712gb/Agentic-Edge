import Link from "next/link";
import { ArrowRight, Cpu, ShieldCheck, Sparkles, Trophy } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import WorkflowDiagram from "@/components/workflow-diagram";

export default function HomePage() {
  return (
    <div>
      <PageHeader
        eyebrow="The platform"
        title="A digital trading floor"
        subtitle="Agentic Edge runs the full hedge-fund workflow — analyst coverage, Bull/Bear debate, risk management, position sizing, portfolio rebalancing, trade execution — scoring entire investment themes by fusing institutional options flow, fundamentals, and dialectical AI reasoning into a ranked 'best positioned to win' list. Targets 3x index outperformance."
        actions={
          <Link href="/themes" className="btn btn-primary">
            Pick a theme <ArrowRight className="h-4 w-4" />
          </Link>
        }
      />

      <WorkflowDiagram />

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mt-8">
        <FeatureCard
          icon={<Sparkles className="h-4 w-4" />}
          title="Theme-first, not ticker-first"
          body="You define the thesis — 'AI memory wall', '800G optical', 'Nuclear power for data centers' — and the team scores every name in the basket on three axes: MTF setup, options sentiment, and thesis fit. The output is a ranked list of which names are best positioned to win."
        />
        <FeatureCard
          icon={<Cpu className="h-4 w-4" />}
          title="Dialectical reasoning"
          body="Bull and Bear go N rounds before the Research Manager picks a side and assigns conviction. A Conviction Gate auto-Holds anything below threshold, so weak signals never reach the risk committee or the trader. The reasoning is auditable end-to-end."
        />
        <FeatureCard
          icon={<ShieldCheck className="h-4 w-4" />}
          title="Paper-only by design"
          body="Approved trades route to the paper-only brokerage through a double-gated wrapper: the broker-mode flag must be paper AND the account ID must carry the paper prefix — both must hold or the call raises. Live trading is intentionally not a path the agents can take."
        />
      </div>

      <div className="glass p-6 mt-8 flex items-start gap-4">
        <div className="grid place-items-center h-10 w-10 rounded-xl bg-gradient-to-br from-[var(--color-accent)] to-[var(--color-accent-2)] shrink-0">
          <Trophy className="h-5 w-5 text-white" />
        </div>
        <div className="flex-1">
          <div className="label-eyebrow mb-1">Today's run</div>
          <div className="text-lg font-semibold tracking-tight">
            Optical networking — CRDO, LITE, COHR top the leaderboard
          </div>
          <p className="text-sm text-[var(--color-fg-muted)] mt-1.5 leading-relaxed">
            The team scored five candidates in the 800G/1.6T transceiver theme. CRDO leads on the
            composite (9.2) — institutional flow, clean MTF, quality fundamentals. AAOI is at the
            bottom on execution risk.
          </p>
        </div>
        <Link href="/runs" className="btn btn-ghost shrink-0 self-center">
          View all runs <ArrowRight className="h-4 w-4" />
        </Link>
      </div>
    </div>
  );
}

function FeatureCard({
  icon,
  title,
  body,
}: {
  icon: React.ReactNode;
  title: string;
  body: string;
}) {
  return (
    <div className="glass p-5">
      <div className="flex items-center gap-2 mb-2">
        <div className="grid place-items-center h-8 w-8 rounded-lg bg-[var(--color-panel-2)] text-[var(--color-accent)]">
          {icon}
        </div>
        <div className="font-semibold tracking-tight">{title}</div>
      </div>
      <p className="text-sm text-[var(--color-fg-muted)] leading-relaxed">{body}</p>
    </div>
  );
}
