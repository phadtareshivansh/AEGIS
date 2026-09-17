"use client";

import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { useScenario } from "./ScenarioProvider";
import type { LiveLocation, PreviewResult } from "./ScenarioProvider";

const EASE = [0.22, 1, 0.36, 1] as const;

type PreviewState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "done"; result: PreviewResult }
  | { kind: "error"; message: string };

export default function Hero() {
  const {
    phase,
    runScenario,
    dataMode,
    setDataMode,
    quickPicks,
    resolvedLocation,
    setResolvedLocation,
    geocodeLocations,
    previewLocation,
  } = useScenario();
  const busy = phase === "running";

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<LiveLocation[]>([]);
  const [showResults, setShowResults] = useState(false);
  const [searching, setSearching] = useState(false);
  const [searchMessage, setSearchMessage] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreviewState>({ kind: "idle" });
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function runSearch(q: string) {
    const text = q.trim();
    if (text.length < 2) {
      setResults([]);
      setSearchMessage(null);
      setShowResults(false);
      return;
    }
    setSearching(true);
    setSearchMessage(null);
    try {
      const list = await geocodeLocations(text);
      setResults(list);
      setShowResults(true);
      if (list.length === 0) {
        setSearchMessage(`No matches for “${text}”. Try a district or river-adjacent city.`);
      }
    } catch (err) {
      setResults([]);
      setSearchMessage(`Geocoding failed: ${(err as Error).message}`);
    } finally {
      setSearching(false);
    }
  }

  function onQueryChange(value: string) {
    setQuery(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => runSearch(value), 350);
  }

  async function selectLocation(loc: LiveLocation) {
    setResolvedLocation(loc);
    setShowResults(false);
    setQuery("");
    setPreview({ kind: "checking" });
    try {
      const result = await previewLocation(loc);
      setPreview({ kind: "done", result });
    } catch (err) {
      setPreview({ kind: "error", message: (err as Error).message });
    }
  }

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);

  async function handleRun() {
    await runScenario();
    document.getElementById("command")?.scrollIntoView({ behavior: "smooth" });
  }

  function LocationStatus() {
    if (preview.kind === "checking") {
      return (
        <p className="font-mono text-[11px] tracking-[0.15em] text-muted">
          checking hydrology at {resolvedLocation?.name}…
        </p>
      );
    }
    if (preview.kind === "error") {
      return (
        <p className="font-mono text-[11px] tracking-[0.15em] text-accent">
          ✗ Sensing temporarily unavailable — {preview.message}
        </p>
      );
    }
    if (preview.kind === "done") {
      const status = preview.result.hydrology_status;
      const rain = preview.result.rainfall_mm_24h;
      if (status === "available") {
        return (
          <p className="font-mono text-[11px] tracking-[0.15em] text-foreground/80">
            ✓ {resolvedLocation?.name} — river + rainfall data available
            {typeof rain === "number" ? ` (${rain.toFixed(0)} mm/24h)` : ""}
          </p>
        );
      }
      if (status === "insufficient") {
        return (
          <p className="font-mono text-[11px] tracking-[0.15em] text-amber-300">
            ⚠ {resolvedLocation?.name} — no river reach; rainfall-only flash
            watch. Riverine risk will NOT be reported.
          </p>
        );
      }
      return (
        <p className="font-mono text-[11px] tracking-[0.15em] text-accent">
          ✗ {resolvedLocation?.name} — no hydrology data. Result will be
          “flood probability: not applicable”.
        </p>
      );
    }
    if (!resolvedLocation) {
      return (
        <p className="font-mono text-[11px] tracking-[0.15em] text-muted">
          Search a place or pick a quick start location.
        </p>
      );
    }
    return (
      <p className="font-mono text-[11px] tracking-[0.15em] text-muted">
        {resolvedLocation.name} selected — checking data…
      </p>
    );
  }

  const canRun = !busy && (dataMode === "demo" || resolvedLocation !== null);

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
          <motion.div className="mt-8 flex flex-wrap items-center gap-4">
            <button
              type="button"
              onClick={() => setDataMode(dataMode === "live" ? "demo" : "live")}
              className={`font-mono text-[11px] tracking-[0.25em] ${
                dataMode === "live" ? "text-accent" : "text-muted"
              }`}
            >
              {dataMode === "live" ? "● LIVE DATA" : "○ DEMO DATA"}
            </button>
          </motion.div>

          {dataMode === "live" ? (
            <div className="relative mt-5 max-w-lg">
              <div className="flex items-center gap-2 border border-white/15 bg-background/60 px-3 py-2 focus-within:border-accent">
                <span className="font-mono text-[11px] text-muted">⌕</span>
                <input
                  value={query}
                  onChange={(e) => onQueryChange(e.target.value)}
                  onFocus={() => setShowResults(true)}
                  onBlur={() => setTimeout(() => setShowResults(false), 150)}
                  placeholder="Search any city or place…"
                  className="w-full bg-transparent font-mono text-xs text-foreground outline-none placeholder:text-muted"
                />
                {searching ? (
                  <span className="animate-pulse font-mono text-[10px] text-muted">
                    searching…
                  </span>
                ) : null}
              </div>

              {showResults && results.length > 0 ? (
                <ul className="absolute left-0 right-0 z-20 mt-1 max-h-64 overflow-y-auto border border-white/15 bg-panel py-1">
                  {results.map((loc, i) => (
                    <li key={`${loc.lat}-${loc.lon}-${i}`}>
                      <button
                        type="button"
                        onMouseDown={(e) => {
                          e.preventDefault();
                          selectLocation(loc);
                        }}
                        className="block w-full px-4 py-2 text-left font-mono text-xs text-foreground/90 transition-colors hover:bg-white/5"
                      >
                        {loc.name}
                        <span className="text-muted">
                          {loc.admin1 || loc.country
                            ? ` · ${[loc.admin1, loc.country].filter(Boolean).join(", ")}`
                            : ""}
                        </span>
                        {typeof loc.elevation_m === "number" ? (
                          <span className="ml-2 text-[10px] text-muted">
                            {Math.round(loc.elevation_m)} m
                          </span>
                        ) : null}
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <span className="font-mono text-[10px] tracking-[0.2em] text-muted">
                  QUICK PICKS
                </span>
                {quickPicks.map((loc) => {
                  const active = resolvedLocation?.lat === loc.lat && resolvedLocation?.lon === loc.lon;
                  return (
                    <button
                      key={loc.key ?? loc.name}
                      type="button"
                      onMouseDown={(e) => {
                        e.preventDefault();
                        selectLocation(loc);
                      }}
                      className={`border px-3 py-1 font-mono text-[11px] tracking-[0.1em] transition-colors ${
                        active
                          ? "border-accent bg-accent text-background"
                          : "border-white/15 text-foreground/70 hover:border-accent/50 hover:text-foreground"
                      }`}
                    >
                      {loc.name}
                    </button>
                  );
                })}
              </div>

              <div className="mt-3">
                <LocationStatus />
              </div>

              {searchMessage ? (
                <p className="mt-2 font-mono text-[11px] tracking-[0.1em] text-accent">
                  {searchMessage}
                </p>
              ) : null}
            </div>
          ) : null}

          <button
            onClick={handleRun}
            disabled={!canRun}
            className="mt-8 inline-block bg-accent px-10 py-4 font-display text-base font-bold tracking-tight text-background transition-transform duration-200 ease-out hover:-translate-y-0.5 hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy ? "Running…" : "Run a scenario"}
          </button>
          {dataMode === "live" && !resolvedLocation && !busy ? (
            <p className="mt-2 font-mono text-[11px] tracking-[0.1em] text-muted">
              Pick a location to enable live data.
            </p>
          ) : null}
        </motion.div>
      </div>
    </section>
  );
}