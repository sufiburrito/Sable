"""
forward_lib.py — shared helpers for the TRADE-call forward-testing rig.

The rig answers, honestly and out-of-sample, whether our swing calls actually pay:
each fired BUY/SELL call gets reconstructed into an entry/target/stop, then resolved
against the OHLC that printed *after* it (target → +R, stop → −1R, time-cap →
fractional R). This module holds the pieces shared by backfill / resolve / estimate:

  - OHLC + as-of-date slicing
  - level reconstruction reusing the real trade_levels primitives (buy_stop/buy_target)
    and sr_levels (support for the stop, resistance as a target fallback)
  - a cheap as-of regime proxy and a liquidity tier (so we never re-run the HMM 530×)
  - append-only JSONL ledger I/O

Faithful where it can be (reuses the production stop/target math), robust where it
must be (no dependency on the current stocks/*.md, which drifts).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from alert_bot.trade_levels import (
    buy_stop, buy_target, atr_k_for_regime, rr_triplet, nearest_below, nearest_above,
)
from sr_levels import compute_sr

ROOT = Path(__file__).parent
ANALYSIS = ROOT / "analysis"
LEDGER = ROOT / "data" / "forward_ledger.jsonl"

HORIZON_CAP = 63          # trading days before an open call is force-resolved
MA_WEEKS = 150            # ~30-week MA for the regime proxy


# ── data ────────────────────────────────────────────────────────────────────

def load_ohlc(ticker: str) -> pd.DataFrame | None:
    p = ANALYSIS / f"{ticker}_ohlc_cache.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, parse_dates=["Date"], index_col="Date").sort_index()
    return df if len(df) else None


def as_of(df: pd.DataFrame, ts) -> pd.DataFrame:
    """Rows up to and including the fire date (no lookahead)."""
    return df.loc[:pd.Timestamp(ts).tz_localize(None).normalize()]


def _naive(ts) -> "pd.Timestamp":
    """A tz-naive, day-normalized Timestamp from an ISO string, date string, or date."""
    t = pd.Timestamp(ts)
    if t.tz is not None:
        t = t.tz_localize(None)
    return t.normalize()


def first_touch_low(df: "pd.DataFrame | None", from_date, level, days: int = 45) -> tuple:
    """For a BUY level: was it actually reachable? Did any daily Low trade at/below `level`
    within `days` calendar days from from_date (inclusive)? Returns (hit: bool, min_low: float)
    — `min_low` is the lowest the stock traded in the window (the closest it got to the level
    when not hit). (None, None) if no OHLC / no level / no bars in the window."""
    if df is None or not level:
        return (None, None)
    start = _naive(from_date)
    win = df.loc[(df.index >= start) & (df.index <= start + pd.Timedelta(days=days))]
    if win.empty:
        return (None, None)
    lo = float(win["Low"].min())
    return (lo <= level, round(lo, 2))


def excursion(df: "pd.DataFrame | None", from_date, ref_price, horizon: int = HORIZON_CAP) -> dict | None:
    """Realized peak/trough move over the `horizon` trading bars AFTER from_date.

    Returns {peak_pct, trough_pct, bars, complete} as % vs `ref_price` (peak = max High,
    trough = min Low over the window); `complete` is True once a full `horizon` of bars has
    printed (so a too-recent event can be held 'pending'). None if no OHLC, no ref, or no
    future bars. This is the *fact* against which forecast targets/exits are checked."""
    if df is None or not ref_price:
        return None
    fut = df.loc[df.index > _naive(from_date)].head(horizon)
    if fut.empty:
        return None
    peak = (float(fut["High"].max()) - ref_price) / ref_price * 100.0
    trough = (float(fut["Low"].min()) - ref_price) / ref_price * 100.0
    return {"peak_pct": round(peak, 2), "trough_pct": round(trough, 2),
            "bars": int(len(fut)), "complete": len(fut) >= horizon}


def parse_band(price_str: str) -> tuple[float, float]:
    """'₹275-284' → (275.0, 284.0); single value → (v, v)."""
    s = price_str.replace("₹", "").replace(",", "").strip()
    if "-" in s:
        lo, hi = s.split("-", 1)
        return float(lo), float(hi)
    v = float(s)
    return v, v


def atr14(df: pd.DataFrame, n: int = 14) -> float | None:
    if len(df) < n + 1:
        return None
    h, l, c = df["High"].values, df["Low"].values, df["Close"].values
    prev = c[:-1]
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))
    return float(np.mean(tr[-n:]))


def regime_proxy(df: pd.DataFrame) -> str:
    """Cheap Weinstein-flavoured as-of regime (avoids re-running the HMM 530×):
    price vs the 30-week MA and the MA's slope. Labelled 'proxy' in the ledger."""
    if len(df) < MA_WEEKS:
        return "sideways"
    ma = df["Close"].rolling(MA_WEEKS).mean()
    price, ma_now, ma_then = df["Close"].iloc[-1], ma.iloc[-1], ma.iloc[-20]
    if price > ma_now and ma_now > ma_then:
        return "bull"
    if price < ma_now and ma_now < ma_then:
        return "bear"
    return "sideways"


