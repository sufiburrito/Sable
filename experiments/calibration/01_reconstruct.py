"""01_reconstruct.py — build the regime-tagged calibration sample table.

WHAT THIS DOES
--------------
Walks every watchlist ticker's cached OHLC history and, at each past date,
re-scores the 7 price-derivable confidence factors *as of that date* (no
look-ahead), tags the date with the market regime (from the backfilled 5-year
NIFTY benchmark's trailing return), and pairs it with the realized blended
63/126/252-day forward return. The result is one tidy CSV — the input every
weighting method in the bake-off (02_bakeoff.py) reads.

This widens the seed window from the production engine's ~9 months (pinned by
the live 2y NIFTY cache) to ~4 years, because it reads the SEPARATE 5-year
benchmark file the live bot never touches.

Pure offline: cached CSVs only, zero network. Run from the repo root:

    python3 experiments/calibration/01_reconstruct.py
"""
import sys
from pathlib import Path

# Resolve everything off THIS file / the repo root so CWD doesn't matter.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent

# Run-as-a-file puts THIS dir on sys.path, not the repo root — so `experiments`
# and `alert_bot` wouldn't import. Put the repo root first.
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from experiments.calibration import calib_lib as cl  # noqa: E402

_ANALYSIS_DIR = _ROOT / "analysis"
_NIFTY_5Y = _HERE / "data" / "NIFTY50_5y.csv"
_OUT = _HERE / "data" / "samples.csv"


def main() -> None:
    if not _NIFTY_5Y.exists():
        raise SystemExit(
            f"missing {_NIFTY_5Y} — run 00_backfill_nifty.py first to fetch the 5y benchmark"
        )

    # The watchlist universe = one .md per tracked ticker (same source the
    # production engine reconstructs over).
    from alert_bot.calibrate import _watchlist_tickers
    tickers = _watchlist_tickers()

    nifty = cl.load_benchmark(_NIFTY_5Y)
    print(f"benchmark: {len(nifty)} bars  {nifty.index[0].date()} -> {nifty.index[-1].date()}")
    print(f"universe:  {len(tickers)} tickers")

    table = cl.reconstruct_table(tickers, nifty, _ANALYSIS_DIR)

    if table.empty:
        raise SystemExit("no samples reconstructed — are the OHLC caches present?")

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(_OUT, index=False)

    # Human-readable confirmation: total samples, span, and the regime mix (the
    # whole point of the stratification — show it's not one flat regime).
    print(f"\nwrote {_OUT}")
    print(f"  samples={len(table)}  tickers={table['ticker'].nunique()}"
          f"  span={table['date'].min()} -> {table['date'].max()}")
    print("  regime mix:")
    for regime, count in table["regime"].value_counts().items():
        print(f"    {regime:10s} {count:6d}  ({count / len(table) * 100:4.1f}%)")


if __name__ == "__main__":
    main()
