"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";

const API_BASE = (process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000").replace(
  /\/+$/,
  "",
);
const WS_BASE = API_BASE.replace(/^http/, "ws");
const PRESET = {
  rainfall_mm_24h: 250,
  river_level_m: 6.5,
  river_level_danger_threshold_m: 4.0,
};

export type ScenarioEvent = {
  type: string;
  agent: string;
  message: string;
  data?: Record<string, unknown> | null;
  timestamp: string;
};

export type Briefing = {
  headline: string;
  risk_summary: string;
  resource_plan: string;
  conflict_resolution: string | null;
  recommended_actions: string[];
};

export type DebateEntry = {
  kind: "debate";
  conflictId: string;
  resource: string | null;
  turns: Partial<Record<number, string>>;
  resolution: ScenarioEvent | null;
};

export type LineEntry = {
  kind: "line";
  event: ScenarioEvent;
};

export type Entry = LineEntry | DebateEntry;

type Phase = "idle" | "running" | "briefing";
type Connection = "idle" | "connecting" | "live" | "lost";

type ScenarioContextValue = {
  phase: Phase;
  connection: Connection;
  entries: Entry[];
  scenarioId: string | null;
  briefing: Briefing | null;
  runScenario: () => Promise<void>;
  resetScenario: () => void;
};

const ScenarioContext = createContext<ScenarioContextValue | null>(null);

export function useScenario(): ScenarioContextValue {
  const ctx = useContext(ScenarioContext);
  if (!ctx) throw new Error("useScenario must be used within ScenarioProvider");
  return ctx;
}

function upsertTurn(
  entries: Entry[],
  conflictId: string,
  turn: number,
  message: string,
  resource: string | null,
): Entry[] {
  const idx = entries.findIndex(
    (e) => e.kind === "debate" && e.conflictId === conflictId,
  );
  const conflictIdKey = conflictId;
  if (idx === -1) {
    return [...entries, {
      kind: "debate",
      conflictId: conflictIdKey,
      resource,
      turns: { [turn]: message },
      resolution: null,
    }];
  }
  const existing = entries[idx];
  if (existing.kind !== "debate") return entries;
  const prev = existing.turns[turn] ?? "";
  const next: DebateEntry = {
    ...existing,
    resource: existing.resource ?? resource,
    turns: { ...existing.turns, [turn]: prev + message.slice(prev.length) },
  };
  const copy = [...entries];
  copy[idx] = next;
  return copy;
}

function attachResolution(
  entries: Entry[],
  conflictId: string,
  event: ScenarioEvent,
): Entry[] {
  const idx = entries.findIndex(
    (e) => e.kind === "debate" && e.conflictId === conflictId,
  );
  if (idx === -1) return [...entries, { kind: "line", event }];
  const existing = entries[idx];
  if (existing.kind !== "debate") return entries;
  const next: DebateEntry = { ...existing, resolution: event };
  const copy = [...entries];
  copy[idx] = next;
  return copy;
}

export function ScenarioProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [connection, setConnection] = useState<Connection>("idle");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [scenarioId, setScenarioId] = useState<string | null>(null);
  const [briefing, setBriefing] = useState<Briefing | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const resourcesRef = useRef<Record<string, string>>({});
  const completedRef = useRef(false);

  const resetScenario = useCallback(() => {
    wsRef.current?.close();
    wsRef.current = null;
    resourcesRef.current = {};
    completedRef.current = false;
    setPhase("idle");
    setConnection("idle");
    setEntries([]);
    setBriefing(null);
    setScenarioId(null);
  }, []);

  const runScenario = useCallback(async () => {
    if (phase === "running") return;
    const id = `demo_${Date.now()}`;
    wsRef.current?.close();
    resourcesRef.current = {};
    completedRef.current = false;
    setScenarioId(id);
    setPhase("running");
    setConnection("connecting");
    setEntries([]);
    setBriefing(null);

    try {
      const res = await fetch(`${API_BASE}/run-scenario`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario_id: id, raw_data: PRESET }),
      });
      if (!res.ok) throw new Error(`POST /run-scenario -> ${res.status}`);
    } catch (err) {
      const event: ScenarioEvent = {
        type: "error",
        agent: "pipeline",
        message: `Failed to start scenario: ${(err as Error).message}`,
        data: null,
        timestamp: new Date().toISOString(),
      };
      setEntries([{ kind: "line", event }]);
      setConnection("lost");
      setPhase("idle");
      return;
    }

    const ws = new WebSocket(`${WS_BASE}/ws/feed/${id}`);
    wsRef.current = ws;
    ws.onopen = () => setConnection("live");
    ws.onmessage = (evt) => {
      try {
        const event = JSON.parse(evt.data as string) as ScenarioEvent;
        if (event.type === "negotiation_turn") {
          const cid = String(event.data?.conflict_id ?? "");
          const turn = Number(event.data?.turn ?? 0);
          const resource = resourcesRef.current[cid] ?? null;
          setEntries((prev) => upsertTurn(prev, cid, turn, event.message, resource));
        } else if (event.type === "resolution") {
          const cid = String(event.data?.conflict_id ?? "");
          setEntries((prev) => attachResolution(prev, cid, event));
        } else if (event.type === "briefing_ready" && event.data) {
          setBriefing(event.data as unknown as Briefing);
        } else if (event.type === "scenario_complete") {
          completedRef.current = true;
          setPhase("briefing");
        } else {
          if (event.type === "conflict_flagged") {
            const data = (event.data ?? {}) as Record<string, unknown>;
            const cid = String(data.id ?? "");
            const resource = String(data.resource ?? "");
            if (cid && resource && !resourcesRef.current[cid]) {
              resourcesRef.current[cid] = resource;
            }
          }
          setEntries((prev) => [...prev, { kind: "line", event }]);
        }
      } catch {
        /* ignore malformed payloads */
      }
    };
    ws.onerror = () => setConnection("lost");
    ws.onclose = () => {
      if (wsRef.current === ws && !completedRef.current) {
        setConnection((prev) =>
          prev === "live" || prev === "connecting" ? "lost" : prev,
        );
      }
    };
  }, [phase]);

  const value = useMemo<ScenarioContextValue>(
    () => ({
      phase,
      connection,
      entries,
      scenarioId,
      briefing,
      runScenario,
      resetScenario,
    }),
    [phase, connection, entries, scenarioId, briefing, runScenario, resetScenario],
  );

  return <ScenarioContext.Provider value={value}>{children}</ScenarioContext.Provider>;
}