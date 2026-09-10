"use client";

import { useEffect } from "react";
import { motion } from "framer-motion";
import { useScenario } from "./ScenarioProvider";

const EASE = [0.22, 1, 0.36, 1] as const;

export default function BriefingPanel() {
  const { phase, briefing, scenarioId, resetScenario } = useScenario();
  const visible = phase === "briefing" && briefing !== null;

  useEffect(() => {
    if (!visible) return;
    const t = setTimeout(() => {
      document.getElementById("briefing")?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }, 600);
    return () => clearTimeout(t);
  }, [visible]);

  if (!visible || !briefing) return null;

  function handleRunAnother() {
    resetScenario();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  return (
    <motion.section
      id="briefing"
      data-phase="briefing"
      initial={{ opacity: 0, y: 32 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.8, ease: EASE }}
      className="py-40"
    >
      <div className="mx-auto w-full max-w-3xl px-6">
        <motion.p
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.6, delay: 0.2 }}
          className="mb-10 font-mono text-xs tracking-[0.35em] text-muted"
        >
          SCENARIO {scenarioId ?? "—"} · BRIEFING
        </motion.p>

        <motion.h2
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.15, ease: EASE }}
          className="max-w-2xl font-display text-4xl font-extrabold leading-[1.05] tracking-tight md:text-5xl"
        >
          {briefing.headline}
        </motion.h2>

        <motion.p
          initial={{ opacity: 0, y: 18 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.3, ease: EASE }}
          className="mt-8 max-w-2xl text-lg leading-relaxed text-foreground/85"
        >
          {briefing.risk_summary}
        </motion.p>

        <motion.p
          initial={{ opacity: 0, y: 18 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.4, ease: EASE }}
          className="mt-5 max-w-2xl text-lg leading-relaxed text-foreground/85"
        >
          {briefing.resource_plan}
        </motion.p>

        {briefing.conflict_resolution ? (
          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.5, ease: EASE }}
            className="mt-10 max-w-2xl rounded-r-md border-l-2 border-accent bg-accent-dim py-3 pl-5 pr-4"
          >
            <p className="mb-1 font-mono text-[11px] tracking-[0.3em] text-accent">
              CONFLICTS RESOLVED
            </p>
            <p className="text-sm leading-relaxed text-foreground/75">
              {briefing.conflict_resolution}
            </p>
          </motion.div>
        ) : null}

        {briefing.recommended_actions.length > 0 ? (
          <motion.ol
            initial={{ opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.6, ease: EASE }}
            className="mt-12 max-w-2xl"
          >
            {briefing.recommended_actions.map((action, i) => (
              <li
                key={i}
                className="flex gap-5 border-b border-white/5 py-4 first:border-t"
              >
                <span className="font-display text-lg font-extrabold text-accent">
                  {String(i + 1).padStart(2, "0")}
                </span>
                <span className="text-base leading-relaxed text-foreground/85">
                  {action}
                </span>
              </li>
            ))}
          </motion.ol>
        ) : null}

        <motion.div
          initial={{ opacity: 0, y: 18 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.75, ease: EASE }}
        >
          <button
            onClick={handleRunAnother}
            className="mt-14 border border-white/20 px-8 py-4 font-display text-sm font-bold tracking-tight transition-colors duration-200 hover:border-accent hover:bg-accent hover:text-background"
          >
            Run another scenario
          </button>
        </motion.div>
      </div>
    </motion.section>
  );
}