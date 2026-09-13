"use client";

import { useEffect, useRef } from "react";
import { motion } from "framer-motion";
import { useScenario } from "./ScenarioProvider";
import type { DebateEntry, Entry, ScenarioEvent } from "./ScenarioProvider";
import SimulationPanel from "./SimulationPanel";

const EASE = [0.22, 1, 0.36, 1] as const;

function fmtTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

function FeedLine({ event }: { event: ScenarioEvent }) {
  return (
    <motion.p
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.25, ease: "easeOut" }}
      className="mb-2 flex items-baseline gap-2 font-mono text-[13px] leading-relaxed"
    >
      <span className="shrink-0 text-white/25">{fmtTime(event.timestamp)}</span>
      <span className="shrink-0 text-accent">[{event.agent.toUpperCase()}]</span>
      <span className="break-words text-foreground/90">{event.message}</span>
    </motion.p>
  );
}

function ConflictLine({ event }: { event: ScenarioEvent }) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.3, ease: "easeOut" }}
      className="my-3 border-l-2 border-accent bg-accent-dim py-2 pl-3 pr-3"
    >
      <p className="mb-1 font-mono text-[11px] tracking-[0.25em] text-accent">
        ⚠ CONFLICT FLAGGED
      </p>
      <p className="break-words font-mono text-[13px] leading-relaxed text-foreground/90">
        {event.message}
      </p>
    </motion.div>
  );
}

function ErrorLine({ event }: { event: ScenarioEvent }) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.3, ease: "easeOut" }}
      className="my-3 border border-accent/50 bg-accent-dim p-3"
    >
      <p className="mb-1 font-mono text-[11px] tracking-[0.25em] text-accent">
        ⚠ ERROR [{event.agent.toUpperCase()}]
      </p>
      <p className="break-words font-mono text-[13px] leading-relaxed text-foreground/90">
        {event.message}
      </p>
    </motion.div>
  );
}

function TurnBubble({
  turn,
  agent,
  text,
}: {
  turn: number;
  agent: string;
  text: string;
}) {
  const isEvac = agent === "evacuation_advocate";
  return (
    <motion.div
      initial={{ opacity: 0, y: 14, scale: 0.985 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ duration: 0.4, ease: EASE }}
      className="w-full rounded-xl border border-white/10 bg-panel p-4"
    >
      <div className="mb-2 flex items-center gap-2">
        <span
          className={`font-mono text-[11px] tracking-[0.25em] ${
            isEvac ? "text-accent" : "text-foreground"
          }`}
        >
          {isEvac ? "EVACUATION" : "LOGISTICS"}
        </span>
        <span className="font-mono text-[11px] text-muted">· T{turn}</span>
      </div>
      <p className="whitespace-pre-wrap break-words font-mono text-[13px] leading-relaxed text-foreground/90">
        {text}
      </p>
    </motion.div>
  );
}

function ResolutionCallout({ event }: { event: ScenarioEvent }) {
  const data = (event.data ?? {}) as Record<string, unknown>;
  const decision = data.decision ?? event.message;
  const justification = data.justification;
  const side = data.winning_side;
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: EASE }}
      className="mx-auto mt-6 max-w-2xl rounded-lg border border-accent/50 bg-accent-dim px-5 py-4"
    >
      <p className="mb-1.5 font-mono text-[11px] tracking-[0.35em] text-accent">
        RESOLUTION
      </p>
      <p className="font-display text-base font-bold tracking-tight text-foreground">
        {String(decision)}
      </p>
      {justification ? (
        <p className="mt-2 text-sm leading-relaxed text-foreground/70">
          {String(justification)}
        </p>
      ) : null}
      {side ? (
        <p className="mt-3 font-mono text-[11px] tracking-wider text-muted">
          WINNING SIDE · {String(side).toUpperCase()}
        </p>
      ) : null}
    </motion.div>
  );
}

function DebateBlock({ entry }: { entry: DebateEntry }) {
  const { conflictId, resource, turns, resolution } = entry;
  const evacTurns = [1, 3].filter((t) => turns[t]);
  const logiTurns = [2, 4].filter((t) => turns[t]);
  return (
    <motion.div
      data-phase="debate"
      initial={{ opacity: 0, y: 18 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.45, ease: EASE }}
      className="my-4 rounded-lg border border-white/10 bg-white/[0.02] p-5 md:p-6"
    >
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <span className="border border-accent/40 bg-accent-dim px-2 py-1 font-mono text-[11px] tracking-[0.15em] text-accent">
          {conflictId}
        </span>
        {resource ? (
          <span className="font-mono text-[11px] tracking-wider text-muted">
            · {resource}
          </span>
        ) : null}
        <span className="ml-auto font-mono text-[11px] tracking-[0.25em] text-muted">
          LIVE DEBATE
        </span>
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <div className="flex flex-col gap-4 lg:self-start">
          {evacTurns.map((t) => (
            <TurnBubble key={t} turn={t} agent="evacuation_advocate" text={turns[t]!} />
          ))}
        </div>
        <div className="flex flex-col gap-4 lg:self-start">
          {logiTurns.map((t) => (
            <TurnBubble key={t} turn={t} agent="logistics_advocate" text={turns[t]!} />
          ))}
        </div>
      </div>

      {resolution ? <ResolutionCallout event={resolution} /> : null}
    </motion.div>
  );
}

