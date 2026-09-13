"use client";

import { motion } from "framer-motion";
import { useScenario } from "./ScenarioProvider";
import type { SimulationVisual } from "./ScenarioProvider";

const EASE = [0.22, 1, 0.36, 1] as const;

function svgDataUri(svg: string): string {
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

function Visual({ simulation }: { simulation: SimulationVisual }) {
  if (simulation.source === "image") {
    return (
      <img
        src={simulation.imageUrl}
        alt="AI-generated flood simulation"
        className="h-full w-full object-cover"
      />
    );
  }
  return (
    <img
      src={svgDataUri(simulation.svg)}
      alt="Animated flood-risk overlay"
      className="h-full w-full object-contain"
    />
  );
}

function Caption({ simulation }: { simulation: SimulationVisual }) {
  if (simulation.source === "image") {
    return (
      <span className="font-mono text-[10px] tracking-wider text-muted">
        image · {simulation.model ?? "huggingface"}
      </span>
    );
  }
  return (
    <span className="font-mono text-[10px] tracking-wider text-muted">
      svg fallback · animated overlay
    </span>
  );
}

export default function SimulationPanel() {
  const { simulation, phase, scenarioId } = useScenario();
  if (!simulation) return null;

  return (
    <motion.aside
      data-phase="simulation"
      initial={{ opacity: 0, y: 24 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: EASE }}
      className="lg:sticky lg:top-6 lg:self-start"
    >
      <div className="overflow-hidden rounded-lg border border-white/10 bg-panel">
        <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
          <span className="font-mono text-[11px] tracking-[0.25em] text-accent">
            SIMULATION OVERLAY
          </span>
          <span className="font-mono text-[10px] tracking-wider text-muted">
            {phase === "briefing" ? "complete" : "live"}
          </span>
        </div>
        <div className="aspect-[16/10] overflow-hidden bg-[#0A0A0A]">
          <Visual simulation={simulation} />
        </div>
        <div className="flex items-center justify-between border-t border-white/10 px-4 py-2.5">
          <Caption simulation={simulation} />
          <span className="font-mono text-[10px] tracking-wider text-muted">
            {scenarioId ?? "—"}
          </span>
        </div>
      </div>
    </motion.aside>
  );
}