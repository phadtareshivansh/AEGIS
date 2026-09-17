# AEGIS — Limitations

An honest statement of what AEGIS is, what it is validated against, and where you
should not trust it. Read this before treating any AEGIS output as a basis for a
decision. A tool that states its limits is more trustworthy than one that
overclaims — this document exists so anyone (a real user, a reviewer, a future
contributor) can evaluate AEGIS without being misled.

Last verified against the codebase and the validation results: Build 2, final
pass. Figures in §2–§3 come from `backend/validation/RESULTS.md` (reproducible
via `python3 backend/validation/backtest.py --spatial --tune`).

---

## 1. What AEGIS is

AEGIS is a multi-agent disaster-response *coordinator prototype*. It:

1. **Senses** live rainfall and river discharge for a geocoded location
   (or uses a demo preset).
2. **Predicts** per-zone flood risk with a deterministic scoring function.
3. **Allocates** resources and flags conflicts between competing claims.
4. **Debates** each conflict through a structured LLM negotiation, then checks
   the debate's outcome against an explicit, auditable policy ruleset.
5. **Briefs** a human operator in plain language.

Every conflict resolution stops in front of a human. AEGIS proposes; it does
not act. That property is structural — see §5.

What AEGIS is **not** is in §7. If you are making an evacuation, ordering, or
resource decision in a real flood, the relevant legal and moral authority is the
official meteorological / disaster-management service for your region, not this
prototype.

---

## 2. What the prediction model is validated for

The shipped scoring function (`backend/agents/prediction.py`, factor weights
river 0.35 / proximity 0.25 / elevation 0.20 / rainfall 0.20, dynamic/historical
blend 70/30) was run against **real historical floods with known outcomes**:

- **Bangladesh weather-station dataset (1948–2013):** 20,544 monthly records at
  33 real climate stations (Gauhar, Das & Moury, IEEE 2021), each with real
  measured rainfall, real station elevation, real lat/lon, and a ground-truth
  `Flood?` label compiled from Bangladesh flood reports.
- **Kerala, August 2018 case study:** district-wise realised rainfall from the
  official CWC *Study Report*, ground-truth affected-district lists from the
  IIT Delhi / IMD India Flood Inventory (Zenodo 10.5281/zenodo.16994648).

Measured results (shipped weights, 7 validation events):

| metric                 | shipped | spatial variant |
|------------------------|--------:|----------------:|
| mean precision@5       |   0.771 |           0.829 |
| mean recall@5          |   0.205 |           0.226 |
| mean Spearman ρ        |   0.286 |           0.394 |
| Kerala 2018 precision@5|   1.000 | (uninformative) |
| Kerala 2018 recall@5   |   0.357 |                 |

What these numbers genuinely support, and the honest reading:

- On **broad monsoon floods in flat, riverine, Bengal-style terrain**, the
  static ranking (low elevation, near river, historically floody) reliably
  surfaces the *perennially* flooded zones. On the four big multi-state events
  the top-5 was 100% or 80% real flood zones.
- The Kerala precision@5 of 1.0 is **meaningless as praise**: 13–14 of 14
  districts were affected, so any top-5 was "correct." Only the *ordering*
  discussion carries information, and that ordering exposes the model's
  weakness (§3).
- The weights are hand-picked. An in-sample grid search (`--tune`) found that
  raising historical weight to ~0.4–0.5 would improve means (+0.03 precision,
  +0.14 recall), but that tuning is in-sample/overfit and was deliberately **not**
  adopted.

Interpretation, stated plainly: **the model's output is a relative risk score,
not a calibrated probability of flooding.** Treat the top-N ranking as "which
zones should a human look at first in this region," never as "this zone has an
X% chance of flooding."

---

## 3. What the prediction model is NOT validated for

The validation is honest about where it fails, and those failures are structural.

### Measured weaknesses (from the validation run)

