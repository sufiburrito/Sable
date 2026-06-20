#!/usr/bin/env python3
"""
experiments/ml_advisory_learning/build_dataset.py — EXPERIMENT (read-only, no production change).

Materialise the supervised learning dataset Sable would need to *learn from real outcomes*:
join the 16-factor vote vector logged at fire-time (`data/sent_alerts.json`) to the
market-verified label (`data/forward_ledger.jsonl` realized_R / win-loss), by ticker+fired_at.

Then report the live signal each factor actually carries (Spearman IC vs realized_R on the
resolved trades) and contrast it with the *reconstructed* IC in `data/factor_weights.json`.

This is an existence check on a tiny label set — read it as indicative, not conclusive.
Nothing here writes to or imports into production.
"""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
FACTORS = ["Trend", "Momentum", "Volume", "Regime", "Level", "RS", "MMI", "Insider",
           "vcp", "vix", "flow", "breadth", "fund", "DMA-S", "DMA-X", "DMA-C"]


def load_dataset() -> pd.DataFrame:
    ledger = [json.loads(l) for l in (ROOT / "data/forward_ledger.jsonl").read_text().splitlines() if l.strip()]
    resolved = {(r["ticker"], r["fired_at"]): r for r in ledger if r.get("realized_R") is not None}
    alerts = json.loads((ROOT / "data/sent_alerts.json").read_text())
    alerts = list(alerts.values()) if isinstance(alerts, dict) else alerts

    rows = []
    for a in alerts:
        key = (a.get("ticker"), a.get("fired_at"))
        r = resolved.get(key)
        if r is None or not isinstance(a.get("factors"), dict):
            continue
        row = {f: a["factors"].get(f, 0) for f in FACTORS}
        row.update(realized_R=r["realized_R"], win=int(r["status"] == "win"),
                   regime=r.get("regime_at_fire"), conviction=r.get("conviction"),
                   alert_type=r.get("alert_type"))
        rows.append(row)
    return pd.DataFrame(rows)


def availability() -> dict:
    """The binding constraint: how many factor-vector × resolved-outcome rows exist to learn from."""
    ledger = [json.loads(l) for l in (ROOT / "data/forward_ledger.jsonl").read_text().splitlines() if l.strip()]
    resolved = {(r["ticker"], r["fired_at"]) for r in ledger if r.get("realized_R") is not None}
    open_ = {(r["ticker"], r["fired_at"]) for r in ledger if r.get("status") == "open"}
    alerts = json.loads((ROOT / "data/sent_alerts.json").read_text())
    alerts = list(alerts.values()) if isinstance(alerts, dict) else alerts
    fk = {(a.get("ticker"), a.get("fired_at")) for a in alerts if isinstance(a.get("factors"), dict)}
    fdts = sorted(a["fired_at"][:10] for a in alerts if isinstance(a.get("factors"), dict))
    return {"resolved": len(resolved), "factored": len(fk),
            "usable_now": len(resolved & fk), "pending": len(open_ & fk),
            "factor_logging_from": fdts[0] if fdts else None}


def main():
    a = availability()
    print("=== Data availability (the binding constraint) ===")
    print(f"resolved market-verified labels : {a['resolved']}")
    print(f"alerts with a 16-factor vector  : {a['factored']}  (logging began {a['factor_logging_from']})")
    print(f"USABLE training rows TODAY       : {a['usable_now']}  (factors × resolved outcome)")
    print(f"pending (factored, not resolved) : {a['pending']}  → become labels over ~63 sessions\n")

    df = load_dataset()
    if df.empty:
        print("No joinable (factor-vector → outcome) rows yet — a feature-rich supervised model is "
              "DATA-BLOCKED. Learn from coarse features (regime/conviction) via the Bayesian per-class "
              "edge until the factored alerts resolve. Re-run this as the label set accrues.")
        return
    print(f"Joined labeled examples: {len(df)}  (win rate {df['win'].mean()*100:.0f}%)\n")
    recon = json.loads((ROOT / "data/factor_weights.json").read_text()).get("ic", {})
    print(f"{'factor':<10} {'live IC(R)':>10} {'live IC(win)':>13} {'recon IC':>10} {'votes!=0':>9}")
    print("-" * 56)
    for f in FACTORS:
        ic_r = df[f].corr(df["realized_R"], method="spearman")
        ic_w = df[f].corr(df["win"], method="spearman")
        rec = recon.get(f)
        print(f"{f:<10} {ic_r:>10.3f} {ic_w:>13.3f} "
              f"{(f'{rec:.3f}' if rec is not None else '—'):>10} {int((df[f] != 0).sum()):>9}")


if __name__ == "__main__":
    main()
