#!/usr/bin/env python3
"""
datasets/ingest_mmi.py — load the historical MMI CSV into datasets.db (table `mmi`).

Source: datasets/mmi/MMI_*.csv  (Date DD/MM/YYYY, Market Mood Index 0-100, Nifty Index).
Idempotent: re-run any time; rows upsert by date, newest file wins.

Usage:  python3 datasets/ingest_mmi.py
"""
import csv
import datetime as dt
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datasets_db as ddb  # noqa: E402  (same-directory module)

SRC_GLOB = str(Path(__file__).resolve().parent / "mmi" / "MMI_*.csv")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mmi (
    date  TEXT PRIMARY KEY,   -- ISO YYYY-MM-DD
    value REAL,               -- Market Mood Index, 0-100
    nifty REAL,               -- Nifty Index close that day (rides along, useful later)
    zone  TEXT                -- derived band (matches alert_bot/confidence.py)
)
"""


def zone_for(v: float) -> str:
    return ("Extreme Fear" if v < 30 else "Fear" if v < 50
            else "Greed" if v < 70 else "Extreme Greed")


def parse_rows(path: str) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            r = {k.strip(): (v.strip() if v else v) for k, v in r.items()}
            try:
                d = dt.datetime.strptime(r["Date"], "%d/%m/%Y").date().isoformat()
                val = float(r["Market Mood Index"])
            except (ValueError, KeyError, TypeError):
                continue
            nifty = None
            try:
                nifty = float(r["Nifty Index"])
            except (ValueError, KeyError, TypeError):
                pass
            rows.append({"date": d, "value": round(val, 4), "nifty": nifty, "zone": zone_for(val)})
    return rows


def main():
    files = sorted(glob.glob(SRC_GLOB))
    if not files:
        print(f"No MMI CSV found at {SRC_GLOB}")
        return
    con = ddb.connect()
    con.execute(SCHEMA)
    total = 0
    for path in files:                       # oldest first → newest file's values win on conflict
        rows = parse_rows(path)
        total += ddb.upsert(con, "mmi", rows, key="date")
        print(f"  {Path(path).name}: {len(rows)} rows")
    n, lo, hi = con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM mmi").fetchone()
    con.close()
    print(f"mmi table → {ddb.DB}  ·  {n} rows  ·  {lo} → {hi}  ({total} upserted)")


if __name__ == "__main__":
    main()
