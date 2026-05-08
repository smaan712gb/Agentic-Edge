"use client";

import { Background, BackgroundVariant, Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { cn } from "@/lib/utils";
import type { AgentDef, AgentEdge } from "@/lib/api";

export type AgentStatus = "idle" | "running" | "done";

const LANE_X: Record<string, number> = {
  analysts: 0,
  debate: 320,
  synthesis: 640,
  risk: 920,
  scoring: 1480,
};

// Per-agent positions chosen so the flow reads left-to-right with even vertical spacing.
const POSITIONS: Record<string, { x: number; y: number }> = {
  market:             { x: LANE_X.analysts,  y:   0 },
  fundamentals:       { x: LANE_X.analysts,  y: 110 },
  news:               { x: LANE_X.analysts,  y: 220 },
  options:            { x: LANE_X.analysts,  y: 330 },
  social:             { x: LANE_X.analysts,  y: 440 },

  bull:               { x: LANE_X.debate,    y: 110 },
  bear:               { x: LANE_X.debate,    y: 330 },

  research_manager:   { x: LANE_X.synthesis, y: 110 },
  trader:             { x: LANE_X.synthesis, y: 330 },

  risk_aggressive:    { x: LANE_X.risk,      y:  60 },
  risk_neutral:       { x: LANE_X.risk,      y: 220 },
  risk_conservative:  { x: LANE_X.risk,      y: 380 },

  portfolio_manager:  { x: 1200,             y: 220 },

  scorecard:          { x: LANE_X.scoring,   y: 140 },
  ranker:             { x: LANE_X.scoring,   y: 300 },
};

type AgentNodeData = {
  agent: AgentDef;
  status: AgentStatus;
  onClick: (id: string) => void;
  selected: boolean;
};

const ROLE_TINT: Record<string, string> = {
  analyst:     "from-sky-500/20 to-sky-500/5 border-sky-400/30",
  researcher:  "from-amber-500/20 to-amber-500/5 border-amber-400/30",
  synthesizer: "from-violet-500/25 to-violet-500/5 border-violet-400/40",
  executor:    "from-fuchsia-500/20 to-fuchsia-500/5 border-fuchsia-400/30",
  risk:        "from-rose-500/20 to-rose-500/5 border-rose-400/30",
  decider:     "from-emerald-500/25 to-emerald-500/5 border-emerald-400/40",
  scorer:      "from-cyan-500/20 to-cyan-500/5 border-cyan-400/30",
  ranker:      "from-violet-600/30 to-violet-500/5 border-violet-400/50",
};

function AgentNode({ data }: NodeProps<Node<AgentNodeData>>) {
  const { agent, status, onClick, selected } = data;
  const tint = ROLE_TINT[agent.role] ?? "from-slate-500/20 to-slate-500/5 border-slate-400/30";
  return (
    <div
      onClick={() => onClick(agent.id)}
      className={cn(
        "relative cursor-pointer w-[200px] rounded-xl border bg-gradient-to-br p-3 transition-all",
        tint,
        status === "running" && "agent-running",
        status === "done" && "ring-1 ring-emerald-400/40",
        selected && "ring-2 ring-[var(--color-accent)]",
      )}
    >
      <Handle type="target" position={Position.Left} style={{ background: "#7c5cff", width: 6, height: 6, border: 0 }} />
      <Handle type="source" position={Position.Right} style={{ background: "#7c5cff", width: 6, height: 6, border: 0 }} />
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="text-xs uppercase tracking-wider text-[var(--color-fg-dim)]">{agent.role}</div>
        <StatusDot status={status} />
      </div>
      <div className="font-medium text-sm text-[var(--color-fg)] leading-tight">{agent.name}</div>
      <div className="text-[11px] text-[var(--color-fg-muted)] mt-1 leading-snug line-clamp-2">{agent.summary}</div>
    </div>
  );
}

function StatusDot({ status }: { status: AgentStatus }) {
  if (status === "running") return <span className="h-2 w-2 rounded-full bg-[var(--color-accent-2)] animate-pulse" />;
  if (status === "done") return <span className="h-2 w-2 rounded-full bg-[var(--color-up)]" />;
  return <span className="h-2 w-2 rounded-full bg-[var(--color-border)]" />;
}

const nodeTypes = { agent: AgentNode };

export function AgentDiagram({
  agents,
  edges,
  statuses,
  selectedId,
  onSelect,
}: {
  agents: AgentDef[];
  edges: AgentEdge[];
  statuses: Record<string, AgentStatus>;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const rfNodes: Node<AgentNodeData>[] = agents.map((a) => ({
    id: a.id,
    type: "agent",
    position: POSITIONS[a.id] ?? { x: 0, y: 0 },
    data: {
      agent: a,
      status: statuses[a.id] ?? "idle",
      onClick: onSelect,
      selected: selectedId === a.id,
    },
    draggable: false,
    selectable: false,
  }));

  const rfEdges: Edge[] = edges
    .filter((e) => e.from !== "start") // virtual start node — don't draw edges from it
    .map((e, i) => {
      const fromActive = statuses[e.from] === "running" || statuses[e.from] === "done";
      const toActive = statuses[e.to] === "running";
      const animated = fromActive && (toActive || statuses[e.to] !== "done");
      return {
        id: `e${i}`,
        source: e.from,
        target: e.to,
        animated: statuses[e.to] === "running" && statuses[e.from] !== "idle",
        style: {
          stroke: animated ? "#7c5cff" : "#2a3145",
          strokeWidth: animated ? 1.6 : 1,
          opacity: statuses[e.from] === "idle" ? 0.45 : 1,
        },
      };
    });

  return (
    <div className="h-[560px] w-full rounded-xl overflow-hidden border border-[var(--color-border)] bg-[var(--color-bg-soft)]">
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag
        zoomOnScroll={false}
        zoomOnPinch
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#232a3a" />
        <Controls showInteractive={false} className="!bg-[var(--color-panel)] !border-[var(--color-border)]" />
      </ReactFlow>
    </div>
  );
}
