#!/usr/bin/env python3
"""
experiments/ml_advisory_learning/replay.py — EXPERIMENT (read-only, no production change).

Track 2 of the data-gathering system (bean algotrading-kkyc): the as-of replay harness.
Walk every active ticker's OHLC history; at each past date, re-score the reconstructable
factors AS-OF (no look-ahead), reconstruct a BUY level, and resolve it forward with the SAME
production resolver the live ledger uses — so replay labels are directly comparable to the
live 63. Yields thousands of (features → realized R) rows to develop the model on, and lets us
DE-CONFOUND the MMI contrarian signal across many fear/greed cycles instead of one 3-month window.

Reuses production scorers/levels read-only: alert_bot.confidence scorers (via calibrate.PRICE_FACTORS
+ _score_relative_strength), forward_lib.reconstruct_buy / regime_proxy / atr14, forward_resolve.
MMI joined as-of from datasets.db (2012→). The ~181-style "no defensible level" cases are recovered
with an ATR fallback (2:1) and flagged. Nothing here writes to or imports into production paths.
"""
import bisect
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from alert_bot.calibrate import PRICE_FACTORS                 # noqa: E402
from alert_bot.confidence import _score_relative_strength      # noqa: E402
from alert_bot.ohlc_cache import read_ohlc_cache               # noqa: E402
import forward_lib as fl                                       # noqa: E402
import forward_resolve                                         # noqa: E402

ANALYSIS = ROOT / "analysis"
OUT = Path(__file__).resolve().parent / "replay_dataset.jsonl"
STRIDE = 5                 # sample every 5 trading days (overlap fine; handle in CV)
WARMUP = 220               # need the 200-DMA + 20-day slope before scoring
HORIZON = fl.HORIZON_CAP   # 63 forward bars to resolve (matches the live rig)
ATR_TARGET_K, ATR_STOP_K = 2.0, 1.0   # ATR fallback when no defensible SR level (2:1)


def mmi_lookup():
    db = ROOT / "datasets" / "datasets.db"
    if not db.exists():
        return lambda d: None
    rows = sqlite3.connect(str(db)).execute("SELECT date, value FROM mmi ORDER BY date").fetchall()
    dates = [d for d, _ in rows]; vals = [v for _, v in rows]
    return lambda d: (vals[i] if (i := bisect.bisect_right(dates, d) - 1) >= 0 else None)


def replay_ticker(ticker, df, nifty, mmi) -> list[dict]:
    closes = df["Close"].to_numpy(float)
    n = len(df)
    out = []
    for t in range(WARMUP, n - HORIZON, STRIDE):
        past = df.iloc[: t + 1]
        date_t = df.index[t]
        entry = float(closes[t])
        regime = fl.regime_proxy(past)
        lv = fl.reconstruct_buy(entry, past, regime, None)
        fallback = False
        if not lv:                                            # recover the would-be-excluded
            atr = fl.atr14(past)
            if not atr:
                continue
            tgt, stp = round(entry + ATR_TARGET_K * atr, 2), round(entry - ATR_STOP_K * atr, 2)
            if not (tgt > entry > stp):
                continue
            lv = {"target": tgt, "stop": stp, "rr": round((tgt - entry) / (entry - stp), 2)}
            fallback = True
        row = {"alert_type": "BUY", "entry": entry, "target": lv["target"], "stop": lv["stop"],
               "rr": lv["rr"], "fired_at": date_t.strftime("%Y-%m-%dT00:00:00+05:30"), "status": "open"}
        if not forward_resolve.resolve_row(row, df):          # resolve with the production walker
            continue
        feats = {name: fn(past, "BUY").score for name, fn in PRICE_FACTORS.items()}
        if nifty is not None and len(nifty.loc[:date_t]) >= 63:
            feats["RS"] = _score_relative_strength(past, "BUY", nifty=nifty.loc[:date_t]).score
        else:
            feats["RS"] = 0
        out.append({
            "date": str(date_t)[:10], "ticker": ticker, "regime": regime, "fallback_level": fallback,
            "entry": entry, "target": lv["target"], "stop": lv["stop"], "rr": lv["rr"],
            "realized_R": row["realized_R"], "win": int(row["status"] == "win"),
            "exit_reason": row["exit_reason"], "mmi": mmi(str(date_t)[:10]), **feats,
        })
    return out


def main():
    from alert_bot import calibrate
    tickers = calibrate._watchlist_tickers()
    nifty = read_ohlc_cache("NIFTY50", analysis_dir=ANALYSIS)
    mmi = mmi_lookup()
    rows, skipped = [], 0
    for tk in tickers:
        df = read_ohlc_cache(tk, analysis_dir=ANALYSIS)
        if df is None or len(df) < WARMUP + HORIZON + 1:
            skipped += 1
            continue
        rows.extend(replay_ticker(tk, df, nifty, mmi))
    OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    df = pd.DataFrame(rows)
    print(f"Replay dataset: {len(df)} rows · {df['ticker'].nunique()} tickers · "
          f"{df['date'].min()} → {df['date'].max()}  ({skipped} tickers skipped)")
    print(f"  win rate {df['win'].mean()*100:.0f}% · mean R {df['realized_R'].mean():+.3f} · "
          f"fallback levels {df['fallback_level'].mean()*100:.0f}% → {OUT.name}\n")
    print("=== Factor IC vs realized R on the big set (Spearman) ===")
    factors = list(PRICE_FACTORS) + ["RS", "mmi"]
    for f in factors:
        ic = df[f].corr(df["realized_R"], method="spearman")
        nz = int((df[f] != 0).sum()) if f != "mmi" else int(df[f].notna().sum())
        print(f"  {f:<10} IC={ic:+.3f}   (n={nz})")
    mdf = df[df["mmi"].notna()]
    print(f"\n=== MMI de-confound (BUY, n={len(mdf)} across many cycles) ===")
    for lo, hi, lbl in [(0, 30, "<30 ExFear"), (30, 50, "30-50 Fear"), (50, 70, "50-70 Greed"), (70, 101, "70+ ExGreed")]:
        s = mdf[(mdf["mmi"] >= lo) & (mdf["mmi"] < hi)]
        if len(s):
            print(f"    {lbl:<12} n={len(s):>4}  win {s['win'].mean()*100:>3.0f}%  mean R {s['realized_R'].mean():+.3f}")
    print("\nResearch read only. Grouped CV by ticker/time before trusting (samples overlap).")


if __name__ == "__main__":
    main()
