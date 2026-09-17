# Data sources and licensing

This file states, flatly, where every raw dataset used by the AEGIS backtest
comes from, what licence it is under, and how to reproduce the backtest. The
two datasets are **not committed to this repo** — they live in
`backend/validation/data/` as local cached files only (see `.gitignore`).

## Reproducing this backtest

`backtest.py` auto-downloads the two raw files into `backend/validation/data/`
on first run (via `ensure_data()`). A **live network connection is required on
a fresh clone**. If a download fails — offline, or the server returns an HTML
error page or an empty/truncated body — `backtest.py` fails loudly with a
message pointing at this file; a bad download is never left on disk to be
mistaken for valid data.

To reproduce:

```
cd backend/validation
python backtest.py                       # auto-downloads both files
python backtest.py --spatial --tune      # reproduces the reported numbers
```

`RESULTS.md` contains the full output of the run already performed — it does
**not** require re-running the backtest to be read or trusted.

## Datasets

### Primary — Bangladesh weather-station flood dataset

- **File:** `backend/validation/data/bangladesh_stations.csv`
- **Auto-downloaded from:** https://github.com/n-gauhar/Flood-prediction
  (`FloodPrediction.csv` on `master`)
- **Attribution & license:** Gauhar, N., Das, S., Moury, K.S. (2021). *Prediction of Flood in Bangladesh using k-Nearest Neighbors Algorithm.* IEEE ICREST 2021, pp. 357–361. Dataset — github.com/n-gauhar/Flood-prediction — **no license file; default copyright; local use only with citation**.

**License note:** the source repository has **no LICENSE file**, so the data
carries default copyright with no explicit redistribution grant. That is why the
file is kept **local-only** (gitignored) rather than committed. The author
README requests the citation above; attribution is provided here and in
`RESULTS.md`.

### Secondary — India Flood Inventory (IFI) v3

- **File:** `backend/validation/data/india_flood_inventory_v3.csv`
- **Auto-downloaded from:** https://zenodo.org/records/16994648
- **Attribution & license:** Saharia, M., Jain, A., Baishya, R.R., Haobam, S., Sreejith, O.P., Pai, D.S., Rafieeinasab, A. (2021). *India flood inventory: creation of a multi-source national geospatial database to facilitate comprehensive flood research.* Natural Hazards. DOI 10.1007/s11069-021-04698-6. Dataset v3 — Zenodo 10.5281/zenodo.16994648 — license **CC-BY-NC 4.0** (attribution required, non-commercial only).

**License note:** the Zenodo record metadata declares **CC-BY-NC 4.0**:
attribution is required for derived works, and **commercial use is not
permitted**. This is a real constraint on how AEGIS, or any fork of it, may be
used downstream — the derived numbers published in `RESULTS.md` count as
derived work, so any commercial deployment must not build on them without a
separate licence.