- **Spatially blind ranking.** `score_zone`/`rank_zones` apply *one* rainfall and
  *one* river level uniformly to every zone. Within a scenario, river and
  rainfall factors are therefore constants across zones, and the ranking is
  produced solely by proximity, elevation, and historical risk. The same five
  zones appear at the top in six of seven events, regardless of what is actually
  happening.
- **No flood/threshold discrimination.** On the dry-season control event
  (2009-10, only 1 of 33 zones flooded) precision@5 = **0.0** — the model ranked
  five zones as at-risk every bit as confidently as in flood months. Nothing in
  the function distinguishes "flood now" from "historically floody."
- **Low recall.** Mean recall@5 = **0.205**: the model typically surfaces under a
  quarter of the zones that actually flood.
- **Missed the worst-hit districts.** In Kerala 2018, Idukki (3,556 mm, highest
  rainfall in the state) ranked 6th and Wayanad (catastrophic landslides) ranked
  14th (last). The elevation factor `1/(1+elev/10)` actively punishes high
  terrain — it cannot see high-ground extreme-rainfall or reservoir-driven
  flooding.

### Structural facts to know before using it

- **The river factor was never tested against real gauge data.** No free, bulk,
  historical river-stage archive covers the validation window. `river_level_m`
  was a rainfall-percentile **proxy**, not real stage data. The 0.35-weight
  "river" term was effectively tested as a rainfall-exceedance proxy.
- **Rainfall unit mismatch.** `RAINFALL_REFERENCE_MM = 200` (a 24h reference) was
  fed monthly/cumulative totals, so monsoon rainfall factors saturate at 1.0.
  Absolute probabilities are not calibrated (§2).
- **`population` is dead weight.** `load_zones` loads it and `score_zone` never
  reads it. Any intuition that denser zones rank higher is false — the factor
  does nothing.
- **The 12h→24h number is a heuristic.** `flood_probability_24h = min(1.0, p12 ×
  1.15)` — a fixed 15% inflation, not an independently computed 24h estimate.
- **Static inputs are approximate.** Distance-to-river uses hand-traced
  centerlines (±2–5 km); the Kerala district elevations are approximate.

### Explicitly NOT validated — do not use for

- **Coastal storm surge** (no surge or tide inputs exist).
- **Flash floods in mountainous terrain** (the elevation factor mis-ranks
  highland catchments that drain extreme rainfall).
- **Dam / reservoir-release flooding**.
- **Regions with different drainage characteristics** than the low-lying,
  riverine, monsoon-fed Bengal-type validation set.
