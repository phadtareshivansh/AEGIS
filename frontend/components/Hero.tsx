"use client";

import { motion } from "framer-motion";
import { useScenario } from "./ScenarioProvider";

const EASE = [0.22, 1, 0.36, 1] as const;

export default function Hero() {
  const { phase, runScenario } = useScenario();
  const busy = phase === "running";

  async function handleRun() {
    await runScenario();
    document.getElementById("command")?.scrollIntoView({ behavior: "smooth" });
  }

  return (
    <section className="relative flex min-h-screen flex-col justify-center overflow-hidden">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-64 right-0 h-[48rem] w-[48rem] rounded-full"
        style={{
          background:
            "radial-gradient(circle, #e8542a22 0%, transparent 65%)",
        }}
      />
      <div className="mx-auto w-full max-w-[1400px] px-6 md:px-12">
        <motion.p
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: EASE }}
          className="mb-8 font-mono text-xs tracking-[0.35em] text-accent"
        >
          <span className="pr-3 text-muted">[</span>AEGIS / autonomous disaster response
          <span className="pl-3 text-muted">]</span>
        </motion.p>

        <motion.h1
          initial={{ opacity: 0, y: 32 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.1, ease: EASE }}
          className="max-w-6xl font-display text-6xl font-extrabold leading-[0.95] tracking-tight md:text-8xl"
        >
          Disaster response
          <br />
          that runs <span className="text-accent">itself</span>.
        </motion.h1>

        <motion.p
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.25, ease: EASE }}
          className="mt-10 max-w-xl text-lg leading-relaxed text-muted md:text-xl"
        >
          Predict, allocate, negotiate, and brief — an autonomous system that
          turns a flood forecast into an executable plan in under a minute.
        </motion.p>

        <motion.div
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.4, ease: EASE }}
          className="mt-14"
        >
          <button
            onClick={handleRun}
            disabled={busy}
            className="inline-block bg-accent px-10 py-4 font-display text-base font-bold tracking-tight text-background transition-transform duration-200 ease-out hover:-translate-y-0.5 hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy ? "Running…" : "Run a scenario"}
          </button>
        </motion.div>
      </div>
    </section>
  );
}