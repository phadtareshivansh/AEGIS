"use client";

import { motion } from "framer-motion";

const EASE = [0.22, 1, 0.36, 1] as const;

const STEPS = [
  {
    number: "01",
    label: "Predict",
    description: "Score every zone against rainfall, river levels, and terrain within hours, not days.",
  },
  {
    number: "02",
    label: "Allocate",
    description: "Route people, shelters, ambulances, and water tankers against the highest-risk demand.",
  },
  {
    number: "03",
    label: "Negotiate",
    description: "Let opposing priorities argue it out — then lock a defensible resolution.",
  },
  {
    number: "04",
    label: "Brief",
    description: "Hand the operator a 30-second briefing they can act on immediately.",
  },
];

export default function HowItWorks() {
  return (
    <section className="py-32 md:py-48">
      <div className="mx-auto w-full max-w-[1400px] px-6 md:px-12">
        <motion.div
          initial={{ opacity: 0, y: 24 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, amount: 0.4 }}
          transition={{ duration: 0.6, ease: EASE }}
        >
          <p className="mb-6 font-mono text-xs tracking-[0.35em] text-accent">
            HOW IT WORKS
          </p>
          <h2 className="max-w-4xl font-display text-4xl font-extrabold tracking-tight md:text-6xl">
            Four moves.
            <br />
            <span className="text-muted">Zero noise.</span>
          </h2>
        </motion.div>

        <div className="mt-20 grid grid-cols-1 gap-y-16 gap-x-8 md:mt-28 md:grid-cols-4">
          {STEPS.map((step, i) => (
            <motion.div
              key={step.number}
              initial={{ opacity: 0, y: 24 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, amount: 0.4 }}
              transition={{ duration: 0.6, delay: i * 0.1, ease: EASE }}
              className="border-t border-white/10 pt-8"
            >
              <p className="mb-6 font-mono text-sm text-accent-dim">{step.number}</p>
              <h3 className="font-display text-2xl font-extrabold tracking-tight md:text-3xl">
                {step.label}
              </h3>
              <p className="mt-4 max-w-xs text-base leading-relaxed text-muted">
                {step.description}
              </p>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
}