- **Any question that needs a spatial answer** ("which specific zones are at risk
  *this* event") rather than a historically conditioned ranking.

---

## 4. Data sources and their known limits

### Open-Meteo Weather API

- **Used for:** 24-hour `precipitation_sum` in live sensing.
- **Known limits:** model resolution varies 1–55 km depending on region and
  provider; hourly (or interpolated) value cadence; 15-minutely data exists only
  for Central Europe and North America. The returned coordinate is the center of
  the grid cell used, which "might be a few kilometres away from the requested
  coordinate" (Open-Meteo documentation). It is a free, keyless API with **no
  availability or accuracy SLA**.

### Open-Meteo Flood API (GloFAS)

- **Used for:** river discharge in live sensing, and the derived danger threshold.
- **What it returns:** simulated river *discharge* (m³/s), not measured water
  level. Data is the Copernicus/ECMWF **GloFAS** product, at **~5 km (v4) / ~11 km
  (v3) resolution**, daily timesteps.
- **Known limits (Open-Meteo's own wording):**
  > "Due to the 5 km resolution the closest river might not be selected
  > correctly. Varying coordinates by 0.1° can help to get a more representable
  > discharge rate."
  
  AEGIS attempts to mitigate this by sampling a ring of 0.1° offsets and picking
  the dominant reach, but the underlying grid-cell snapping limitation remains.
- **Coverage:** global in principle, but "global" means "a river reach within
  5 km of the coordinate" — many locations have no usable reach. AEGIS detects
  this and must not fabricate a number (§8). GloFAS reanalysis runs 1984–July
  2022; the daily forecast extends 30 days. Neither is a substitute for an
  in-river gauge reading.

### Derived danger threshold — the honest caveat

AEGIS does **not** fetch a real flood-warning threshold. It derives one as
`2.0 × the GloFAS climatological mean discharge for the same calendar date`, then
maps it to a nominal level through a deterministic power-law rating curve. This
is a **statistical surrogate, not an authority's flood threshold** (no official
per-gauge warning stages are publicly bulk-available). The absolute numbers it
produces for `river_level_m` / `river_level_danger_threshold_m` are therefore
meaningful only *relative to each other*, not in absolute river terms.

---

## 5. Resolutions are proposals, not actions

The pipeline is deliberately split so that the human gate is structural, not
advisory:

- Every conflict emits an `approval_needed` event. The debate graph **ends** at
  the negotiator node — the briefing graph is a separate phase that only runs
  after human approval.
- There is **no auto-approval path anywhere in the codebase**. The only
  resolution events (`resolution_approved`, `resolution_overridden`) are produced
  by human-initiated HTTP calls.
- The approval watchdog (default 15 minutes) emits a non-terminal
  `approval_timeout` event when a human is slow — it explicitly does **not**
  resolve, approve, escalate, or act. A conflict can sit unresolved indefinitely;
  AEGIS will nag, but it will not decide for you.
- When the LLM is unavailable, a conflict is parked with `winning_side: null`
  ("escalate to human coordinator") rather than defaulted to a side.

Nothing in this prototype executes an evacuation, dispatches a vehicle, or
spends a resource. AEGIS produces recommendations for a human to act on.

---

## 6. The negotiation debate is a framing tool, not an independent authority

The four-turn LLM debate (evacuation advocate / logistics advocate) makes the
tradeoffs of a resource conflict *legible*. It is **not** an independent decision
authority. Its output is checked against an explicit, deterministic policy
ruleset (`backend/agents/policy.py`), and the human always sees both:

- **Rule 1 — Life-safety outranks logistics.** An evacuation (people in
  immediate danger) beats pure logistics unconditionally.
- **Rule 2 — Both safety-critical → compare risk.** `risk = population ×
  flood_probability_12h`. If the relative margin is under 5%, or the scores tie,
  the policy **escalates to a human** instead of pretending the formula has
  precision it does not.
- **Rule 3 — No fabrication.** If a claim's zone is missing from the logistics
  plan, its risk data defaults to zero and the justification says so — AEGIS
  never invents a risk number.

Consequences of this design:

- `policy_agreement` compares the LLM arbiter's `winning_side` to the policy's.
  A disagreement emits a `policy_disagreement` event showing both
  recommendations side by side. **The LLM cannot override the policy** — it can
  agree or be flagged.
- The debate's *text* is persuasive framing that helps a human judge; the
  auditable recommendation is the policy result plus the human's own review.
- Because the debate topics are shaped by scenario data (populations,
  probabilities, pool totals) that was summarized from state, the LLM never sees
  raw internal dicts, and the system prompts forbid the LLM from inventing
  numbers.

---

## 7. Non-goals — explicit disclaimers

- **Not a certified emergency-management system.** AEGIS is a research
  prototype. It holds no certification (ISO, national civil-protection standard,
  or otherwise) and makes no regulatory claim.
- **Not a replacement for official alerts.** AEGIS is not an authoritative
  meteorological or disaster-warning service. Where your region's official
  services (e.g. IMD, CWC, FFWC/BWDB, national meteorological agencies, GloFAS
  operational products) issue warnings, those are authoritative; AEGIS is not.
- **Not a sole basis for evacuation or resource decisions in a real emergency.**
  The prediction and policy outputs are advisory, fallible, and carry the
  limitations in §2–§4. A human operator bears responsibility for decisions.
- **Not a calibrated probability source.** All AEGIS "probabilities" are relative
  risk scores. The numbers 0–1 do not mean "chance of flooding."
- **No availability guarantee.** All external data sources are free, keyless,
  unsupported APIs with no SLA, and the LLM provider is a third-party service.
  Any of them can fail, degrade, or change without notice.
- **No coverage guarantee.** A location without a river reach within GloFAS
  coverage gets a rainfall-only watch or an explicit "not applicable" result —
  not a riverine probability. See §8.

---

## 8. Known failure modes (observed during Build-2 testing) and how they are handled

These are modes that were actually observed or found in testing, not hypotheticals.

| Failure mode | What happens now | Mitigation / status |
|---|---|---|
| **Danger threshold = 0** causes `ZeroDivisionError` in the scorer | Was a crash that would strand every scenario behind a user-typed or degenerate threshold | **Fixed** — `score_zone` normalizes against `max(threshold, 1.0)`; covered by `test_edge_inputs.py::test_zero_danger_threshold_does_not_divide_by_zero` |
| **LLM provider unreachable** during a negotiation turn or arbitration | Returns `NEUTRAL_RESOLUTION` (`winning_side: null`, "escalate to human coordinator"); `error` events streamed; conflict parked for human approval | Validated end-to-end against a dead provider; no hang (server breaks the feed cleanly) |
| **Negative / nonsense raw input** | API returns strict `422` before the pipeline starts; pure functions stay in `[0,1]` | Covered by `test_edge_inputs.py` and API-contract tests |
| **Transient database failure** during a write | Writes retried with exponential backoff (3 attempts) | Covered by `test_db_resilience.py`; a fully-unreachable DB routes to §8 next row |
| **Database fully unreachable / fatal pipeline error** | `mark_scenario_error` sets `status="error"`; a terminal `error`/`pipeline` event is pushed to the live queue *before* the queue is deregistered, so a connected WebSocket always receives it and closes | No-hang invariant; covered by `test_db_resilience.py` |
| **Hydrology data absent** (no river reach / rainfall-only) | Emits explicit sentinel `not_applicable` / `insufficient_hydrology_data`, **not** a fabricated numeric probability; a rainfall-only "flash/urban flood watch" is labeled as such | Covered by edge-input tests; the frontend renders no confident number |
| **Simulation image service fails / times out (40 s)** | Falls back to a deterministic animated-SVG overlay; scenario continues | Non-terminal degradation |
| **Briefing LLM fails** | Falls back to a deterministic briefing; scenario still completes; a non-terminal `error`/`briefing` event is emitted | Scenario is never left hanging |
| **No river gauge data in the whole validation set** | The river factor was tested only as a rainfall-exceedance proxy | Documented in §3; do not interpret `river_level_m` as an observed reading |

### Residual gaps that are still open (not yet mitigated)

- **LLM streaming has no wall-clock timeout.** A provider that stalls mid-stream
  without raising could keep a negotiation turn running indefinitely. The retry
  logic only triggers on exceptions, not on silence. A future change should add
  an idle timeout on `generate_stream`.
- **Approval watchdog is in-memory only.** A backend restart while a scenario is
  `awaiting_approval` loses the 15-minute timer. The timeout event is then only
  materialized lazily on the next replay read. No server-side hang, but a client
  that stays connected across a restart will not see the timeout streamed.
- **Legacy script-style test files** (`test_prediction.py`, `test_negotiation.py`,
  etc.) run module-level asserts at import rather than as real pytest functions,
  so a bare `pytest` in `backend/` collects them as side effects and
  `test_negotiation.py` requires a live LLM. The supported way to run the suites
  is by explicit file name (see the test files under `backend/`).

---

## Bottom line

AEGIS is an honestly-implemented research prototype that is good at one thing —
surfacing, for flat and riverine monsoon floodplains, the zones a human should
look at first — and structurally blind to several flood mechanisms. It proposes
resolutions and explicitly does not execute them, and its data sources are free
APIs with real resolution and coverage limits. Where a number would be dishonest,
it emits an explicit "not applicable" instead. Where it cannot decide, it asks a
human. Trust it accordingly: as a scannable, transparent, first-pass decision
aid — never as an authority.