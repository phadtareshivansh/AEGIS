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

/** Curated demo locations: plain coordinates used as quick-pick chips.
 * They carry no fabricated rating curves — live river data is derived
 * location-generically from GloFAS in the backend. */
const QUICK_PICKS: LiveLocation[] = [
  { key: "pune", name: "Pune", lat: 18.5204, lon: 73.8567, elevation_m: 561 },
  { key: "kolhapur", name: "Kolhapur", lat: 16.6913, lon: 74.2447, elevation_m: 569 },
  { key: "surat", name: "Surat", lat: 21.1702, lon: 72.8311, elevation_m: 12 },
  { key: "kolkata", name: "Kolkata", lat: 22.5726, lon: 88.3639, elevation_m: 9 },
  { key: "guwahati", name: "Guwahati", lat: 26.1445, lon: 91.7362, elevation_m: 55 },
];

// Matches backend/agents/sensing.py geocode response shape.
export type LiveLocation = {
  name: string;
  lat: number;
  lon: number;
  elevation_m: number;
  admin1?: string | null;
  country?: string | null;
  key?: string;
};

export type PreviewResult = {
  ok?: boolean;
  rainfall_mm_24h?: number;
  hydrology_status?: "available" | "insufficient" | "not_applicable";
  hydrology_reason?: string | null;
  river_discharge_m3s?: number;
  river_q_mean_m3s?: number;
  reach_offset_km?: number;
  detail?: string;
};


async function fetchRunScenario(payload: {
  scenario_id: string;
  raw_data: Record<string, unknown>;
}): Promise<void> {
  const res = await fetch(`${API_BASE}/run-scenario`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`run-scenario HTTP ${res.status}`);
}

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

export type SimulationVisual =
  | { source: "image"; imageUrl: string; model?: string }
  | { source: "svg"; svg: string };

export type PolicyRecommendation = {
  decision: string;
  justification: string;
  winning_side: string | null;
};

export type DebateEntry = {
  kind: "debate";
  conflictId: string;
  resource: string | null;
  turns: Partial<Record<number, string>>;
  resolution: ScenarioEvent | null;
  policy: {
    recommendation: PolicyRecommendation | null;
    agree: boolean | null;
    flagged: boolean;
  };
  pending: boolean;
  finalized: Finalized | null;
};

export type Finalized = {
  status: "approved" | "overridden";
  decision: string;
  approvedBy: string;
  overrideReason?: string | null;
};

export type LineEntry = {
  kind: "line";
  event: ScenarioEvent;
};

export type Entry = LineEntry | DebateEntry;

type Phase = "idle" | "running" | "briefing";
type Connection = "idle" | "connecting" | "live" | "lost";
type DataMode = "demo" | "live";

export type ScenarioContextValue = {
  phase: Phase;
  connection: Connection;
  entries: Entry[];
  scenarioId: string | null;
  briefing: Briefing | null;
  simulation: SimulationVisual | null;
  dataMode: DataMode;
  setDataMode: (mode: DataMode) => void;
  quickPicks: LiveLocation[];
  resolvedLocation: LiveLocation | null;
  setResolvedLocation: (loc: LiveLocation | null) => void;
  geocodeLocations: (q: string) => Promise<LiveLocation[]>;
  previewLocation: (loc: LiveLocation) => Promise<PreviewResult>;
  pendingApprovals: number;
  runScenario: () => Promise<void>;
  approveConflict: (conflictId: string, approvedBy: string) => Promise<void>;
  overrideConflict: (
    conflictId: string,
    opts: { approved_by: string; override_reason: string; override_decision: string },
  ) => Promise<void>;
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
    return [
      ...entries,
      {
        kind: "debate",
        conflictId: conflictIdKey,
        resource,
        turns: { [turn]: message },
        resolution: null,
        policy: { recommendation: null, agree: null, flagged: false },
        pending: false,
        finalized: null,
      },
    ];
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
  const data = (event.data ?? {}) as Record<string, unknown>;
  const recommendation =
    (data.policy_recommendation as PolicyRecommendation | null) ?? null;
  const agree = typeof data.agree === "boolean" ? data.agree : null;
  const policy = {
    recommendation,
    agree,
    flagged: agree === false,
  };
  const idx = entries.findIndex(
    (e) => e.kind === "debate" && e.conflictId === conflictId,
  );
  if (idx === -1) {
    return [
      ...entries,
      {
        kind: "debate",
        conflictId,
        resource: null,
        turns: {},
        resolution: event,
        policy,
        pending: false,
        finalized: null,
      },
    ];
  }
  const existing = entries[idx];
  if (existing.kind !== "debate") return entries;
  const next: DebateEntry = { ...existing, resolution: event, policy };
  const copy = [...entries];
  copy[idx] = next;
  return copy;
}

