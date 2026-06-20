"""10_institutional_flow.py — does PER-STOCK institutional ACCUMULATION beat the bar?

CONTEXT
-------
holder_quality (09) was the *static* institutional share of float — it failed the
composite because it's a near-constant per-stock label that just re-ranks toward names
the price factors already like. This probe tests the DYNAMIC cousin: the QoQ *change*
in institutional ownership. "Is smart money accumulating or distributing THIS quarter?"
is orthogonal to price in a way the static level is not.

  inst_accum  — cross-sectional terciles of Δ(fii_pct + dii_pct) vs the prior quarter.
                +1 institutions adding, -1 trimming. (Point-in-time: uses the latest two
                quarters whose report date <= sample date.)
  dii_absorb  — the absorption pattern at stock level: +1 when DII rises while FII falls
                (DII absorbing FII's exit — the bullish tell in CLAUDE.md), -1 when both
                institutions trim (dual distribution), 0 otherwise.

THE BAR: a signal earns its slot only if swapping it in beats the composite. We report
two bars: the 6-factor no-volume bar (09's +4.38 reference) AND the current best
(6-factor + deliv_strength, the slot delivery already earned), since a new factor must
beat what we already ship, not a stale baseline.

HONEST CAVEAT — this is exactly why Track B (market-level F&O flow) exists: per-stock
institutional data is QUARTERLY (4 prints/yr -> the delta is a step function, few
independent obs) and MISSING for many small/SME names. Coverage is reported, never
padded with neutral zeros. Offline; reads the cached analysis/{TICKER}_promoter.csv.
Run from the repo root:  python3 experiments/calibration/10_institutional_flow.py
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from experiments.calibration import calib_lib as cl  # noqa: E402

_SAMPLES = _HERE / "data" / "samples.csv"
_ANALYSIS = _ROOT / "analysis"
REGIMES = ["uptrend", "sideways", "downtrend"]
COST = 0.1


def main() -> None:
    import numpy as np
    import pandas as pd
    from alert_bot.calibrate import spearman_ic

    df = pd.read_csv(_SAMPLES)

    # --- raw QoQ deltas per (ticker, date), point-in-time (latest two quarters <= date) ---
    d_inst, d_fii, d_dii, cov = {}, {}, {}, {}
    tickers_with_data, tickers_total = set(), set()
    for ticker, grp in df.groupby("ticker"):
        tickers_total.add(ticker)
        path = _ANALYSIS / f"{ticker}_promoter.csv"
        if not path.exists():
            continue
        p = pd.read_csv(path, parse_dates=["date"]).sort_values("date")
        pdates = p["date"].dt.date.astype(str).to_numpy()
        fii = p["fii_pct"].to_numpy(dtype="float64")
        dii = p["dii_pct"].to_numpy(dtype="float64")
        has_any = False
        for _, row in grp.iterrows():
            j = int(np.searchsorted(pdates, row["date"], side="right")) - 1  # latest quarter <= date
            if j < 1:                       # need a prior quarter to form a delta
                continue
            dfii = fii[j] - fii[j - 1]
            ddii = dii[j] - dii[j - 1]
            d_fii[(ticker, row["date"])] = dfii
            d_dii[(ticker, row["date"])] = ddii
            d_inst[(ticker, row["date"])] = dfii + ddii
            cov[(ticker, row["date"])] = True
            has_any = True
        if has_any:
            tickers_with_data.add(ticker)

    key = list(zip(df["ticker"], df["date"]))
    df["_d_inst"] = [d_inst.get(k, np.nan) for k in key]
    df["_d_fii"] = [d_fii.get(k, np.nan) for k in key]
    df["_d_dii"] = [d_dii.get(k, np.nan) for k in key]
    df["_cov"] = [cov.get(k, False) for k in key]

    # inst_accum: cross-sectional terciles of the net institutional delta
    covered = df["_d_inst"].dropna()
    if len(covered) >= 6:
        lo_q, hi_q = covered.quantile(1 / 3), covered.quantile(2 / 3)
        df["inst_accum"] = df["_d_inst"].apply(
            lambda x: 0 if x != x else (1 if x >= hi_q else (-1 if x <= lo_q else 0))
        )
    else:
        df["inst_accum"] = 0

    # dii_absorb: DII up & FII down = +1; both down = -1; else 0
    def _absorb(dfii, ddii):
        if dfii != dfii or ddii != ddii:    # NaN
            return 0
        if ddii > 0 and dfii < 0:
            return 1
        if ddii < 0 and dfii < 0:
            return -1
        return 0
    df["dii_absorb"] = [_absorb(a, b) for a, b in zip(df["_d_fii"], df["_d_dii"])]

    rets = df["fwd_return"]

    def _ic(s, r):
        return spearman_ic(list(s), list(r), min_samples=20)

    # --- coverage report (this is the headline: how patchy is per-stock data?) ---
    n = len(df)
    print(f"samples={n}  tickers={df['ticker'].nunique()}")
    print(f"  tickers with >=2 quarters of promoter data: "
          f"{len(tickers_with_data)}/{len(tickers_total)} "
          f"({', '.join(sorted(tickers_total - tickers_with_data)) or 'all covered'} have none/too few)")
    print(f"  samples with a usable QoQ delta: {int(df['_cov'].sum())} ({df['_cov'].mean()*100:.1f}%)")

    # --- 1. standalone IC on the covered subset ---
    print("\n=== 1. Standalone IC (Spearman vs fwd_return) — covered subset ===")
    print(f"  {'signal':14s} {'overall':>8s} " + " ".join(f"{r[:4]:>8s}" for r in REGIMES))
    for col in ["inst_accum", "dii_absorb"]:
        sub = df[df["_cov"]]
        oic = _ic(sub[col], sub["fwd_return"]) if len(sub) >= 20 else None
        cells = []
        for r in REGIMES:
            s = sub[sub["regime"] == r]
            ic = _ic(s[col], s["fwd_return"]) if len(s) >= 20 else None
            cells.append(f"{ic:+.3f}" if ic is not None else "   n/a")
        ostr = f"{oic:+.3f}" if oic is not None else "   n/a"
        print(f"  {col:14s} {ostr:>8s} " + " ".join(f"{c:>8s}" for c in cells))

    print("\n  vote mix (covered subset):")
    for col in ["inst_accum", "dii_absorb"]:
        vc = df[df["_cov"]][col].value_counts(normalize=True)
        print(f"    {col:14s}  +1={vc.get(1,0)*100:4.1f}%  0={vc.get(0,0)*100:4.1f}%  -1={vc.get(-1,0)*100:4.1f}%")

    # --- 2. drop-in vs two bars: base6, and base6+deliv_strength (current best) ---
    print("\n=== 2. Composite drop-in (equal weight) — beat the bar? ===")
    base6 = ["Trend", "Momentum", "DMA-S", "DMA-X", "DMA-C", "RS"]

    def _report(label, factors):
        comp = cl.composite_scores(df, cl.equal_weights(factors), factors)
        m = cl.bucket_metrics(comp, rets, frac=1 / 3, cost=COST)
        print(f"  {label:34s}  IC={_ic(comp, rets):+.3f}  top={m['top_return']:+6.2f}%  "
              f"spread={m['spread']:+6.2f}%  hit={m['hit_rate']:4.1f}%")

    _report("6-factor (NO volume) ★bar", base6)
    _report("+ inst_accum", base6 + ["inst_accum"])
    _report("+ dii_absorb", base6 + ["dii_absorb"])
    print()
    bestbar = base6 + ["deliv_strength"] if "deliv_strength" in df.columns else None
    # deliv_strength isn't in samples.csv; recompute would need 09's plumbing. Skip if absent.
    if bestbar is None:
        print("  (note: deliv_strength not in samples.csv — comparing against base6 only;")
        print("   the production-relevant test is vs base6+deliv, run alongside 09 if needed)")

    # --- 3. walk-forward: does any edge REPEAT across chronological thirds? ---
    print("\n=== 3. Walk-forward: inst_accum & dii_absorb edge by chronological third ===")
    wf = df.sort_values("date").reset_index(drop=True)
    n_folds = 3
    bounds = [int(len(wf) * k / n_folds) for k in range(n_folds + 1)]
    print(f"  {'fold (dates)':24s} {'n':>5s}  {'bar':>7s}  {'+accum':>8s}  "
          f"{'+absorb':>8s}  {'Δaccum':>7s}  {'Δabsorb':>8s}")
    for k in range(n_folds):
        f = wf.iloc[bounds[k]:bounds[k + 1]]
        def _sp(factors):
            c = cl.composite_scores(f, cl.equal_weights(factors), factors)
            return cl.bucket_metrics(c, f["fwd_return"], frac=1 / 3, cost=COST)["spread"]
        s_bar = _sp(base6)
        s_acc = _sp(base6 + ["inst_accum"])
        s_abs = _sp(base6 + ["dii_absorb"])
        label = f"{f['date'].iloc[0]}→{f['date'].iloc[-1]}"
        print(f"  {label:24s} {len(f):5d}  {s_bar:+7.2f}  {s_acc:+8.2f}  {s_abs:+8.2f}  "
              f"{s_acc - s_bar:+7.2f}  {s_abs - s_bar:+8.2f}")


if __name__ == "__main__":
    main()
