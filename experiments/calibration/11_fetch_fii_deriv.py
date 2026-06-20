"""11_fetch_fii_deriv.py — fetch & cache NSE participant-wise F&O open interest (Track B).

WHY (the SECOND networked script in the sandbox, explicitly approved)
---------------------------------------------------------------------
Cash FII/DII flow is today-only (no history) — see CLAUDE.md / nselib's
`fii_dii_trading_activity()`. The participant-wise F&O open-interest report, by
contrast, IS dated and archived back to early 2022. It carries FII/DII positioning
in index & stock futures and options — a MARKET-LEVEL signal that joins to every
sample by date (unlike per-stock holdings, which are blank for half our names).

This pulls one archive file per trading day via
`nselib.derivatives.participant_wise_open_interest(trade_date='dd-mm-YYYY')` and
flattens the FII + DII rows into a single date-indexed CSV. Idempotent: re-running
SKIPS dates already cached, and progress is flushed every FLUSH_EVERY dates, so an
interrupted crawl resumes cleanly.

HONEST CAVEAT (carried into 12's analysis): FIIs run large cash books and use index
futures largely to HEDGE, so "FII net-short index futures" is often a hedge, not a
directional bear bet — the long/short ratio is a NOISY sentiment proxy and may test
null like raw volume did. We fetch it to find out, not because the edge is assumed.

Trading-day calendar = the dates in NIFTY50_5y.csv (real sessions; avoids wasting
requests on holidays). Run from the repo root:
    python3 experiments/calibration/11_fetch_fii_deriv.py
"""
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_NIFTY = _HERE / "data" / "NIFTY50_5y.csv"
_OUT = _HERE / "data" / "fii_deriv" / "participant_oi.csv"
LO, HI = "2021-11-01", "2025-05-30"   # ~3-month lead before first sample -> last sample
DELAY_S = 1.2
FLUSH_EVERY = 25

# (output column, stripped source column) — source names carry stray spaces/BOM.
_FIELDS = [
    ("future_index_long", "Future Index Long"),
    ("future_index_short", "Future Index Short"),
    ("future_stock_long", "Future Stock Long"),
    ("future_stock_short", "Future Stock Short"),
    ("option_index_call_long", "Option Index Call Long"),
    ("option_index_put_long", "Option Index Put Long"),
    ("option_index_call_short", "Option Index Call Short"),
    ("option_index_put_short", "Option Index Put Short"),
    ("total_long", "Total Long Contracts"),
    ("total_short", "Total Short Contracts"),
]


def _flatten(raw):
    """One participant-OI dataframe -> flat dict of fii_* / dii_* fields. None if rows missing."""
    import pandas as pd

    colmap = {str(c).strip(): c for c in raw.columns}
    ct = colmap.get("Client Type")
    if ct is None:
        return None
    raw = raw.copy()
    raw["_ct"] = raw[ct].astype(str).str.strip()
    out = {}
    for who in ("FII", "DII"):
        rows = raw[raw["_ct"] == who]
        if rows.empty:
            return None
        r = rows.iloc[0]
        for out_name, src in _FIELDS:
            col = colmap.get(src)
            val = pd.to_numeric(str(r[col]).replace(",", ""), errors="coerce") if col else None
            out[f"{who.lower()}_{out_name}"] = val
    return out


def main() -> None:
    import pandas as pd
    from nselib import derivatives as der

    nifty = pd.read_csv(_NIFTY)
    dates = pd.to_datetime(nifty["Date"])
    sessions = dates[(dates >= LO) & (dates <= HI)].dt.date.astype(str).tolist()

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    existing = []
    if _OUT.exists():
        prev = pd.read_csv(_OUT)
        done = set(prev["date"].astype(str))
        existing = prev.to_dict("records")
        print(f"resume: {len(done)} dates already cached")

    todo = [d for d in sessions if d not in done]
    print(f"sessions {LO}..{HI}: {len(sessions)} total, {len(todo)} to fetch -> {_OUT}")

    rows = list(existing)
    fetched, failed = 0, 0

    def _flush():
        df = pd.DataFrame(rows).drop_duplicates("date").sort_values("date")
        df.to_csv(_OUT, index=False)

    for i, d in enumerate(todo, 1):
        td = pd.to_datetime(d).strftime("%d-%m-%Y")
        try:
            raw = der.participant_wise_open_interest(trade_date=td)
        except Exception as e:
            failed += 1
            print(f"  {d}: {type(e).__name__} — {str(e)[:50]}")
            time.sleep(DELAY_S)
            continue
        flat = _flatten(raw)
        if flat is None:
            failed += 1
            print(f"  {d}: no FII/DII rows")
            time.sleep(DELAY_S)
            continue
        flat["date"] = d
        rows.append(flat)
        fetched += 1
        if fetched % FLUSH_EVERY == 0:
            _flush()
            print(f"  ...{fetched} fetched ({i}/{len(todo)}), last={d} "
                  f"fii_fut_idx L/S={flat['fii_future_index_long']:.0f}/{flat['fii_future_index_short']:.0f}")
        time.sleep(DELAY_S)

    _flush()
    print(f"\ndone: {fetched} fetched, {failed} failed, {len(rows)} total rows -> {_OUT}")


if __name__ == "__main__":
    main()