function setConflictState(
  entries: Entry[],
  conflictId: string,
  patch: { pending?: boolean; finalized?: Finalized | null },
): Entry[] {
  const idx = entries.findIndex(
    (e) => e.kind === "debate" && e.conflictId === conflictId,
  );
  if (idx === -1) return entries;
  const existing = entries[idx];
  if (existing.kind !== "debate") return entries;
  const copy = [...entries];
  copy[idx] = { ...existing, ...patch };
  return copy;
}

function buildRawData(dataMode: DataMode, resolvedLocation: LiveLocation | null) {
  if (dataMode !== "live" || !resolvedLocation) return PRESET;
  return {
    ...PRESET,
    data_mode: "live",
    location: {
      name: resolvedLocation.name,
      lat: resolvedLocation.lat,
      lon: resolvedLocation.lon,
      elevation_m: resolvedLocation.elevation_m,
    },
  };
}

export async function geocodeLocations(q: string): Promise<LiveLocation[]> {
  const url = `${API_BASE}/geocode?q=${encodeURIComponent(q)}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`geocode HTTP ${res.status}`);
  const body: unknown = await res.json();
  return Array.isArray(body) ? (body as LiveLocation[]) : [];
}

export async function previewLocation(
  loc: LiveLocation,
): Promise<PreviewResult> {
  const url = `${API_BASE}/live/preview?lat=${loc.lat}&lon=${loc.lon}`;
  const res = await fetch(url);
  const body: PreviewResult = await res.json();
  if (!res.ok) return { ...body, ok: false };
  return { ...body, ok: true };
}

export function ScenarioProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [connection, setConnection] = useState<Connection>("idle");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [scenarioId, setScenarioId] = useState<string | null>(null);
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [simulation, setSimulation] = useState<SimulationVisual | null>(null);
  const [dataMode, setDataMode] = useState<DataMode>("demo");
  const [resolvedLocation, setResolvedLocation] = useState<LiveLocation | null>(null);
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
    setSimulation(null);
    setScenarioId(null);
    setDataMode("demo");
    setResolvedLocation(null);
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
    setSimulation(null);

    try {
      await fetchRunScenario({
        scenario_id: id,
        raw_data: buildRawData(dataMode, resolvedLocation),
      });
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
        } else if (event.type === "approval_needed") {
          const cid = String(event.data?.conflict_id ?? "");
          setEntries((prev) => setConflictState(prev, cid, { pending: true }));
        } else if (event.type === "policy_disagreement") {
          const cid = String(event.data?.conflict_id ?? "");
          setEntries((prev) => [
            ...prev.map((e) =>
              e.kind === "debate" && e.conflictId === cid
                ? { ...e, policy: { ...e.policy, flagged: true } }
                : e,
            ),
            { kind: "line", event },
          ]);
        } else if (event.type === "resolution_approved") {
          applyFinalizedFromEvent(event, "approved");
        } else if (event.type === "resolution_overridden") {
          applyFinalizedFromEvent(event, "overridden");
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
          } else if (event.agent === "simulation" && event.data) {
            const data = event.data as Record<string, unknown>;
            if (typeof data.image_url === "string") {
              setSimulation({
                source: "image",
                imageUrl: data.image_url,
                model: typeof data.model === "string" ? data.model : undefined,
              });
            } else if (typeof data.svg === "string") {
              setSimulation({ source: "svg", svg: data.svg });
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
  }, [phase, dataMode, resolvedLocation]);

  const pendingApprovals = useMemo(
    () => entries.filter((e) => e.kind === "debate" && e.pending).length,
    [entries],
  );

  const applyFinalizedFromEvent = useCallback(
    (event: ScenarioEvent, fallbackStatus: "approved" | "overridden") => {
      const cid = String(event.data?.conflict_id ?? "");
      const data = (event.data ?? {}) as Record<string, unknown>;
      const status =
        data.status === "overridden" || data.status === "approved"
          ? (data.status as "approved" | "overridden")
          : fallbackStatus;
      const finalized: Finalized = {
        status,
        decision:
          typeof data.decision === "string"
            ? data.decision
            : event.message,
        approvedBy:
          typeof data.approved_by === "string" ? data.approved_by : "operator",
        overrideReason:
          typeof data.override_reason === "string"
            ? data.override_reason
            : null,
      };
      setEntries((prev) =>
        setConflictState(prev, cid, { pending: false, finalized }),
      );
    },
    [],
  );

  const pushErrorLine = useCallback((message: string) => {
    const event: ScenarioEvent = {
      type: "error",
      agent: "pipeline",
      message,
      data: null,
      timestamp: new Date().toISOString(),
    };
    setEntries((prev) => [...prev, { kind: "line", event }]);
  }, []);

  const approveConflict = useCallback(
    async (conflictId: string, approvedBy: string) => {
      if (!scenarioId) return;
      try {
        const res = await fetch(
          `${API_BASE}/scenarios/${scenarioId}/conflicts/${conflictId}/approve`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ approved_by: approvedBy }),
          },
        );
        const body = await res.json();
        if (!res.ok) throw new Error(body.detail ?? `HTTP ${res.status}`);
        applyFinalizedFromEvent(
          {
            type: "resolution_approved",
            agent: "negotiator",
            message: body.message ?? `Approved by ${approvedBy}`,
            data: body,
            timestamp: new Date().toISOString(),
          },
          "approved",
        );
      } catch (err) {
        pushErrorLine(`Approve failed: ${(err as Error).message}`);
        throw err;
      }
    },
    [scenarioId, applyFinalizedFromEvent, pushErrorLine],
  );

  const overrideConflict = useCallback(
    async (
      conflictId: string,
      opts: { approved_by: string; override_reason: string; override_decision: string },
    ) => {
      if (!scenarioId) return;
      try {
        const res = await fetch(
          `${API_BASE}/scenarios/${scenarioId}/conflicts/${conflictId}/override`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(opts),
          },
        );
        const body = await res.json();
        if (!res.ok) throw new Error(body.detail ?? `HTTP ${res.status}`);
        applyFinalizedFromEvent(
          {
            type: "resolution_overridden",
            agent: "negotiator",
            message: body.decision ?? opts.override_decision,
            data: body,
            timestamp: new Date().toISOString(),
          },
          "overridden",
        );
      } catch (err) {
        pushErrorLine(`Override failed: ${(err as Error).message}`);
        throw err;
      }
    },
    [scenarioId, applyFinalizedFromEvent, pushErrorLine],
  );

  const resetScenarioStable = resetScenario;
  const value = useMemo<ScenarioContextValue>(
    () => ({
      phase,
      connection,
      entries,
      scenarioId,
      briefing,
      simulation,
      dataMode,
      setDataMode,
      quickPicks: QUICK_PICKS,
      resolvedLocation,
      setResolvedLocation,
      geocodeLocations,
      previewLocation,
      pendingApprovals,
      runScenario,
      approveConflict,
      overrideConflict,
      resetScenario: resetScenarioStable,
    }),
    [phase, connection, entries, scenarioId, briefing, simulation, dataMode, resolvedLocation,
     pendingApprovals, runScenario, approveConflict, overrideConflict, resetScenarioStable],
  );

  return (
    <ScenarioContext.Provider value={value}>{children}</ScenarioContext.Provider>
  );
}
