"""
00_backfill_nifty.py — one-time NIFTY50 history backfill for the calibration bake-off.

WHY THIS EXISTS
---------------
The live alert bot keeps a NIFTY50 OHLC cache at analysis/NIFTY50_ohlc_cache.csv,
but it only ever fetches the last 2 YEARS (alert_bot/main.py:282) and OVERWRITES
that file every market open. Our calibration experiment needs the FULL ~5 years of
Nifty history (so Relative-Strength and regime can be measured across the 2023 melt-up,
the 2024-25 correction, and the 2025 recovery — not just one regime).

If we backfilled the live file, the bot would clobber it back to 2 years at the next
market open. So this script writes a SEPARATE file the live bot never reads or touches:

    experiments/calibration/data/NIFTY50_5y.csv

The format matches what the rest of the system expects: a Date-indexed CSV with
Open/High/Low/Close/Volume columns (same shape alert_bot/ohlc_cache.read_ohlc_cache reads).

This is a throwaway experiment script. It is NOT imported by alert_bot/ and changes
no production code. Run it once:

    python3 experiments/calibration/00_backfill_nifty.py
"""
from pathlib import Path

import yfinance as yf  # the same library the live bot uses to fetch prices

# Where we write the long history. Resolve relative to THIS file so it works
# regardless of the current working directory.
OUT = Path(__file__).resolve().parent / "data" / "NIFTY50_5y.csv"

# "^NSEI" is Yahoo Finance's symbol for the Nifty 50 index — identical to what the
# live bot uses (alert_bot/main.py:282, alert_bot/breadth_score.py:247).
SYMBOL = "^NSEI"


def main() -> None:
    # period="5y" asks Yahoo for ~5 years of daily bars. We mirror the live bot's
    # download call exactly (same symbol, same default adjustment) and only widen
    # the period from "2y" to "5y", so the cached values stay consistent with what
    # the bot would have produced.
    df = yf.download(SYMBOL, period="5y", progress=False)

    if df is None or len(df) == 0:
        # yfinance returns an empty frame on a failed/blocked fetch. Fail loudly so
        # we don't silently proceed with no data.
        raise SystemExit("NIFTY50 backfill: no data returned from yfinance (network?)")

    # Recent yfinance versions return a MultiIndex on columns when one ticker is
    # requested (e.g. ('Close', '^NSEI')). Flatten to the plain 'Close' style the
    # rest of the codebase expects — the same flattening the live refresh does.
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT)  # writes the Date index as the first column

    # A short, human-readable confirmation so we can eyeball the span we just pulled.
    first = df.index[0].date()
    last = df.index[-1].date()
    print(f"Wrote {OUT}")
    print(f"  rows={len(df)}  span={first} -> {last}  columns={list(df.columns)}")


if __name__ == "__main__":
    main()
