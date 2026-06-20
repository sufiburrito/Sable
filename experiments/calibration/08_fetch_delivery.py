"""08_fetch_delivery.py — fetch & cache NSE delivery-% history for the sample tickers.

WHY (and why this is the ONE networked script in the sandbox)
------------------------------------------------------------
Raw volume is a confirmed null (probes 04/05/06). Delivery % — the share of a day's
traded volume actually taken to demat vs. squared off intraday — is a *different*
quantity: it measures conviction, not turnover. This script pulls it so 09 can test
whether it has the edge raw volume lacked. Everything else in this sandbox is offline;
this is the deliberate exception, gated on explicit user approval.

Source: nselib's `price_volume_and_deliverable_position_data(symbol, from, to)` — the
per-symbol daily history that carries `%DlyQttoTradedQty`. One request per ticker per
calendar year (the NSE endpoint caps long ranges), with a polite delay. Cached to
`data/delivery/{TICKER}.csv`; re-running SKIPS tickers already cached (idempotent), so
a partial/interrupted fetch resumes cleanly.

Honest expectation: NSE serves recent history reliably and older history patchily, and
the two ETFs (ITBEES, NIF100BEES) have no meaningful delivery — coverage will be PARTIAL.
That gap is itself a finding; 09 reports it rather than papering over it.

Run from the repo root:  python3 experiments/calibration/08_fetch_delivery.py
"""
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SAMPLES = _HERE / "data" / "samples.csv"
_OUT = _HERE / "data" / "delivery"
DELAY_S = 1.5            # polite gap between NSE requests


def _fetch_ticker(symbol: str, year_lo: int, year_hi: int):
    """Year-chunked pull of (date, close, deliv_pct) for one symbol. None on total fail."""
    import pandas as pd
    from nselib import capital_market as cm

    frames = []
    for yr in range(year_lo, year_hi + 1):
        frm, to = f"01-01-{yr}", f"31-12-{yr}"
        try:
            raw = cm.price_volume_and_deliverable_position_data(
                symbol, from_date=frm, to_date=to
            )
        except Exception as e:
            print(f"    {symbol} {yr}: {type(e).__name__} — {str(e)[:60]}")
            time.sleep(DELAY_S)
            continue
        if raw is None or len(raw) == 0:
            time.sleep(DELAY_S)
            continue
        # The Symbol column carries a BOM ('ï»¿"Symbol"'); we only need 3 fields.
        cols = {c.strip().strip('"'): c for c in raw.columns}
        date_c = cols.get("Date")
        close_c = cols.get("ClosePrice")
        deliv_c = cols.get("%DlyQttoTradedQty")
        if not (date_c and close_c and deliv_c):
            print(f"    {symbol} {yr}: schema drift — have {list(raw.columns)[:4]}...")
            time.sleep(DELAY_S)
            continue
        part = pd.DataFrame({
            "date": pd.to_datetime(raw[date_c], format="%d-%b-%Y", errors="coerce"),
            "close": pd.to_numeric(raw[close_c].astype(str).str.replace(",", ""), errors="coerce"),
            "deliv_pct": pd.to_numeric(raw[deliv_c], errors="coerce"),
        }).dropna(subset=["date"])
        frames.append(part)
        print(f"    {symbol} {yr}: {len(part)} rows")
        time.sleep(DELAY_S)

    if not frames:
        return None
    out = pd.concat(frames).drop_duplicates("date").sort_values("date")
    return out


def main() -> None:
    import pandas as pd

    df = pd.read_csv(_SAMPLES)
    tickers = sorted(df["ticker"].unique())
    lo = pd.to_datetime(df["date"].min()).year
    hi = pd.to_datetime(df["date"].max()).year
    _OUT.mkdir(parents=True, exist_ok=True)
    print(f"fetching delivery % for {len(tickers)} tickers, {lo}-{hi} -> {_OUT}")

    fetched, skipped, failed = 0, 0, []
    for tk in tickers:
        dest = _OUT / f"{tk}.csv"
        if dest.exists():
            skipped += 1
            print(f"  {tk}: cached, skip")
            continue
        print(f"  {tk}: fetching...")
        out = _fetch_ticker(tk, lo, hi)
        if out is None or out.empty:
            failed.append(tk)
            print(f"  {tk}: NO DATA")
            continue
        out.to_csv(dest, index=False)
        fetched += 1
        print(f"  {tk}: saved {len(out)} rows  {out['date'].min().date()} -> {out['date'].max().date()}")

    print(f"\ndone: {fetched} fetched, {skipped} cached, {len(failed)} failed"
          + (f" ({', '.join(failed)})" if failed else ""))


if __name__ == "__main__":
    main()
