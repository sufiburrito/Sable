"""
Export a stock's historical data as a structured markdown file optimised for
Claude.ai to analyse and generate alert levels from.

What's in the export:
    Current snapshot, moving averages (20/50/200 daily + weekly), trend
    structure, support/resistance zones, volume profile, ATR-based zone-width
    guidance, distance-to-level table, existing alert levels, current Special
    Alerts, and OHLCV tables across 10 timeframes. A Prophet forecast is
    appended if the stock has ≥120 days of history.

Output sizing:
    Default (full): ~87 KB / stock — paste into Claude.ai for full level work.
    --slim:        ~5 KB / stock — analytics only, no OHLCV tables.
    Selective:     varies — only the requested timeframes' OHLCV sections.

Output files land in exports/TICKER.md.

Works for any NSE ticker (auto-constructs TICKER.NS), even ones not in the
local stocks/ config. The prompt header inside each export tells Claude.ai to
*use* the data — it intentionally does not prescribe methodology, so the
analysis voice on the other end stays free.

Usage:
    python3 export_stock.py                          # exports all stocks (full)
    python3 export_stock.py STLTECH                  # exports one stock (full)
    python3 export_stock.py STLTECH SUVEN BBOX       # exports specific stocks (full)

Optional flags (only use when explicitly requested):
    --slim                    omit all OHLCV tables; keep analytics only (~5 KB/stock)
    --timeframes 1y 5y max    include only these OHLCV sections (space-separated)

Valid timeframe keys: 1d  5d  1mo  3mo  6mo  1y  3y  5y  ytd  max
"""
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

from alert_bot.config import STOCKS_DIR, EXCLUDED_MD_FILES, MARKET_TIMEZONE
from alert_bot.parser import load_all_stocks, StockConfig

IST = pytz.timezone(MARKET_TIMEZONE)
OUTPUT_DIR = Path("exports")

SWING_WINDOW_DAILY  = 5   # candles either side for daily swing detection
SWING_WINDOW_WEEKLY = 3   # candles either side for weekly swing detection
CLUSTER_PCT = 0.025        # merge swing points within 2.5% of each other


# ---------------------------------------------------------------------------
# Technical indicator helpers
# ---------------------------------------------------------------------------

def compute_rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return round(float(rsi.iloc[-1]), 1)


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    prev = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev).abs(),
        (df["Low"]  - prev).abs(),
    ], axis=1).max(axis=1)
    return round(float(tr.ewm(span=period, adjust=False).mean().iloc[-1]), 2)


def rolling_ma(df: pd.DataFrame, n: int):
    if len(df) < n:
        return None
    return round(float(df["Close"].rolling(n).mean().iloc[-1]), 2)


# ---------------------------------------------------------------------------
# Swing detection & clustering
# ---------------------------------------------------------------------------

def detect_swings(df: pd.DataFrame, window: int) -> tuple[list, list]:
    """
    Returns (swing_highs, swing_lows) as lists of (date_str, price, volume).
    A swing high/low is a candle whose high/low is the extreme over
    [i-window .. i+window].
    """
    highs, lows = [], []
    n = len(df)
    for i in range(window, n - window):
        date  = df.index[i]
        h     = float(df["High"].iloc[i])
        l     = float(df["Low"].iloc[i])
        vol   = int(df["Volume"].iloc[i])
        window_h = df["High"].iloc[i - window : i + window + 1]
        window_l = df["Low"].iloc[i  - window : i + window + 1]
        if h >= float(window_h.max()):
            highs.append((date, h, vol))
        if l <= float(window_l.min()):
            lows.append((date, l, vol))
    return highs, lows


def cluster_levels(points: list) -> list[dict]:
    """
    Group swing points within CLUSTER_PCT of each other into zones.
    Each zone: {price, low, high, touches, avg_volume, latest_date}
    """
    if not points:
        return []
    sorted_pts = sorted(points, key=lambda x: x[1])
    groups: list[list] = [[sorted_pts[0]]]

    for pt in sorted_pts[1:]:
        ref = np.mean([p[1] for p in groups[-1]])
        if abs(pt[1] - ref) / ref <= CLUSTER_PCT:
            groups[-1].append(pt)
        else:
            groups.append([pt])

    result = []
    for g in groups:
        prices  = [p[1] for p in g]
        volumes = [p[2] for p in g]
        dates   = [p[0] for p in g]
        result.append({
            "price":       round(float(np.mean(prices)), 0),
            "low":         round(float(min(prices)), 0),
            "high":        round(float(max(prices)), 0),
            "touches":     len(g),
            "avg_volume":  int(np.mean(volumes)),
            "latest_date": max(dates),
        })
    return sorted(result, key=lambda x: x["price"])