def liquidity_tier(df: pd.DataFrame) -> str:
    """Median daily turnover (Close×Volume) over the last 60 sessions → tier."""
    tail = df.tail(60)
    adv = float((tail["Close"] * tail["Volume"]).median())   # ₹ traded/day
    if adv >= 5e7:        # ≥ ₹5 cr/day
        return "liquid"
    if adv >= 5e6:        # ≥ ₹50 lakh/day
        return "mid"
    return "thin"


# ── level reconstruction (reuses production math) ───────────────────────────

def p75_cone(df_asof: pd.DataFrame, entry: float, up: bool) -> float | None:
    """Cheap stand-in for the production regime Monte-Carlo p75 cap: a one-sided
    75th-percentile move over the resolution horizon from daily log-vol. Keeps
    targets realistic (a huge backtest MFE can't project an absurd price)."""
    lr = np.diff(np.log(df_asof["Close"].values[-252:]))
    sigma = float(np.std(lr)) if len(lr) else 0.0
    if sigma <= 0:
        return None
    move = np.exp(0.6745 * sigma * np.sqrt(HORIZON_CAP))   # p75 of a lognormal step
    return entry * move if up else entry / move


def reconstruct_buy(entry: float, df_asof: pd.DataFrame, regime: str,
                    bt_level: dict | None) -> dict:
    """entry/target/stop for a BUY, via the real buy_stop/buy_target primitives.
    Stop ← ATR floor / nearest support; target ← shrunk backtest MFE capped at a
    p75 vol cone, else nearest resistance. {} when no defensible stop+target pair."""
    atr = atr14(df_asof)
    k = atr_k_for_regime(regime)
    sr = compute_sr(df_asof, entry)
    support_below = nearest_below(entry, [z["price"] for z in sr["support"]])
    stop = buy_stop(entry, support_below, atr, k)

    target = None
    if bt_level and bt_level.get("mfe_6m") is not None:
        mfe_level = entry * (1 + bt_level["mfe_6m"] / 100.0)
        target, _ = buy_target(entry, mfe_level, int(bt_level.get("n", 0)),
                               None, p75_cone(df_asof, entry, up=True))
    if target is None:  # fallback: nearest resistance above
        target = nearest_above(entry, [z["price"] for z in sr["resistance"]])
    if stop is None or target is None or not (target > entry > stop):
        return {}
    rr, _, _ = rr_triplet(entry, target, stop)
    return {"entry": entry, "target": round(target, 2), "stop": round(stop, 2),
            "rr": round(rr, 2), "reload_to": None}


def reconstruct_sell(entry: float, df_asof: pd.DataFrame) -> dict:
    """A trim is the mirror of a BUY: 'target' = reload (nearest support below, the
    favourable exit), 'stop' = nearest resistance above (the invalidation — price
    broke higher, the trim was premature). R = (entry−reload)/(resistance−entry)."""
    atr = atr14(df_asof)
    sr = compute_sr(df_asof, entry)
    reload_to = nearest_below(entry, [z["price"] for z in sr["support"]])
    stop_up = nearest_above(entry, [z["price"] for z in sr["resistance"]])
    if reload_to is None or stop_up is None or not (reload_to < entry < stop_up):
        return {}
    # Both legs need real room (≥ ½ ATR), else a level sitting on top of the entry
    # produces a degenerate R. Excludes the "resistance ≈ entry" artifacts.
    floor = 0.5 * (atr or 0)
    if (stop_up - entry) < floor or (entry - reload_to) < floor:
        return {}
    rr = (entry - reload_to) / (stop_up - entry)
    return {"entry": entry, "target": round(reload_to, 2), "stop": round(stop_up, 2),
            "rr": round(rr, 2), "reload_to": round(reload_to, 2)}


# ── ledger I/O ──────────────────────────────────────────────────────────────

def append_row(row: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_ledger(rows: list[dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                      encoding="utf-8")
