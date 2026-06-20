"""09_delivery_factor.py — does DELIVERY % (and holder quality) beat 'no volume'?

CONTEXT
-------
Raw volume is a confirmed null (04/05/06). This probe tests two Indian-specific
signals raw volume can't see, against the same bar: the 6-factor NO-volume composite
(05's +4.38% spread).

  deliv_strength  — delivery % = share of a day's volume taken to demat (conviction,
                    not churn). Scored point-in-time: recent delivery vs its own
                    trailing-60d percentile. +1 conviction rising / -1 churn rising.
  holder_quality  — WHO owns the float: (FII+DII)/(FII+DII+public) from the quarterly
                    shareholding pattern, as-of each sample date. Institution-heavy =
                    sticky hands (+1); retail-heavy = weak hands that sell on first
                    profit (-1) — the trader's-friend red flag. Cross-sectional terciles.
  deliv_adj       — the INTERACTION: delivery's bullish credit is clamped to <=0 in
                    retail-heavy names. "A delivery spike doesn't count as accumulation
                    if weak hands own the float." (Avoids the sign artifact a raw
                    delivery*holder product would create on the low-delivery/retail cell.)

THE BAR: a signal earns its slot only if swapping it into the composite beats the
6-factor no-volume composite (+4.38% spread). Coverage is PARTIAL (NSE delivery
history is shallow; promoter CSVs start ~2023-06) — reported honestly; standalone IC
is measured on the covered subset, never padded with neutral zeros.

Point-in-time, offline (reads the cached delivery/ + promoter CSVs that 08 and
fetch_promoter.py produced). Run from the repo root:
    python3 experiments/calibration/09_delivery_factor.py
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from experiments.calibration import calib_lib as cl  # noqa: E402

_SAMPLES = _HERE / "data" / "samples.csv"
_DELIVERY = _HERE / "data" / "delivery"
_ANALYSIS = _ROOT / "analysis"
REGIMES = ["uptrend", "sideways", "downtrend"]
COST = 0.1
DWIN = 60          # delivery trailing window
DMIN = 20          # min delivery obs before we'll score


def _deliv_score(deliv_vals):
    """Recent delivery vs its own trailing-DWIN percentile. (score, covered)."""
    import numpy as np
    if len(deliv_vals) < DMIN:
        return 0, False
    hist = deliv_vals[-DWIN:]
    recent = float(np.mean(deliv_vals[-5:]))
    pct = float((hist < recent).mean())
    if pct >= 0.67:
        return 1, True
    if pct <= 0.33:
        return -1, True
    return 0, True


def main() -> None:
    import numpy as np
    import pandas as pd
    from alert_bot.calibrate import spearman_ic

    df = pd.read_csv(_SAMPLES)

    # --- 1. delivery score per (ticker, date), point-in-time from cached CSVs ---
    dlv, dlv_cov = {}, {}
    for ticker, grp in df.groupby("ticker"):
        path = _DELIVERY / f"{ticker}.csv"
        if not path.exists():
            continue
        d = pd.read_csv(path, parse_dates=["date"]).sort_values("date")
        dates = d["date"].dt.date.astype(str).to_numpy()
        dvals = d["deliv_pct"].to_numpy(dtype="float64")
        for _, row in grp.iterrows():
            j = int(np.searchsorted(dates, row["date"], side="right"))   # rows <= sample date
            s, cov = _deliv_score(dvals[:j])
            dlv[(ticker, row["date"])] = s
            dlv_cov[(ticker, row["date"])] = cov

    # --- 2. holder-quality ratio per (ticker, date), as-of the latest quarter <= date ---
    hq_ratio, hq_cov = {}, {}
    for ticker, grp in df.groupby("ticker"):
        path = _ANALYSIS / f"{ticker}_promoter.csv"
        if not path.exists():
            continue
        p = pd.read_csv(path, parse_dates=["date"]).sort_values("date")
        pdates = p["date"].dt.date.astype(str).to_numpy()
        fii = p["fii_pct"].to_numpy(dtype="float64")
        dii = p["dii_pct"].to_numpy(dtype="float64")
        pub = p["public_pct"].to_numpy(dtype="float64")
        for _, row in grp.iterrows():
            j = int(np.searchsorted(pdates, row["date"], side="right")) - 1   # latest quarter <= date
            if j < 0:
                continue
            denom = fii[j] + dii[j] + pub[j]
            if denom <= 0:
                continue
            hq_ratio[(ticker, row["date"])] = (fii[j] + dii[j]) / denom
            hq_cov[(ticker, row["date"])] = True

    key = list(zip(df["ticker"], df["date"]))
    df["deliv_strength"] = [dlv.get(k, 0) for k in key]
    df["_dlv_cov"] = [dlv_cov.get(k, False) for k in key]
    df["_hq_raw"] = [hq_ratio.get(k, np.nan) for k in key]
    df["_hq_cov"] = [hq_cov.get(k, False) for k in key]

    # holder_quality: cross-sectional terciles of the institutional-float ratio
    covered = df["_hq_raw"].dropna()
    if len(covered) >= 6:
        lo_q, hi_q = covered.quantile(1 / 3), covered.quantile(2 / 3)
        df["holder_quality"] = df["_hq_raw"].apply(
            lambda r: 0 if r != r else (1 if r >= hi_q else (-1 if r <= lo_q else 0))
        )
    else:
        df["holder_quality"] = 0

    # deliv_adj: clamp delivery's bullish credit to <=0 where the float is retail-heavy
    df["deliv_adj"] = [
        min(d, 0) if h == -1 else d
        for d, h in zip(df["deliv_strength"], df["holder_quality"])
    ]

    rets = df["fwd_return"]

    def _ic(s, r):
        return spearman_ic(list(s), list(r), min_samples=20)

    # --- coverage report ---
    n = len(df)
    print(f"samples={n}  tickers={df['ticker'].nunique()}")
    print(f"  delivery covered:      {int(df['_dlv_cov'].sum()):5d}  ({df['_dlv_cov'].mean()*100:4.1f}%)")
    print(f"  holder-quality covered:{int(df['_hq_cov'].sum()):5d}  ({df['_hq_cov'].mean()*100:4.1f}%)")
    both = (df["_dlv_cov"] & df["_hq_cov"])
    print(f"  both covered:          {int(both.sum()):5d}  ({both.mean()*100:4.1f}%)")

    # --- 1. standalone IC on the COVERED subset (no padding with neutral zeros) ---
    print("\n=== 1. Standalone IC (Spearman vs fwd_return) — covered subset ===")
    print(f"  {'signal':16s} {'overall':>8s} " + " ".join(f"{r[:4]:>8s}" for r in REGIMES))
    specs = [("deliv_strength", df["_dlv_cov"]),
             ("holder_quality", df["_hq_cov"]),
             ("deliv_adj", both)]
    for col, covmask in specs:
        sub_all = df[covmask]
        oic = _ic(sub_all[col], sub_all["fwd_return"]) if len(sub_all) >= 20 else None
        cells = []
        for r in REGIMES:
            s = sub_all[sub_all["regime"] == r]
            ic = _ic(s[col], s["fwd_return"]) if len(s) >= 20 else None
            cells.append(f"{ic:+.3f}" if ic is not None else "   n/a")
        ostr = f"{oic:+.3f}" if oic is not None else "   n/a"
        print(f"  {col:16s} {ostr:>8s} " + " ".join(f"{c:>8s}" for c in cells))

    print("\n  vote mix (covered subset):")
    for col, covmask in specs:
        vc = df[covmask][col].value_counts(normalize=True)
        print(f"    {col:16s}  +1={vc.get(1,0)*100:4.1f}%  0={vc.get(0,0)*100:4.1f}%  -1={vc.get(-1,0)*100:4.1f}%")

    # --- 2. drop-IN: swap each into the 6-factor no-volume composite ---
    print("\n=== 2. Composite drop-in (equal weight) — beat the no-volume bar? ===")
    base6 = ["Trend", "Momentum", "DMA-S", "DMA-X", "DMA-C", "RS"]

    def _report(label, factors):
        comp = cl.composite_scores(df, cl.equal_weights(factors), factors)
        m = cl.bucket_metrics(comp, rets, frac=1 / 3, cost=COST)
        oic = _ic(comp, rets)
        print(f"  {label:30s}  IC={oic:+.3f}  top={m['top_return']:+6.2f}%  "
              f"spread={m['spread']:+6.2f}%  hit={m['hit_rate']:4.1f}%")

    _report("6-factor (NO volume) ★bar", base6)
    _report("+ deliv_strength", base6 + ["deliv_strength"])
    _report("+ holder_quality", base6 + ["holder_quality"])
    _report("+ deliv_strength + holder", base6 + ["deliv_strength", "holder_quality"])
    _report("+ deliv_adj (interaction)", base6 + ["deliv_adj"])

    # --- 3. Fair tests for holder_quality: covered-only + IC-weighted ---
    # holder_quality is only 57% covered and near-static per stock; equal-weight on
    # the full sample may be unfair. Re-test where it actually has data, and let the
    # IC engine SIZE it (a weak factor should earn a small weight, not a loud one).
    print("\n=== 3. holder_quality — fair tests (covered subset & IC-weighted) ===")
    sub = df[df["_hq_cov"]].copy()
    sret = sub["fwd_return"]

    def _rep_sub(label, factors, weights=None):
        w = weights if weights is not None else cl.equal_weights(factors)
        comp = cl.composite_scores(sub, w, factors)
        m = cl.bucket_metrics(comp, sret, frac=1 / 3, cost=COST)
        print(f"  {label:34s}  IC={_ic(comp, sret):+.3f}  top={m['top_return']:+6.2f}%  "
              f"spread={m['spread']:+6.2f}%  hit={m['hit_rate']:4.1f}%")

    print(f"  (covered subset: n={len(sub)})")
    _rep_sub("6-factor (covered) ★sub-bar", base6)
    _rep_sub("+ holder_quality (equal-wt)", base6 + ["holder_quality"])
    _rep_sub("+ deliv_strength (equal-wt)", base6 + ["deliv_strength"])
    _rep_sub("+ both (equal-wt)", base6 + ["deliv_strength", "holder_quality"])
    icw = cl.ic_weights(sub, base6 + ["deliv_strength", "holder_quality"],
                        min_samples=30, ic_floor=0.0)
    _rep_sub("+ both (IC-weighted)", base6 + ["deliv_strength", "holder_quality"], icw)
    print("  IC-weights: " + "  ".join(f"{k}={icw[k]:.2f}" for k in
          ["deliv_strength", "holder_quality"]))

    # --- 4. Walk-forward: does the deliv_strength edge REPEAT across folds? ---
    # deliv_strength is the only signal that beat the bar; before recommending it for
    # production we check the +spread is not a one-episode artifact. Chronological
    # thirds (deliv is ~100% covered, so the full sample is honest here).
    print("\n=== 4. Walk-forward: deliv_strength edge by chronological third ===")
    print("  the +spread must REPEAT, not live in one period\n")
    wf = df.sort_values("date").reset_index(drop=True)
    n_folds = 3
    bounds = [int(len(wf) * k / n_folds) for k in range(n_folds + 1)]
    print(f"  {'fold (dates)':24s} {'n':>5s}  {'bar spread':>10s}  "
          f"{'+deliv spread':>13s}  {'Δ':>7s}")
    for k in range(n_folds):
        f = wf.iloc[bounds[k]:bounds[k + 1]]
        c_bar = cl.composite_scores(f, cl.equal_weights(base6), base6)
        c_dlv = cl.composite_scores(
            f, cl.equal_weights(base6 + ["deliv_strength"]), base6 + ["deliv_strength"])
        s_bar = cl.bucket_metrics(c_bar, f["fwd_return"], frac=1 / 3, cost=COST)["spread"]
        s_dlv = cl.bucket_metrics(c_dlv, f["fwd_return"], frac=1 / 3, cost=COST)["spread"]
        d = s_dlv - s_bar
        label = f"{f['date'].iloc[0]}→{f['date'].iloc[-1]}"
        print(f"  {label:24s} {len(f):5d}  {s_bar:+10.2f}  {s_dlv:+13.2f}  {d:+7.2f}")


if __name__ == "__main__":
    main()
