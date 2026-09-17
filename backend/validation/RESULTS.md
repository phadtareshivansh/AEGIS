# Backtest results: AEGIS flood scoring against real historical floods

This document reports the outcome of running the **shipped scoring function**
(`backend/agents/prediction.py` — weights `river .35 / proximity .25 /
elevation .20 / rainfall .20`, `70/30` dynamic/historical blend) against real
historical flood data with known outcomes. The numbers below come from
`python3 backend/validation/backtest.py --spatial --tune` and are reproducible
(raw files cached in `backend/validation/data/`, per-run JSON in
`data/results/backtest.json`).

This is an **honest validation, not a sales pitch**. Where the model failed, it
is reported below, verbatim.

---

## 1. Datasets used

### Primary — Bangladesh weather-station flood dataset (quantitative backtest)
- **Source:** [n-gauhar/Flood-prediction](https://github.com/n-gauhar/Flood-prediction),
  Gauhar, Das & Moury, *"Prediction of Flood in Bangladesh using k-Nearest
  Neighbors Algorithm"* (2021, IEEE).
- **Attribution & license:** Gauhar, N., Das, S., Moury, K.S. (2021). *Prediction of Flood in Bangladesh using k-Nearest Neighbors Algorithm.* IEEE ICREST 2021, pp. 357–361. Dataset — github.com/n-gauhar/Flood-prediction — **no license file; default copyright; local use only with citation**.
- **What it is:** 20,544 monthly records for **33 real climate stations**
  (1948–2013). Each row carries real** measured rainfall (mm)**, real **station
  elevation** (ALT), real **lat/lon**, and a ground-truth **`Flood?` label**
  (1 = floods reported that month; blank/0 = none), compiled from Bangladesh
  flood reports.
- **Why it works for us:** each station is a "zone"; the `Flood?` flag is a
  real per-area outcome; rainfall and elevation are real inputs matching two of
  the scoring function's four factors; 65 years lets us build a genuine,
  non-leaking "historical risk" per zone.

### Secondary — Kerala, August 2018 (documented case study)
- **Source:** India Flood Inventory – Impacts v4, HydroSense Lab IIT Delhi ×
  IMD, [Zenodo 10.5281/zenodo.16994648](https://zenodo.org/records/16994648)
  (Saharia et al., *Nat. Hazards* 2021; Saharia et al. 2025). District-level
  affected lists for IMD-sourced events 1967–2023; the Aug-2018 event
  (`UEI-IMD-FL-2018-0035/0036`) lists all 14 districts affected.
- **Attribution & license:** Saharia, M., Jain, A., Baishya, R.R., Haobam, S., Sreejith, O.P., Pai, D.S., Rafieeinasab, A. (2021). *India flood inventory: creation of a multi-source national geospatial database to facilitate comprehensive flood research.* Natural Hazards. DOI 10.1007/s11069-021-04698-6. Dataset v3 — Zenodo 10.5281/zenodo.16994648 — license **CC-BY-NC 4.0** (attribution required, non-commercial only).
- **Rainfall:** district-wise realised rainfall (1 Jun–22 Aug 2018) from the
  official CWC *Study Report: Kerala Floods of August 2018* (Table 3), IMD
  records. Real figures, embedded in `backtest.py`.
- **Ground truth caveat (real):** the inventory's label for this event is
  coarse — **all 14 districts** appear as affected. The CWC report prose states
  "severe flooding in 13 out of 14 districts" (Kasaragod was the least
  affected). This coarse label limits how much can be concluded (see §6).

### River gauge data — attempted, not publicly bulk-available
Bangladesh FFWC/BWDB publishes water levels only as ~40-day rolling windows
around the current date; the only public FFWC-derived historical archive found
is a July-2026 bulletin snapshot (`Hurairiam/flood-risk-analytics`), which lies
outside our 1948–2013 validation window. **No free, bulk, historical gauge
archive covers the events below.** Consequently the scoring function's river
factor is fed a documented proxy rather than real stage data (next section).

---

## 2. Field mapping and documented assumptions

| Scoring input | How derived from real data |
|---|---|
| `elevation_m` | Real `ALT` per station (BD). Kerala: approximate district-average terrain elevation (hand-entered, flagged approx). |
| `distance_to_river_km` | **Derived.** Haversine distance to the nearest point on approximate, hand-traced centerlines of the Padma/Ganges, Jamuna/Brahmaputra, Old Brahmaputra, Surma/Meghna, Lower Meghna, Arial Khan, Gorai/Madhumati and Karnaphuli. Positional accuracy ±2–5 km; used only as a *relative* proximity feature (`1/(1+d)`). |
| `population` | District/division population, Bangladesh Bureau of Statistics Census 2011, and Census of India 2011 (Kerala). **Note: the scoring function never reads `population`** — it is carried in the zone dict but does not affect probability. |
| `historical_flood_risk` | **Empirical, no leakage.** Fraction of flood-flagged months at that zone in all years *strictly before* the event year (BD). Kerala: fraction of IFI flood events (1967–2017) listing the district. |
| `rainfall_mm_24h` | Event-month station rainfall (BD, real). Kerala: Jun1–Aug22 cumulative (real). **Unit caveat:** these are monthly/cumulative totals fed into a reference designed for 24 h (`RAINFALL_REFERENCE_MM=200`), so monsoon-month `rainfall_factor` saturates at 1.0. Acknowledged; see §6. |
| `river_level_m` / threshold | **Proxy (no real gauge archive).** Stage index = percentile of event rainfall in the zone/climatology; proxy danger = 90th-percentile rainfall; level = `10·(1+overshoot)`, threshold = `10 m`. Only the *overshoot ratio* matters because the scoring divides by the threshold, so the absolute datum is arbitrary. Cleanly identified as an assumption, not real data. |

**Critical structural fact about the shipped artifact (validated):**
`score_zone`/`rank_zones` accept **one** rainfall and **one** river level for the
whole set of zones and apply them uniformly. Therefore, within a single event,
`river_factor` and `rainfall_factor` are *constants across zones*, and the
ranking is produced **solely** by `proximity`, `elevation`, and historical risk.
This is measured, not assumed, and shapes every result below. A per-zone
("spatial") variant of the same function is also evaluated to show the ceiling.

---

## 3. Quantitative results — Bangladesh (shipped weights)

Validation events (year-month): `1998-07` (29/33 zones flooded — Great Flood),
`2004-09` (28/33), `2011-08` (30/33), `2005-08` (19/32), `1997-06` (9/32,
localized), `1956-06` (14/16), and `2009-10` (1/33, dry-season **control**).

### precision@5 / recall@5 / Spearman ρ (default weights: 0.35/0.25/0.2/0.2, 70/30)

| event      | zones | actually flooded | **precision@5** | recall@5 | ρ(predicted, label) |
|------------|------:|-----------------:|----------------:|---------:|--------------------:|
| 1998-07    |    32 |               29 | **1.000**       | 0.172    | 0.168 |
| 2004-09    |    32 |               28 | **0.800**       | 0.143    | 0.276 |
| 2011-08    |    33 |               30 | **1.000**       | 0.167    | 0.421 |
| 1997-06    |    32 |                9 | **0.600**       | 0.333    | 0.335 |
| 2005-08    |    32 |               19 | **1.000**       | 0.263    | 0.541 |
| 1956-06    |    16 |               14 | **1.000**       | 0.357    | 0.205 |
| 2009-10    |    33 |                1 | **0.000**       | 0.000    | 0.056 |
| **mean**   |       |                  | **0.771**       | **0.205** | **0.286** |

Interpretation (this is the real story, not just the average):

- **The top-5 output is nearly static.** With uniform forcing the same five
  zones appear at the top in six of seven events — Hatiya, Cox's Bazar, Teknaf,
  Barisal, Maijdee Court — because the ranking is fixed by terrain + history.
- So on **near-nationwide floods** (29–30 of 33 zones flooded) precision@5 is
  trivially high (1.0): almost every zone flooded, so any plausible pick is a
  "hit." These are the numbers that flatter the model.
- On the **localized event** (1997-06, 9 flooded) precision@5 **drops to 0.6**:
  the model missed 6 of the 9 flooded zones (Chittagong IAP, Dinajpur, Kutubdia,
  Rangamati, Sitakunda, Sylhet) and raised false alarms (Cox's Bazar, Maijdee
  Court) purely because those are historically floody, low-lying stations —
  *not* because of 1997's conditions. The model cannot react to *where* the
  flood actually was.
- On the **dry-season control** (2009-10, one flooded zone) **precision@5 = 0.0**:
  the model ranked Hatiya, Teknaf, Barisal, Cox's Bazar, Maijdee Court as
  at-risk every bit as confidently as in flood months. It has no threshold —
  nothing in the function stops it from calling a quiet month "at risk."
- **Aggregate: mean precision@5 = 0.771, mean recall@5 = 0.205, mean
  ρ = 0.286.** Recall is the honest headline: **the model surfaces only ~20%
  (on average) of the zones that actually flood**, and Spearman correlation with
  outcome is weak.

### What happens if the function were allowed per-zone rainfall/level (same weights)?

Running the identical function with each zone's own rainfall and derived stage
is not what the shipped `rank_zones` does — it is included to quantify the cost
of the uniform-forcing design:

| event      | precision@5 (shipped) | precision@5 (spatial) | ρ (shipped → spatial) |
|------------|----------------------:|----------------------:|----------------------:|
| 1998-07    | 1.000 | 1.000 | 0.168 → 0.284 |
| 2004-09    | 0.800 | 1.000 | 0.276 → 0.327 |
| 2011-08    | 1.000 | 1.000 | 0.421 → 0.498 |
| 1997-06    | 0.600 | 0.800 | 0.335 → 0.440 |
| 2005-08    | 1.000 | 1.000 | 0.541 → 0.686 |
| 1956-06    | 1.000 | 1.000 | 0.205 → 0.410 |
| 2009-10    | 0.000 | 0.000 | 0.056 → 0.111 |
| **mean**   | **0.771** | **0.829** | **0.286 → 0.394** |

Feeding spatial forcing improves the moderate/localized events (2004-09: 0.8→1.0;
1997-06: 0.6→0.8) and roughly doubles the correlation. **The biggest, cheapest
improvement available is to make the scoring accept per-zone rainfall/river
inputs — the weight values are secondary.**

---

## 4. Weight tuning (`--tune`, IN-SAMPLE)

Grid search over `HISTORICAL_WEIGHT ∈ {0.3,0.4,0.5,0.2}` × `proximity ∈ {0.15,0.25,0.35}`
(keeping the dynamic sum 1.0), optimised on the same seven events. **This is
in-sample and overfits; treat as diagnostic, not as a validated improvement.**

| config | mean precision@5 | mean recall@5 | mean ρ |
|---|---:|---:|---:|
| **shipped** (0.30 / 0.25 / 0.20) | 0.771 | 0.205 | 0.286 |
| 0.40 hist, 0.35 prox, 0.10 elev | **0.800** | **0.348** | 0.415 |
| 0.50 hist, 0.25 prox, 0.20 elev | 0.800 | 0.348 | 0.388 |
| 0.50 hist, 0.35 prox, 0.10 elev | 0.800 | 0.348 | 0.427 |

What "tuning" does and does not do:
- Raising the historical weight to ~0.4–0.5 and boosting proximity improves the
  average (+0.03 precision, +0.14 recall) — because historical flood frequency
  is genuinely the best single predictor in this setup.
- **It cannot fix the structural weaknesses:** the control event stays at
  precision 0.0 under every searched config, and the top-5 output remains
  essentially static. No reweighting within this architecture adds the one
  thing that is missing — sensitivity to spatial rainfall/level distribution.
- Verdict: **changing the weights is not worth breaking the carefully
  established unit-test expectations for ~+0.03 precision here**; reworking the
  input contract to accept per-zone forcing is the higher-value change.

---

## 5. Kerala 2018 case study

Model scores: 14 districts, each with its CWC/IMD rainfall, proxied stage, and
IFI-derived historical risk. Ground truth: IFI says all 14 affected; CWC prose
says 13/14 (Kasaragod the mild outlier).

| rank | district | p(12h) | rainfall (mm) | hist | elev (m) |
|---|---:|--:|--:|--:|--:|
| 1 | Alappuzha | 0.626 | 1784 | 1.000 | 1 |
| 2 | Ernakulam | 0.598 | 2478 | 1.000 | 4 |
| 3 | Kasaragod | 0.598 | 2287 | 1.000 | 4 |
| 4 | Kannur | 0.554 | 2573 | 1.000 | 10 |
| 5 | Kozhikode | 0.549 | 2898 | 1.000 | 9 |
| 6 | Idukki | 0.536 | **3556** | 1.000 | 1050 |
| … | … | … | … | … | … |
| 14 | Wayanad | 0.455 | 2885 | 1.000 | 750 |

- precision@5 = **1.000**, recall@5 = **0.357** (5 of the 14 affected districts
  in the predicted top-5). Spearman is undefined (all labels are 1 — no spread).
- The precision@5 is **meaningless as praise**: with 14/14 (or 13/14) districts
  really affected, *any* top-5 is fully or almost fully "correct." What matters
  is the ordering, and the ordering exposes the model's weakness:
- **The two districts that were hit hardest by rainfall — Idukki (3,556 mm,
  highest in the state, all five Idukki Dam gates opened for the first time in
  26 years) and Wayanad (your writing: catastrophic landslides) — rank 6th and
  14th (last).** Why: the `elevation_factor = 1/(1+elev/10)` punishing mountain
  terrain. Kerala 2018's dominant failure mode was **catchment/reservoir flooding
  and hill-slope saturation in high terrain** — exactly what this static,
  "low ground floods" proxy cannot see.
- Conversely, the coastal low-lying backwater districts (Alappuzha, Ernakulam)
  rank first, and those did flood — so the model is not *wrong* there; it is
  just blind to the highland extreme-rainfall mechanism.

---

## 6. Limitations of this validation (read before trusting anything)

1. **River factor is proxied, not observed.** No public bulk gauge archive
   covers 1948–2013 Bangladesh; the river factor is a rainfall-percentile proxy.
   So the 0.35-weight `river` term was effectively tested as "rainfall exceedance
   proxy," not as real stage. Kerala did not escape this either (no per-district
   stage public record).
2. **Rainfall unit mismatch.** The function's reference (`200 mm` per 24 h) is
   fed monthly/cumulative totals. Ranking within an event is unaffected
   (constant term under uniform forcing), but absolute probabilities are not a
   calibrated probability of flooding — treat them as relative-risk scores.
3. **`population` is dead weight.** The scoring function loads it and never
   uses it. Any intuition that denser zones surface higher is false; the factor
   simply does nothing.
4. **approximate static inputs.** Distance-to-river uses hand-traced centerlines
   (±2–5 km); Kerala district elevation is approximate. Both affect only the
   relative static ordering, not the qualitative conclusions.
5. **Coarse Kerala labels.** 13–14 of 14 districts affected → precision@5
   carries no information for Kerala; only the ordering discussion is meaningful.
6. **Tuning is in-sample** (§4) and is not a held-out estimate.

---

## 7. Verdict and appropriate use

**What the model got right:** for broad monsoon floods in flat, riverine,
Bengal-style terrain, its static ranking (low elevation, near river, historically
floody) genuinely surfaces the perennially flooded zones — on the four big
multi-state events its top-5 was 100% or 80% real flood zones, and the spatial
variant pushed even localized-event precision to 0.8 with ρ up to 0.69.

**Where the model failed:** (a) it is spatially blind — same top-5 every event,
precision 0.0 on a quiet month, ~60% precision on a localized event; (b) it
ranked Idukki 6th and Wayanad 14th in the Kerala disaster — it cannot see
high-ground extreme-rainfall / reservoir-driven flooding; (c) it can never
distinguish "flood now" from "historically floody" because no threshold exists;
(d) recall is ~0.2 — it typically surfaces under a quarter of the zones that
actually flood.

**Measured, not claimed:**
- mean precision@5 = **0.771** (shipped) / **0.829** (spatial variant)
- mean recall@5 = **0.205** / **0.226**
- mean Spearman ρ = **0.286** / **0.394**
- Kerala 2018: precision@5 = 1.0 (uninformative), recall@5 = 0.357; worst-hit
  highland districts ranked 6th and 14th.

**Appropriate use (recommendation):** *Reasonable for relative risk ranking
within a region whose drainage and terrain resemble the validation set
(low-lying, riverine, monsoon-fed Bengal-type floodplains), and only when a
human treats the number as a relative score, not a calibrated probability.*

**Not validated / do not use for:** coastal surge (no surge or tide inputs),
flash flood in mountainous terrain (the elevation factor actively
mis-ranks highland catchments that drain extreme rainfall), regions with
different drainage characteristics, dam/reservoir-release flooding, or any
situation where a spatial answer ("which zones") rather than a historically
conditioned ranking is required. If the model is used operationally, the single
most valuable change is to let `rank_zones` accept per-zone rainfall and river
inputs; reweighting the four constants is, on this evidence, not worth it.