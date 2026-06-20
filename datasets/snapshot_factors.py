#!/usr/bin/env python3
"""
datasets/snapshot_factors.py — the parallel data-gathering system.

Five of Sable's confidence factors are LIVE-ONLY with no archive (MMI, VIX, flow regime,
breadth, FII/DII) — every day we don't record them, that point-in-time value is lost forever
and can never be reconstructed for ML. This snapshots the values production already computes,
once per day, into datasets.db `factor_snapshots` — a clean, point-in-time time series the
auto-learner needs.

Read-only on production (reads the JSON files the bot writes); runs in PARALLEL to the live
loops and cannot affect them. Idempotent: upsert by date (re-run same day just refreshes).
Captures the RAW continuous values (not the coarse votes) so features can be modelled richly
later — continuous MMI/VIX/breadth/flow, not just fear/greed bands.

Usage:  python3 datasets/snapshot_factors.py
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datasets_db as ddb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

SCHEMA = """
CREATE TABLE IF NOT EXISTS factor_snapshots (
    date         TEXT PRIMARY KEY,    -- ISO YYYY-MM-DD (capture date)
    mmi          REAL, mmi_zone   TEXT,
    vix          REAL, vix_regime TEXT,
    pcr          REAL, pcr_regime TEXT,
    flow_regime  TEXT,
    breadth_zone TEXT, breadth_score REAL,
    fii_net_cr   REAL, dii_net_cr REAL,
    captured_at  TEXT
)
"""


def _load(name: str) -> dict:
    try:
        return json.loads((DATA / name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get(d: dict, *path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
        if d is None:
            return None
    return d


def snapshot() -> dict:
    state = _load("state.json")
    fno = _load("fno_signals.json")
    flow = _load("flow_regime.json")
    breadth = _load("breadth.json")
    macro = _load("macro_signals.json")
    return {
        "date": dt.date.today().isoformat(),
        "mmi": _get(state, "mmi", "last_value"), "mmi_zone": _get(state, "mmi", "last_zone"),
        "vix": _get(fno, "vix", "value"), "vix_regime": _get(fno, "vix", "regime"),
        "pcr": _get(fno, "pcr", "value"), "pcr_regime": _get(fno, "pcr", "regime"),
        "flow_regime": _get(flow, "regime"),
        "breadth_zone": _get(breadth, "zone"), "breadth_score": _get(breadth, "composite_score"),
        "fii_net_cr": _get(macro, "fii_dii", "fii_net_cr"), "dii_net_cr": _get(macro, "fii_dii", "dii_net_cr"),
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


MIRROR = Path(__file__).resolve().parent / "factor_snapshots.jsonl"  # git-committed source of truth


def main():
    row = snapshot()
    con = ddb.connect()
    con.execute(SCHEMA)
    ddb.upsert(con, "factor_snapshots", [row], key="date")
    # Mirror the full table to a committed JSONL — these daily captures are NOT regenerable,
    # so they must live in git, not only in the gitignored datasets.db cache.
    cols = [d[0] for d in con.execute("SELECT * FROM factor_snapshots LIMIT 0").description]
    rows = [dict(zip(cols, r)) for r in con.execute("SELECT * FROM factor_snapshots ORDER BY date")]
    MIRROR.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    n, lo, hi = con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM factor_snapshots").fetchone()
    con.close()
    captured = ", ".join(f"{k}={row[k]}" for k in ("mmi", "vix", "flow_regime", "breadth_zone") if row[k] is not None)
    print(f"factor_snapshots ← {row['date']}  ({captured})")
    print(f"  → datasets.db  ·  {n} day(s)  ·  {lo} → {hi}")


if __name__ == "__main__":
    main()
