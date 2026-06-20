"""12_fii_deriv_factor.py — does market-level FII F&O POSITIONING earn its slot?

CONTEXT
-------
11 cached FII/DII participant open interest per trading day. FII index-futures
positioning is a MARKET-LEVEL signal: ONE value per date, identical across every
stock that day. That has a hard consequence for how we test it —

  * It CANNOT improve cross-sectional rank WITHIN a date (every name gets the same
    nudge), so the 09-style "drop it into the composite and measure spread" test is
    the wrong frame.
  * It CAN act as a market-TIMING signal — a gate/overlay that says "trust BUYs less
    when FII are positioned bearish" — exactly like the regime detectors (03/07).

So this probe asks two market-timing questions:
  Test A — pooled IC: do names bought when FII are net-LONG index futures go on to
           return more? Spearman(fii_idx_net @ sample date, fwd_return), by regime.
  Test B — as a gate: damp the composite when FII positioning is bearish-extreme
           (point-in-time), and ask whether that cuts the downtrend tax / lifts
           overall spread — AND whether it adds anything the price-based HMM regime
           gate (samples' own 'regime' col) doesn't already capture. A market signal
           earns its slot only if it beats HMM-alone, the same bar D4 had to clear.

HONEST CAVEAT: FIIs hedge cash books with index futures, so "FII net-short" is often
a hedge, not a bear bet — this may test null. Point-in-time throughout (danger at date
t uses only positioning <= t). Offline once 11 has cached. Run from repo root:
    python3 experiments/calibration/12_fii_deriv_factor.py
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
REGIMES = ["uptrend", "sideways", "downtrend"]
COST = 0.1
LO_PCT = 0.33          # net positioning in its trailing bottom-third => bearish-extreme
MIN_HIST = 30          # sessions before we'll judge a positioning extreme
CHG_WIN = 20           # sessions for the positioning-change (flow) variant


def _net(long_, short_):
    import numpy as np
    denom = long_ + short_
    return np.where(denom > 0, (long_ - short_) / denom, np.nan)


def _danger_pit(net_vals):
    """Point-in-time: True where net positioning is in its trailing bottom LO_PCT.

    A bearish-extreme is judged against the signal's OWN past only (no peek-ahead):
    the share of prior sessions whose net was strictly below today's. <= LO_PCT means
    today sits in the bearish tail. Neutral (False) until MIN_HIST sessions accrue.
    """
    import numpy as np
    out = np.zeros(len(net_vals), dtype=bool)
    for i in range(len(net_vals)):
        if i + 1 < MIN_HIST or net_vals[i] != net_vals[i]:
            continue
        hist = net_vals[: i + 1]
        hist = hist[~np.isnan(hist)]
        if len(hist) < MIN_HIST:
            continue
        pct = float((hist < net_vals[i]).mean())
        out[i] = pct <= LO_PCT
    return out


def main() -> None:
    import numpy as np
    import pandas as pd
    from alert_bot.calibrate import spearman_ic

    if not _OI.exists():
        print(f"missing {_OI} — run 11_fetch_fii_deriv.py first.")
        return

    oi = pd.read_csv(_OI, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    oi["fii_idx_net"] = _net(oi["fii_future_index_long"].to_numpy(float),
                             oi["fii_future_index_short"].to_numpy(float))
    oi["fii_idx_chg"] = oi["fii_idx_net"].diff(CHG_WIN)          # flow: N-session change
    oi["danger"] = _danger_pit(oi["fii_idx_net"].to_numpy(float))
    odates = oi["date"].dt.date.astype(str).to_numpy()

    print(f"participant OI: {len(oi)} sessions  {odates[0]} -> {odates[-1]}")
    print(f"  fii_idx_net range [{np.nanmin(oi['fii_idx_net']):+.2f}, "
          f"{np.nanmax(oi['fii_idx_net']):+.2f}]  mean {np.nanmean(oi['fii_idx_net']):+.3f}")
    print(f"  bearish-extreme sessions (danger): {int(oi['danger'].sum())} "
          f"({oi['danger'].mean()*100:.1f}%)")

    # --- join market-level signal to each sample by latest session <= sample date ---
    df = pd.read_csv(_SAMPLES)
    net_v = oi["fii_idx_net"].to_numpy(float)
    chg_v = oi["fii_idx_chg"].to_numpy(float)
    dng_v = oi["danger"].to_numpy(bool)
    jj = np.searchsorted(odates, df["date"].to_numpy(), side="right") - 1
    ok = jj >= 0
    df["fii_net"] = np.where(ok, net_v[jj.clip(0)], np.nan)
    df["fii_chg"] = np.where(ok, chg_v[jj.clip(0)], np.nan)
    df["fii_danger"] = np.where(ok, dng_v[jj.clip(0)], False)
    print(f"  samples joined: {int(ok.sum())}/{len(df)} ({ok.mean()*100:.1f}%)")

    rets = df["fwd_return"]

    def _ic(s, r):
        return spearman_ic(list(s), list(r), min_samples=20)

    # --- Test A: market-timing IC (pooled across dates) ---
    print("\n=== A. Market-timing IC — buy when FII positioned long? (pooled) ===")
    print(f"  {'signal':10s} {'overall':>8s} " + " ".join(f"{r[:4]:>8s}" for r in REGIMES))
    sub = df[df["fii_net"].notna()]
    for col in ["fii_net", "fii_chg"]:
        s2 = sub[sub[col].notna()]
        oic = _ic(s2[col], s2["fwd_return"]) if len(s2) >= 20 else None
        cells = []
        for r in REGIMES:
            g = s2[s2["regime"] == r]
            ic = _ic(g[col], g["fwd_return"]) if len(g) >= 20 else None
            cells.append(f"{ic:+.3f}" if ic is not None else "   n/a")
        ostr = f"{oic:+.3f}" if oic is not None else "   n/a"
        print(f"  {col:10s} {ostr:>8s} " + " ".join(f"{c:>8s}" for c in cells))

    # --- Test B: as a gate. Does FII-danger beat / add to the HMM regime gate? ---
    print("\n=== B. Gate test — damp BUY confidence when positioning is bearish ===")
    base6 = ["Trend", "Momentum", "DMA-S", "DMA-X", "DMA-C", "RS"]
    comp = cl.composite_scores(df, cl.equal_weights(base6), base6)

    def _metrics(gated):
        m = cl.bucket_metrics(gated, rets, frac=1 / 3, cost=COST)
        dn = df["regime"] == "downtrend"
        mt = cl.bucket_metrics(gated[dn], rets[dn], frac=1 / 3, cost=COST)
        return m, mt

    # gate cell series
    fii_cell = pd.Series(np.where(df["fii_danger"], "danger", "ok"), index=df.index)
    hmm_cell = pd.Series(np.where(df["regime"] == "downtrend", "danger", "ok"), index=df.index)
    union_cell = pd.Series(
        np.where(df["fii_danger"] | (df["regime"] == "downtrend"), "danger", "ok"),
        index=df.index)

    variants = [
        ("ungated baseline", cl.gate_composite(comp, hmm_cell, set(), damp=0.0)),  # no danger cells = no-op
        ("gate: FII-danger", cl.gate_composite(comp, fii_cell, {"danger"}, damp=0.0)),
        ("gate: HMM-downtrend", cl.gate_composite(comp, hmm_cell, {"danger"}, damp=0.0)),
        ("gate: FII OR downtrend", cl.gate_composite(comp, union_cell, {"danger"}, damp=0.0)),
    ]
    print(f"  {'variant':24s} {'spread':>8s} {'top':>8s} {'hit':>6s} | "
          f"{'downtrend-spread (tax)':>22s}")
    for label, g in variants:
        m, mt = _metrics(g)
        print(f"  {label:24s} {m['spread']:+8.2f} {m['top_return']:+8.2f} {m['hit_rate']:5.1f}% | "
              f"{mt['spread']:+10.2f}  (n={mt['n_bucket']})")

    print("\n  read: FII-gate earns its slot only if it lifts overall spread or cuts the")
    print("  downtrend tax BEYOND what HMM-downtrend already does (the 'beats HMM-alone' bar).")


if __name__ == "__main__":
    main()