def compute_undercut_stats(df: pd.DataFrame, support_zones: list[dict], atr14: float, lookahead: int = 10) -> list[dict]:
    """
    For each support zone, measure how far below zone_low the price historically dipped
    before recovering. Returns a list of dicts, one per zone, with undercut distribution.

    A "touch" is when the previous close was above the zone top and the current low
    enters or drops through the zone. The undercut depth is how far below zone_low
    the minimum Low went over the next `lookahead` bars.

    Zones with <2 touches fall back to ATR-based estimates.
    """
    results = []
    lows   = df["Low"].values
    closes = df["Close"].values

    for z in support_zones:
        zone_low  = z["low"]
        zone_high = z["high"]
        undercuts = []

        for i in range(1, len(df) - lookahead):
            prev_close = closes[i - 1]
            curr_low   = lows[i]
            # Touch from above: price was above zone top, now enters or breaches zone
            if prev_close > zone_high and curr_low <= zone_high:
                window_min = float(min(lows[i : i + lookahead]))
                depth = max(0.0, zone_low - window_min)
                undercuts.append(depth)

        touch_count = len(undercuts)
        if touch_count >= 2:
            sorted_uc  = sorted(undercuts)
            median_uc  = sorted_uc[len(sorted_uc) // 2]
            p75_uc     = sorted_uc[int(len(sorted_uc) * 0.75)]
            data_source = "historical"
        else:
            median_uc   = round(atr14 * 0.4, 1)
            p75_uc      = round(atr14 * 0.7, 1)
            data_source = "atr_estimate"

        results.append({
            "zone_low":          zone_low,
            "zone_high":         zone_high,
            "touch_count":       touch_count,
            "median_undercut":   round(float(median_uc), 1),
            "p75_undercut":      round(float(p75_uc), 1),
            "suggested_tranche": round(zone_low - p75_uc, 1),
            "data_source":       data_source,
        })

    return results


def volume_profile(df: pd.DataFrame, n_bins: int = 25) -> list[dict]:
    """Return top-5 high-volume price nodes across the full daily range."""
    lo = float(df["Low"].min())
    hi = float(df["High"].max())
    edges  = np.linspace(lo, hi, n_bins + 1)
    mids   = (edges[:-1] + edges[1:]) / 2
    vols   = np.zeros(n_bins)

    for _, row in df.iterrows():
        for i in range(n_bins):
            if row["Low"] <= mids[i] <= row["High"]:
                vols[i] += row["Volume"]

    nodes = sorted(
        [{"price": round(mids[i], 0), "volume": vols[i]} for i in range(n_bins)],
        key=lambda x: x["volume"],
        reverse=True,
    )
    return nodes[:5]


def trend_structure(highs: list, lows: list) -> str:
    """Classify trend from the last few swing highs and lows."""
    rh = [p[1] for p in sorted(highs, key=lambda x: x[0])[-3:]]
    rl = [p[1] for p in sorted(lows,  key=lambda x: x[0])[-3:]]
    if len(rh) < 2 or len(rl) < 2:
        return "Insufficient swing data"
    hh = all(rh[i] > rh[i-1] for i in range(1, len(rh)))
    hl = all(rl[i] > rl[i-1] for i in range(1, len(rl)))
    lh = all(rh[i] < rh[i-1] for i in range(1, len(rh)))
    ll = all(rl[i] < rl[i-1] for i in range(1, len(rl)))
    if hh and hl: return "UPTREND — Higher Highs + Higher Lows"
    if lh and ll: return "DOWNTREND — Lower Highs + Lower Lows"
    if hh and ll: return "EXPANDING — Higher Highs + Lower Lows"
    if lh and hl: return "CONTRACTING — Lower Highs + Higher Lows"
    return "SIDEWAYS / MIXED"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fp(p: float) -> str:
    return f"₹{p:,.0f}"

def fpf(p: float) -> str:
    return f"₹{p:,.2f}"

def fv(v: int) -> str:
    if v >= 10_000_000: return f"{v/10_000_000:.1f}Cr"
    if v >= 100_000:    return f"{v/100_000:.1f}L"
    if v >= 1_000:      return f"{v/1_000:.0f}K"
    return str(v)

def fpc(pct: float) -> str:
    return f"{'+'if pct>=0 else ''}{pct:.1f}%"

def zone_str(z: dict) -> str:
    return fp(z["price"]) if z["low"] == z["high"] else f"{fp(z['low'])}–{fp(z['high'])}"

def is_round(p: float) -> bool:
    return (round(p / 100) * 100 == p) or (round(p / 50) * 50 == p)


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------

def export_stock(stock: StockConfig, slim: bool = False, timeframes: set[str] | None = None) -> Path | None:
    print(f"  Fetching {stock.ticker} ({stock.yf_symbol})…")

    t = yf.Ticker(stock.yf_symbol)

    # All timeframes fetched independently
    tf: dict[str, pd.DataFrame] = {}
    TIMEFRAMES = [
        ("1d",   "1 Day",       "1d",  "5m"),
        ("5d",   "5 Days",      "5d",  "15m"),
        ("1mo",  "1 Month",     "1mo", "1d"),
        ("3mo",  "3 Months",    "3mo", "1d"),
        ("6mo",  "6 Months",    "6mo", "1d"),
        ("1y",   "1 Year",      "1y",  "1d"),
        ("3y",   "3 Years",     "3y",  "1wk"),
        ("5y",   "5 Years",     "5y",  "1wk"),
        ("ytd",  "Year to Date","ytd", "1d"),
        ("max",  "All Time",    "max", "1mo"),
    ]
    for key, _, period, interval in TIMEFRAMES:
        df = t.history(period=period, interval=interval)
        tf[key] = df

    # Use 1y daily as primary for analytics, 5y weekly for swing detection
    daily   = tf["1y"]
    weekly  = tf["5y"]

    if daily.empty:
        print(f"  WARNING: no data for {stock.ticker}, skipping.")
        return None

    curr = float(daily["Close"].iloc[-1])
    now  = datetime.now(IST)

    # 52-week stats from 1y daily
    h52  = float(daily["High"].max())
    l52  = float(daily["Low"].min())
    h52d = daily["High"].idxmax().strftime("%Y-%m-%d")
    l52d = daily["Low"].idxmin().strftime("%Y-%m-%d")

    # Moving averages
    ma20d  = rolling_ma(daily,  20)
    ma50d  = rolling_ma(daily,  50)
    ma200d = rolling_ma(daily,  200)
    ma20w  = rolling_ma(weekly, 20)
    ma50w  = rolling_ma(weekly, 50)

    # RSI, ATR
    rsi = compute_rsi(daily["Close"])
    atr = compute_atr(daily)
    atr_pct = round(atr / curr * 100, 1)

    # Swing points (1y daily + 5y weekly for richer S/R)
    sh_d, sl_d = detect_swings(daily,  SWING_WINDOW_DAILY)
    sh_w, sl_w = detect_swings(weekly, SWING_WINDOW_WEEKLY)

    res_zones = cluster_levels(sh_d + sh_w)
    sup_zones = cluster_levels(sl_d + sl_w)

    resistance = sorted([z for z in res_zones if z["price"] > curr], key=lambda x: x["price"])
    support    = sorted([z for z in sup_zones if z["price"] < curr], key=lambda x: x["price"], reverse=True)

    trend = trend_structure(sh_d, sl_d)
    hvn   = volume_profile(daily)

    # -----------------------------------------------------------------------
    # Build markdown
    # -----------------------------------------------------------------------
    L: list[str] = []

    def h2(t):  L.append(f"\n## {t}\n")
    def h3(t):  L.append(f"\n### {t}\n")
    def row(*cols): L.append("| " + " | ".join(str(c) for c in cols) + " |")
    def div(n): L.append("| " + " | ".join(["---"] * n) + " |")
    def ln(t=""): L.append(t)

    # Header
    L.append(f"# {stock.ticker} — Technical Data Export\n")
    L.append(f"**Generated:** {now.strftime('%Y-%m-%d %H:%M')} IST  |  "
             f"**Symbol:** {stock.yf_symbol}  |  **Core:** {stock.core_pct}%\n")
    L.append("> **Instructions for Claude.ai:** This file contains historical price data")
    L.append(f"> for {stock.ticker} across multiple timeframes. Use it to build your data")
    L.append("> bank for determining alert points for this stock. Regenerate your knowledgebase for this stock")
    L.append(f"and include all available news and community sentiment before finding alert levels.\n")
    L.append("---")

    # ── Snapshot ────────────────────────────────────────────────────────────
    h2("Current Snapshot")
    row("Metric", "Value"); div(2)
    row("Current Price",  fpf(curr))
    row("52W High",       f"{fp(h52)} ({h52d})  |  {fpc((curr-h52)/h52*100)} from here")
    row("52W Low",        f"{fp(l52)} ({l52d})  |  {fpc((curr-l52)/l52*100)} from here")
    row("ATR (14d)",      f"{fp(atr)} — {atr_pct}% of price  ← use for zone width")
    row("RSI (14d)",      f"{rsi}  {'(overbought)' if rsi>70 else '(oversold)' if rsi<30 else '(neutral)'}")

    # ── Moving Averages ──────────────────────────────────────────────────────
    h2("Moving Averages")
    row("MA", "Level", "vs Current Price"); div(3)
    for label, val in [("20-day", ma20d), ("50-day", ma50d), ("200-day", ma200d),
                        ("20-week", ma20w), ("50-week", ma50w)]:
        if val:
            row(label, fp(val), fpc((curr - val) / val * 100))

    # ── Trend ────────────────────────────────────────────────────────────────
    h2("Trend Structure (Daily)")
    ln(f"**{trend}**\n")
    if sh_d:
        recent_sh = sorted(sh_d, key=lambda x: x[0])[-5:]
        ln("Recent swing highs: " + "  →  ".join(
            f"{fp(h[1])} ({h[0].strftime('%b %y')})" for h in recent_sh))
    if sl_d:
        recent_sl = sorted(sl_d, key=lambda x: x[0])[-5:]
        ln("Recent swing lows:  " + "  →  ".join(
            f"{fp(l[1])} ({l[0].strftime('%b %y')})" for l in recent_sl))

    # ── S/R Zones ────────────────────────────────────────────────────────────
    h2("Key Support & Resistance Zones")
    ln("_Zones detected from daily + weekly swing points, clustered within 2.5%. "
       "More touches = stronger level. MA confluence and round numbers add significance._\n")

    def zone_notes(z: dict) -> str:
        notes = []
        if ma50d  and abs(z["price"] - ma50d)  / ma50d  < 0.025: notes.append("50d MA confluence")
        if ma200d and abs(z["price"] - ma200d) / ma200d < 0.025: notes.append("200d MA confluence")
        if ma20w  and abs(z["price"] - ma20w)  / ma20w  < 0.025: notes.append("20w MA confluence")
        if ma50w  and abs(z["price"] - ma50w)  / ma50w  < 0.025: notes.append("50w MA confluence")
        if abs(z["price"] - h52) / h52 < 0.02: notes.append("near 52W High")
        if abs(z["price"] - l52) / l52 < 0.02: notes.append("near 52W Low")
        if is_round(z["price"]): notes.append("round number")
        return ", ".join(notes) if notes else "—"

    h3(f"Resistance Zones (above {fp(curr)})")
    if resistance:
        row("Zone", "Centre", "Touches", "Avg Vol", "Last Seen", "Notes"); div(6)
        for z in resistance:
            row(zone_str(z), fp(z["price"]), z["touches"], fv(z["avg_volume"]),
                z["latest_date"].strftime("%Y-%m-%d"), zone_notes(z))
    else:
        ln("_None identified above current price._")

    h3(f"Support Zones (below {fp(curr)})")
    if support:
        row("Zone", "Centre", "Touches", "Avg Vol", "Last Seen", "Notes"); div(6)
        for z in support:
            row(zone_str(z), fp(z["price"]), z["touches"], fv(z["avg_volume"]),
                z["latest_date"].strftime("%Y-%m-%d"), zone_notes(z))
    else:
        ln("_None identified below current price._")

    # ── Volume Profile ───────────────────────────────────────────────────────
    h2("Volume Profile — Top 5 High-Volume Nodes (Daily, 6 months)")
    ln("_These price bands saw the most trading activity = strongest S/R._\n")
    row("Price Node", "Relative Volume"); div(2)
    max_vol = hvn[0]["volume"] if hvn else 1
    for node in hvn:
        bar = "█" * round(node["volume"] / max_vol * 12)
        row(fp(node["price"]), bar)

    # ── Distance table ───────────────────────────────────────────────────────
    h2(f"All Zones by Distance from Current Price ({fpf(curr)})")
    ln("_Closest zones are the most immediately actionable._\n")
    combined = (
        [(z, "RESISTANCE") for z in resistance] +
        [(z, "SUPPORT")    for z in support]
    )
    combined.sort(key=lambda x: abs(x[0]["price"] - curr))
    row("Zone", "Type", "Distance", "% Away", "Touches"); div(5)
    for z, ztype in combined[:14]:
        dist = z["price"] - curr
        row(zone_str(z), ztype, fp(abs(dist)), fpc(dist / curr * 100), z["touches"])

    # ── Zone width guidance ──────────────────────────────────────────────────
    h2("Zone Width Guidance (ATR-based)")
    ln(f"ATR = {fp(atr)} ({atr_pct}%). Use this to size price bands in the alert table:\n")
    ln(f"- **Tight zone:** ±{fp(atr*0.3)} around the level  (e.g. for a high-precision bounce)")
    ln(f"- **Normal zone:** ±{fp(atr*0.5)} around the level  ← recommended default")
    ln(f"- **Wide zone:** ±{fp(atr*0.75)} around the level  (for choppy / wide-ranging levels)\n")
    ln(f"Example: level at {fp(curr)} → normal band = {fp(curr - atr*0.5)}–{fp(curr + atr*0.5)}")

    # ── Undercut statistics ──────────────────────────────────────────────────
    h2("Zone Undercut Statistics (Support Zones)")
    ln("_How far below each zone floor prices historically dipped before recovering._")
    ln("_Use to calibrate second tranche placement. 75th pct = where 75% of dips bottom out._\n")
    undercut_stats = compute_undercut_stats(daily, sup_zones, atr)
    if undercut_stats:
        row("Zone", "Touches", "Median Undercut", "75th Pct Undercut", "Suggested 2nd Tranche", "Source"); div(6)
        for s in undercut_stats:
            if s["zone_low"] == s["zone_high"]:
                zone_display = fp(s["zone_low"])
            else:
                zone_display = f"{fp(s['zone_low'])}–{fp(s['zone_high'])}"
            pct_low = s["zone_low"] if s["zone_low"] > 0 else 1
            median_str = f"{fp(s['median_undercut'])} ({round(s['median_undercut'] / pct_low * 100, 1)}%)"
            p75_str    = f"{fp(s['p75_undercut'])} ({round(s['p75_undercut'] / pct_low * 100, 1)}%)"
            touch_str  = str(s["touch_count"]) if s["data_source"] == "historical" else "< 2"
            source_str = "✓ historical" if s["data_source"] == "historical" else "~ ATR est."
            row(zone_display, touch_str, median_str, p75_str, fp(s["suggested_tranche"]), source_str)
        ln(f"\n_ATR (14d) = {fp(atr)} — used as fallback estimate when historical touches < 2._")
    else:
        ln("_No support zones detected._")

    # ── Existing alert levels ────────────────────────────────────────────────
    h2("Existing Alert Levels (current config — do not duplicate)")
    if stock.levels:
        row("Signal", "Price", "Type", "Conf", "Message"); div(5)
        for lv in stock.levels:
            msg = lv.message[:65] + ("…" if len(lv.message) > 65 else "")
            row(lv.signal, lv.price_str, lv.alert_type, lv.confidence, msg)
    else:
        ln("_No levels configured yet — generate from scratch._")

    # ── Current Special Alerts ───────────────────────────────────────────────
    h2("Current Special Alerts (review EVENT rows against news findings)")
    if stock.calendar_alerts:
        row("Date", "Type", "Message"); div(3)
        for ca in stock.calendar_alerts:
            if ca.alert_type == "DATE" and ca.exact_date:
                date_str = ca.exact_date.strftime("%Y-%m-%d")
            elif ca.alert_type == "MONTH":
                date_str = f"{ca.year}-{ca.month:02d}"
            else:
                date_str = "EVENT"
            msg = ca.message[:80] + ("…" if len(ca.message) > 80 else "")
            row(date_str, ca.alert_type, msg)
        ln("")
        ln("_EVENT rows = undated watch triggers (VTODO in CalDAV). If news in this analysis confirms")
        ln("a date for any EVENT row: change Date to YYYY-MM-DD (or YYYY-MM), change Type to DATE (or")
        ln("MONTH). Copy the message text CHARACTER-FOR-CHARACTER — any change breaks the CalDAV UID._")
    else:
        ln("_No special alerts configured._")

    # ── Price Forecast (Prophet) ────────────────────────────────────────────
    try:
        from alert_bot.forecaster import prophet_forecast
        # Use the 1-year daily data for forecast (already loaded in tf["1y"])
        forecast_df = tf.get("1y")
        if forecast_df is not None and not forecast_df.empty and len(forecast_df) >= 120:
            fc = prophet_forecast(forecast_df["Close"])
            if fc:
                h2("Price Forecast (80% confidence interval)")
                ln("_Statistical forecast from Prophet model — use as one input alongside technicals._")
                ln("")
                row("Horizon", "Lower", "Predicted", "Upper", "Trend"); div(5)
                for h_days in sorted(fc.keys()):
                    vals = fc[h_days]
                    row(
                        f"{h_days} days",
                        f"₹{vals['lower']:,.0f}",
                        f"₹{vals['predicted']:,.0f}",
                        f"₹{vals['upper']:,.0f}",
                        f"{'↑' if vals['trend'] == 'up' else '↓' if vals['trend'] == 'down' else '→'} {vals['trend']}",
                    )
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"export: forecast section skipped: {e}")

    # ── OHLCV tables — all timeframes ────────────────────────────────────────
    if not slim:
        DATE_FMT = {
            "5m":  "%Y-%m-%d %H:%M",
            "15m": "%Y-%m-%d %H:%M",
            "1d":  "%Y-%m-%d",
            "1wk": "%Y-%m-%d",
            "1mo": "%Y-%m",
        }
        for key, label, _, interval in TIMEFRAMES:
            if timeframes is not None and key not in timeframes:
                continue
            df = tf[key]
            if df.empty:
                h2(f"OHLCV — {label} (no data)")
                continue
            fmt = DATE_FMT.get(interval, "%Y-%m-%d")
            h2(f"OHLCV — {label}  ({len(df)} candles, {interval} interval)")
            row("Date", "Open", "High", "Low", "Close", "Volume"); div(6)
            for date, r in df.iterrows():
                row(date.strftime(fmt),
                    fp(r["Open"]), fp(r["High"]), fp(r["Low"]), fp(r["Close"]),
                    fv(int(r["Volume"])))

    # ── Write ────────────────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / f"{stock.ticker}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