function FeedItem({ entry }: { entry: Entry }) {
  if (entry.kind === "debate") return <DebateBlock entry={entry} />;
  const { event } = entry;
  if (event.type === "conflict_flagged") return <ConflictLine event={event} />;
  if (event.type === "error") return <ErrorLine event={event} />;
  return <FeedLine event={event} />;
}

export default function CommandCenter() {
  const { phase, connection, entries, scenarioId } = useScenario();
  const feedRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  function onFeedScroll() {
    const el = feedRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 64;
  }

  useEffect(() => {
    const el = feedRef.current;
    if (!el || !pinnedRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [entries]);

  const running = phase === "running";

  return (
    <section id="command" className="py-32 md:py-48">
      <div className="mx-auto w-full max-w-[1400px] px-6 md:px-12">
        <motion.div
          initial={{ opacity: 0, y: 24 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, amount: 0.4 }}
          transition={{ duration: 0.6, ease: EASE }}
        >
          <p className="mb-6 font-mono text-xs tracking-[0.35em] text-accent">
            LIVE COMMAND CENTER
          </p>
          <h2 className="max-w-4xl font-display text-4xl font-extrabold tracking-tight md:text-6xl">
            Watch the response
            <br />
            <span className="text-muted">assemble itself.</span>
          </h2>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 32 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, amount: 0.2 }}
          transition={{ duration: 0.7, delay: 0.1, ease: EASE }}
          className="mt-16 overflow-hidden rounded-lg border border-white/10 bg-panel"
        >
          <div className="flex items-center justify-between border-b border-white/10 px-6 py-4">
            <div className="flex items-center gap-2.5">
              {running ? (
                <>
                  <span className="relative flex h-2.5 w-2.5">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" />
                    <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-accent" />
                  </span>
                  <span className="font-mono text-xs font-bold tracking-[0.3em] text-foreground">
                    LIVE
                  </span>
                </>
              ) : phase === "briefing" ? (
                <>
                  <span className="h-2.5 w-2.5 rounded-full bg-muted" />
                  <span className="font-mono text-xs font-bold tracking-[0.3em] text-muted">
                    COMPLETE
                  </span>
                </>
              ) : (
                <>
                  <span className="h-2.5 w-2.5 rounded-full bg-white/20" />
                  <span className="font-mono text-xs font-bold tracking-[0.3em] text-muted">
                    STANDBY
                  </span>
                </>
              )}
            </div>
            <div className="hidden font-mono text-xs tracking-wider text-muted sm:block">
              scenario: {scenarioId ?? "—"}&nbsp;&nbsp;·&nbsp;&nbsp;websocket:{" "}
              {connection === "lost" ? (
                <span className="text-accent">lost</span>
              ) : (
                connection
              )}
            </div>
          </div>

          {connection === "lost" && running ? (
            <div className="border-b border-accent/40 bg-accent-dim px-6 py-2 font-mono text-xs tracking-wider text-accent">
              ⚠ connection lost — the feed is paused. Click &quot;Run a scenario&quot; again
              to retry.
            </div>
          ) : null}

          <div className="grid gap-4 p-0 lg:grid-cols-[1fr,400px]">
            <div
              ref={feedRef}
              onScroll={onFeedScroll}
              className="feed-scroll max-h-[560px] overflow-y-auto border-t border-white/10 px-6 py-6 font-mono text-[13px] leading-relaxed lg:border-t-0"
            >
              {entries.length === 0 ? (
                <p className="text-muted">
                  {running || connection === "connecting"
                    ? "connecting to live feed…"
                    : "Run a scenario to begin the live feed."}
                </p>
              ) : (
                entries.map((entry, i) => (
                  <FeedItem key={entry.kind === "debate" ? `${entry.conflictId}` : `l-${i}`} entry={entry} />
                ))
              )}
            </div>
            <SimulationPanel />
          </div>
        </motion.div>
      </div>
    </section>
  );
}