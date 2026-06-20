# datasets/ — persistent research dataset store

A SQLite DB (`datasets.db`) of important persistent datasets, kept separate from production
`data/*.db`. It's the queryable feature store the `experiments/` read from. "More will come" — each
new dataset is one table.

## Tables
- **`mmi`** `(date PK, value, nifty, zone)` — daily Market Mood Index, **2012→2026**, plus the Nifty
  close that day (rides along; useful for RS/regime later). `zone` bands match
  `alert_bot/confidence.py` (<30 Extreme Fear · <50 Fear · <70 Greed · ≥70 Extreme Greed).
  Source: `datasets/mmi/MMI_*.csv` (TickerTape export; `Date` is DD/MM/YYYY).
  Ingest: `python3 datasets/ingest_mmi.py` (idempotent — upsert by date, newest file wins).
- **`factor_snapshots`** `(date PK, mmi, mmi_zone, vix, vix_regime, pcr, pcr_regime, flow_regime,
  breadth_zone, breadth_score, fii_net_cr, dii_net_cr, captured_at)` — a daily point-in-time capture
  of the **live-only** contextual factors (MMI/VIX/flow/breadth/FII-DII) that production computes but
  never archives. Built by `datasets/snapshot_factors.py` (read-only on the bot's `data/*.json`),
  run **nightly in parallel** via `run_forward_test.sh`. Raw continuous values (not coarse votes) so
  features can be modelled richly later. One row/day; the series only grows **going forward**.

## Add a dataset
Drop the raw file under `datasets/<name>/`, then write a small ingest script: `CREATE TABLE`, parse
rows, and call `datasets_db.connect()` + `datasets_db.upsert(con, table, rows, key)`.

## Query
`sqlite3 datasets/datasets.db "select … from mmi"`, or `datasets_db.connect()` from Python.

`datasets.db` is **regenerable** from the source files → gitignore it; commit the raw inputs + scripts.
