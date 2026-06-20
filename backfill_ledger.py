#!/usr/bin/env python3
"""
backfill_ledger.py — warm-start the forward-test ledger from sent_alerts.json.

For every historically-fired BUY/SELL alert, reconstruct an entry/target/stop (via
the production trade_levels math, as-of the fire date) and append an open ledger
row. The resolver then scores it forward against the OHLC that printed afterwards.
Idempotent: re-running only adds calls not already in the ledger, so it doubles as
the catch-up step for any alerts fired since the last run.

Usage:  python3 backfill_ledger.py
"""
import hashlib
import json
from pathlib import Path

import forward_lib as fl

ROOT = Path(__file__).parent
SENT = ROOT / "data" / "sent_alerts.json"
EXCLUDE_TICKERS = {"NIF100BEES"}     # benchmark, not a stock-pick signal


def call_id(a: dict) -> str:
    raw = f"{a.get('ticker')}|{a.get('price_str')}|{a.get('fired_at')}|{a.get('alert_type')}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def load_backtest_levels(ticker: str) -> dict:
    p = ROOT / "analysis" / f"{ticker}_backtest.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()).get("levels", {})
    except (json.JSONDecodeError, OSError):
        return {}


def main():
    sent = json.loads(SENT.read_text(encoding="utf-8"))
    existing = {r["id"] for r in fl.load_ledger()}
    added = excluded = skipped = 0

    for a in sent.values():
        atype = a.get("alert_type")
        if atype not in ("BUY", "SELL"):
            continue
        cid = call_id(a)
        if cid in existing:
            skipped += 1
            continue

        ticker = a.get("ticker", "")
        if ticker in EXCLUDE_TICKERS:
            continue
        df = fl.load_ohlc(ticker)
        if df is None:
            continue
        df_asof = fl.as_of(df, a["fired_at"])
        if len(df_asof) < 60:                    # too little history to reconstruct
            continue

        entry = float(a["price"])
        regime = fl.regime_proxy(df_asof)
        liq = fl.liquidity_tier(df_asof)
        bt = load_backtest_levels(ticker).get(a.get("price_str", ""))
        lv = (fl.reconstruct_buy(entry, df_asof, regime, bt) if atype == "BUY"
              else fl.reconstruct_sell(entry, df_asof))
        # Backtest win-rate as the (weak) prior. NOTE: win_rate_6m is "% positive at
        # 6M", a different metric than our forward "target before stop in 63d" — so it
        # anchors only loosely; the δ discount + forward data carry the weight.
        bt_wr = (bt.get("win_rate_6m") / 100.0
                 if bt and bt.get("win_rate_6m") is not None else None)

        row = {
            "id": cid, "ticker": ticker, "alert_type": atype,
            "signal": a.get("signal"), "conviction": a.get("confidence"),
            "regime_at_fire": regime, "regime_source": "proxy", "liq_tier": liq,
            "bt_winrate": bt_wr,
            "entry": entry, "target": None, "stop": None, "rr": None, "reload_to": None,
            "fired_at": a["fired_at"], "source": "reconstructed",
            "status": "open", "triggered_at": a["fired_at"],
            "resolved_at": None, "realized_R": None, "exit_price": None, "exit_reason": None,
        }
        if lv:
            row.update(lv)
        else:
            row["status"] = "excluded"
            row["exit_reason"] = "no_levels"
            excluded += 1
        fl.append_row(row)
        added += 1

    print(f"backfill: +{added} new rows ({excluded} excluded no-levels), "
          f"{skipped} already present. Ledger: {fl.LEDGER}")


if __name__ == "__main__":
    main()
