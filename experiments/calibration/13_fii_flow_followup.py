"""13_fii_flow_followup.py — is the FII-flow downtrend signal REAL, or an artifact?

CONTEXT
-------
12 found the FII positioning LEVEL is a null gate (hedging-driven, structurally
net-short). The one live thread: the 20-session CHANGE in net positioning (fii_chg)
showed +0.175 IC in DOWNTRENDS — FII covering shorts as a downtrend exhausts is a
plausible bottoming tell. That number rests on a tunable window, lives in one regime,
and is exactly the kind of result that evaporates under scrutiny. This probe is built
to FALSIFY it, not flatter it. Four tests, hardest first:

  1. WINDOW ROBUSTNESS — recompute the downtrend IC for windows {5,10,20,30,40}.
     A real signal is stable across windows; a knife-edge spike at 20 is overfit.
  2. TEMPORAL CONCENTRATION — where do the downtrend samples sit in time? If they
     all fall in the 2024-25 drawdown, +0.175 is ONE episode and cannot be walk-
     forward-validated with this data. That admission is itself the finding.
  3. MONOTONICITY — downtrend fwd_return by fii_chg tercile. A clean rising
     relationship is trustworthy; a lone IC number that isn't monotone is noise.
  4. GATE REFINEMENT (the only way it earns a slot) — the HMM gate blanket-damps ALL
     downtrend confidence. Does damping ONLY the still-falling downtrend names
     (fii_chg<=0) while KEEPING the FII-covering ones (fii_chg>0) beat blanket
     damping — i.e. does the flow recover downtrend upside the blanket throws away?

Point-in-time, offline (reads 11's cache). Run from repo root:
    python3 experiments/calibration/13_fii_flow_followup.py
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from experiments.calibration import calib_lib as cl  # noqa: E402

_SAMPLES = _HERE / "data" / "samples.csv"
_OI = _HERE / "data" / "fii_deriv" / "participant_oi.csv"
COST = 0.1
BASE6 = ["Trend", "Momentum", "DMA-S", "DMA-X", "DMA-C", "RS"]


def _net(long_, short_):
    import numpy as np
    denom = long_ + short_
    return np.where(denom > 0, (long_ - short_) / denom, np.nan)


def main() -> None:
    import numpy as np
    import pandas as pd
    from alert_bot.calibrate import spearman_ic

    if not _OI.exists():
        print(f"missing {_OI} — run 11_fetch_fii_deriv.py first.")
        return

    oi = pd.read_csv(_OI, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    oi["net"] = _net(oi["fii_future_index_long"].to_numpy(float),
                     oi["fii_future_index_short"].to_numpy(float))
    odates = oi["date"].dt.date.astype(str).to_numpy()

    df = pd.read_csv(_SAMPLES)
    jj = np.searchsorted(odates, df["date"].to_numpy(), side="right") - 1
    ok = jj >= 0
    jc = jj.clip(0)

    def _ic(s, r):
        return spearman_ic(list(s), list(r), min_samples=20)

    dn = df["regime"] == "downtrend"
    print(f"samples={len(df)}  downtrend={int(dn.sum())}  joined={int(ok.sum())}")

    # --- TEST 1: window robustness ----------------------------------------------
    print("\n=== 1. Window robustness — downtrend IC of fii_chg across windows ===")
    print("   a real signal is stable; a spike only at win=20 is overfit\n")
    print(f"  {'window':>7s} {'overall IC':>11s} {'downtrend IC':>13s}")
    for w in [5, 10, 20, 30, 40]:
        chg = oi["net"].diff(w).to_numpy(float)
        col = np.where(ok, chg[jc], np.nan)
        s = pd.Series(col, index=df.index)
        ov = _ic(s[s.notna()], df["fwd_return"][s.notna()])
        sd = s[dn & s.notna()]
        di = _ic(sd, df["fwd_return"][sd.index]) if len(sd) >= 20 else None
        print(f"  {w:7d} {ov:+11.3f} {(f'{di:+.3f}' if di is not None else 'n/a'):>13s}")

    # fix window=20 for the remaining tests
    chg20 = oi["net"].diff(20).to_numpy(float)
    df["fii_chg"] = np.where(ok, chg20[jc], np.nan)

    # --- TEST 2: temporal concentration -----------------------------------------
    print("\n=== 2. Temporal concentration — can the downtrend IC even be validated? ===")
    dd = df[dn].copy()
    dd["year"] = pd.to_datetime(dd["date"]).dt.year
    by_year = dd["year"].value_counts().sort_index()
    print("  downtrend samples by year:")
    for y, c in by_year.items():
        print(f"    {y}: {c:4d}  ({c/len(dd)*100:4.1f}%)")
    print("  -> if one year dominates, the +0.175 is one episode, not a walk-forward result.")

    # --- TEST 3: monotonicity ----------------------------------------------------
    print("\n=== 3. Monotonicity — downtrend fwd_return by fii_chg tercile ===")
    d3 = df[dn & df["fii_chg"].notna()].copy()
    if len(d3) >= 9:
        d3["tercile"] = pd.qcut(d3["fii_chg"], 3, labels=["falling", "mid", "covering"])
        g = d3.groupby("tercile", observed=True)["fwd_return"].agg(["mean", "count"])
        for t, row in g.iterrows():
            print(f"    {str(t):9s}  mean fwd_return {row['mean']:+7.2f}%  (n={int(row['count'])})")
        print("  -> trustworthy only if 'covering' > 'mid' > 'falling' (clean monotone).")

    # --- TEST 4: gate refinement -------------------------------------------------
    print("\n=== 4. Gate refinement — does flow-selective damping beat blanket? ===")
    rets = df["fwd_return"]
    comp = cl.composite_scores(df, cl.equal_weights(BASE6), BASE6)

    def _report(label, gated):
        m = cl.bucket_metrics(gated, rets, frac=1 / 3, cost=COST)
        mt = cl.bucket_metrics(gated[dn], rets[dn], frac=1 / 3, cost=COST)
        print(f"  {label:34s} spread {m['spread']:+7.2f}  top {m['top_return']:+7.2f}  "
              f"hit {m['hit_rate']:4.1f}% | downtrend-spread {mt['spread']:+7.2f}")

    # blanket: damp every downtrend sample
    blanket_cell = pd.Series(np.where(dn, "danger", "ok"), index=df.index)
    # selective: damp downtrend ONLY where flow is not improving (fii_chg <= 0);
    # keep confidence where FII are covering (fii_chg > 0). Missing flow -> treat as danger.
    keep = dn & (df["fii_chg"] > 0)
    selective_cell = pd.Series(np.where(dn & ~keep, "danger", "ok"), index=df.index)

    _report("ungated baseline", comp)
    _report("blanket HMM-downtrend damp", cl.gate_composite(comp, blanket_cell, {"danger"}, damp=0.0))
    _report("flow-selective damp (keep covering)",
            cl.gate_composite(comp, selective_cell, {"danger"}, damp=0.0))
    print(f"\n  kept {int(keep.sum())} of {int(dn.sum())} downtrend samples alive "
          f"(FII covering); blanket damps all {int(dn.sum())}.")
    print("  flow earns a slot ONLY if selective lifts overall spread above blanket "
          "without giving back the tax cut.")


if __name__ == "__main__":
    main()
