"""
Nightly fetch: India VIX + Nifty PCR + F&O ban list.
Writes data/fno_signals.json.

Per docs/fno_signals.md — signal-only. No Black-Scholes, no Greeks, no strategy.
India VIX via yfinance (^INDIAVIX); PCR and ban list degrade gracefully if unavailable.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

FNO_PATH     = Path("data/fno_signals.json")
VIX_TICKER   = "^INDIAVIX"
NIFTY_TICKER = "^NSEI"


def _fetch_vix() -> dict:
    try:
        value = round(float(yf.Ticker(VIX_TICKER).fast_info.last_price), 2)
    except Exception as e:
        return {"value": None, "regime": "UNKNOWN", "note": str(e)}

    if value < 12:
        regime, posture = "EXTREME_COMPLACENCY", "skeptical of breakouts"
    elif value < 15:
        regime, posture = "LOW_VOL_TRENDING",    "normal sizing"
    elif value < 20:
        regime, posture = "NORMAL",              "default"
    elif value < 25:
        regime, posture = "ELEVATED_FEAR",       "near-bottom signal — watch DII absorption"
    elif value <= 35:
        regime, posture = "HIGH_FEAR",           "contrarian accumulation zone — confirm stage first"
    else:
        regime, posture = "CRISIS",              "cash heavy — wait for VIX < 30 before swing adds"

    return {"value": value, "regime": regime, "delivery_posture": posture}


def _fetch_pcr() -> dict:
    try:
        nifty = yf.Ticker(NIFTY_TICKER)
        exps  = nifty.options
        if not exps:
            return {"value": None, "regime": "UNKNOWN", "note": "no expiry dates"}
        chain   = nifty.option_chain(exps[0])
        put_oi  = chain.puts["openInterest"].sum()
        call_oi = chain.calls["openInterest"].sum()
        pcr = round(put_oi / call_oi, 3) if call_oi > 0 else None
        if pcr is None:              regime = "UNKNOWN"
        elif pcr > 1.3:              regime = "CONTRARIAN_BULLISH"
        elif pcr > 1.1:              regime = "MILDLY_BEARISH"
        elif pcr >= 0.9:             regime = "NEUTRAL"
        elif pcr >= 0.7:             regime = "MILDLY_BULLISH"
        else:                        regime = "CONTRARIAN_BEARISH"
        return {"value": pcr, "regime": regime}
    except Exception as e:
        return {"value": None, "regime": "UNKNOWN", "note": str(e)}


def refresh() -> dict:
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vix":     _fetch_vix(),
        "pcr":     _fetch_pcr(),
        "fno_ban": {"tickers": [], "note": "auto-detection not yet implemented"},
    }
    FNO_PATH.parent.mkdir(exist_ok=True)
    FNO_PATH.write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    r = refresh()
    print(f"India VIX: {r['vix']['value']} — {r['vix']['regime']}")
    print(f"Nifty PCR: {r['pcr']['value']} — {r['pcr']['regime']}")