VALID_TF_KEYS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "3y", "5y", "ytd", "max"}


def main():
    all_stocks = load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)

    args = sys.argv[1:]
    slim = "--slim" in args
    args = [a for a in args if a != "--slim"]

    timeframes: set[str] | None = None
    if "--timeframes" in args:
        idx = args.index("--timeframes")
        tf_args = []
        for a in args[idx + 1:]:
            if a.startswith("--") or a in {s.ticker for s in all_stocks}:
                break
            tf_args.append(a)
        invalid = set(tf_args) - VALID_TF_KEYS
        if invalid:
            raise SystemExit(f"Unknown timeframe key(s): {invalid}. Valid: {VALID_TF_KEYS}")
        timeframes = set(tf_args)
        args = args[:idx] + args[idx + 1 + len(tf_args):]

    ticker_args = [a for a in args if not a.startswith("--")]
    if ticker_args:
        known = {s.ticker: s for s in all_stocks}
        stocks = []
        for t in ticker_args:
            if t in known:
                stocks.append(known[t])
            else:
                # Unknown ticker — build a minimal StockConfig (no levels/calendar)
                from alert_bot.parser import StockConfig
                stocks.append(StockConfig(
                    ticker=t,
                    yf_symbol=f"{t}.NS",
                    name=t,
                    core_pct=0,
                ))
                print(f"  Note: {t} not in config — exporting as {t}.NS with no existing levels.")
    else:
        stocks = all_stocks

    mode = "slim" if slim else (f"timeframes={timeframes}" if timeframes else "full")
    print(f"Exporting {len(stocks)} stock(s) → {OUTPUT_DIR}/  [{mode}]")
    for stock in stocks:
        path = export_stock(stock, slim=slim, timeframes=timeframes)
        if path:
            size_kb = round(path.stat().st_size / 1024, 1)
            print(f"  ✓  {path}  ({size_kb} KB)")
    print("Done.")


if __name__ == "__main__":
    main()
