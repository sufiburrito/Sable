#!/usr/bin/env python3
"""
Retrospective floor analysis.

Finds every historical price floor for each stock (local minimum followed
by a significant recovery), then analyses what signals were present in the
candles at and leading up to that floor.

Usage:
    python3 retrospective_analysis.py             # all stocks
    python3 retrospective_analysis.py BBOX SUVEN  # specific tickers

Output:
    analysis/TICKER_floors.md   — per-stock detailed floor report
    FLOOR_SIGNALS.md            — synthesised cross-stock findings (updated in place)
"""
import sys
from datetime import datetime, date
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from alert_bot.config import STOCKS_DIR, EXCLUDED_MD_FILES, FEEDBACK_LOG
from alert_bot.feedback import FeedbackStore, POSITIVE_OUTCOMES, NEGATIVE_OUTCOMES, ENGAGEMENT
from alert_bot.ohlc_cache import load_ohlc_cached as _load_ohlc_cached
from alert_bot.parser import load_all_stocks, StockConfig

OUTPUT_DIR = Path("analysis")


# ── Tuning knobs ────────────────────────────────────────────────────────────
FLOOR_WINDOW        = 5      # candles either side to qualify as a local minimum
MIN_RECOVERY_PCT    = 0.08   # floor must be followed by ≥8% rally within RECOVERY_BARS
RECOVERY_BARS       = 25     # look-forward window for recovery (trading days)
PERIOD              = "2y"   # history to analyse — override with --period flag
MA_PROXIMITY_PCT    = 0.025  # within 2.5% = "near an MA"
SUPPORT_PROXIMITY   = 0.03   # within 3% = "at a known support zone"
HIGH_VOLUME_MULT    = 1.5    # floor candle volume > 1.5x avg = notable


# ---------------------------------------------------------------------------
# Candlestick helpers
# ---------------------------------------------------------------------------

def candle_metrics(o: float, h: float, l: float, c: float) -> dict:
    """Decompose a single candle into wick/body ratios."""
    body       = abs(c - o)
    full_range = h - l
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return {
        "body":             body,
        "full_range":       full_range,
        "lower_wick":       lower_wick,
        "upper_wick":       upper_wick,
        "wick_ratio":       round(lower_wick / full_range, 3) if full_range else 0,
        "body_ratio":       round(body / full_range, 3) if full_range else 0,
        "close_position":   round((c - l) / full_range, 3) if full_range else 0,
        "bullish":          c >= o,
    }


def classify_pattern(o: float, h: float, l: float, c: float,
                      prev_o: float | None, prev_h: float | None,
                      prev_l: float | None, prev_c: float | None,
                      atr: float) -> str:
    """Return the most significant bullish reversal pattern present, or 'None'."""
    m = candle_metrics(o, h, l, c)
    body = m["body"]
    full = m["full_range"]
    lw   = m["lower_wick"]
    uw   = m["upper_wick"]

    if full == 0:
        return "Doji (flat)"

    # Dragonfly doji: tiny body near high, huge lower wick
    if body < 0.1 * full and lw > 0.7 * full:
        return "Dragonfly Doji ★★★"

    # Hammer (bullish): lower wick > 2x body, small upper wick, body at top
    if lw >= 2 * body and uw <= body and m["close_position"] > 0.5:
        return "Hammer ★★★" if m["bullish"] else "Hanging Man (bearish)"

    # Inverted hammer (watch only — needs next candle confirmation)
    if uw >= 2 * body and lw <= body and m["close_position"] < 0.5:
        return "Inverted Hammer ★ (needs confirmation)"

    # Bullish engulfing — needs previous candle
    if (prev_o is not None and prev_c is not None and
            c > o and prev_c < prev_o and           # current green, prev red
            o <= prev_c and c >= prev_o):            # body engulfs prev body
        return "Bullish Engulfing ★★★"

    # Tweezer bottom — similar lows on consecutive candles
    if (prev_l is not None and abs(l - prev_l) < atr * 0.1 and
            c > o):  # current is green
        return "Tweezer Bottom ★★"

    # Strong bullish candle with notable lower wick
    if m["bullish"] and lw > 0.3 * full and m["close_position"] > 0.6:
        return "Bullish candle + lower wick ★"

    return "No clear pattern"


# ---------------------------------------------------------------------------
# Floor detection
# ---------------------------------------------------------------------------

def find_floors(df: pd.DataFrame) -> list[int]:
    """
    Return indices of candles that are:
      - Local minimum of Low over [i-FLOOR_WINDOW .. i+FLOOR_WINDOW]
      - Followed by ≥MIN_RECOVERY_PCT rally within RECOVERY_BARS candles
    """
    n = len(df)
    floors = []
    for i in range(FLOOR_WINDOW, n - FLOOR_WINDOW):
        low_i = float(df["Low"].iloc[i])
        window_lows = df["Low"].iloc[i - FLOOR_WINDOW: i + FLOOR_WINDOW + 1]
        if float(window_lows.min()) < low_i:
            continue  # not a local minimum

        # Check recovery within RECOVERY_BARS
        forward = min(i + RECOVERY_BARS, n)
        future_high = float(df["High"].iloc[i + 1: forward].max()) if i + 1 < forward else 0
        if (future_high - low_i) / low_i >= MIN_RECOVERY_PCT:
            floors.append(i)

    return floors


# ---------------------------------------------------------------------------
# Signal analysis at a floor
# ---------------------------------------------------------------------------

def analyse_floor(df: pd.DataFrame, idx: int, atr: float,
                  ma_levels: dict, support_zones: list[dict]) -> dict:
    """Analyse signals at floor candle idx. Returns a dict of findings."""
    row  = df.iloc[idx]
    o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
    vol  = int(row["Volume"])
    date = df.index[idx]

    # Previous candle for pattern detection
    if idx > 0:
        pr = df.iloc[idx - 1]
        prev_o, prev_h, prev_l, prev_c = float(pr["Open"]), float(pr["High"]), float(pr["Low"]), float(pr["Close"])
    else:
        prev_o = prev_h = prev_l = prev_c = None

    m       = candle_metrics(o, h, l, c)
    pattern = classify_pattern(o, h, l, c, prev_o, prev_h, prev_l, prev_c, atr)

    # Volume vs rolling 20-day average
    avg_vol = float(df["Volume"].iloc[max(0, idx - 20):idx].mean()) if idx > 0 else vol
    vol_mult = round(vol / avg_vol, 2) if avg_vol > 0 else 1.0

    # RSI at floor (14-period, computed up to this candle)
    close_series = df["Close"].iloc[:idx + 1]
    rsi = _rsi(close_series)

    # MA proximity
    ma_notes = []
    for label, level in ma_levels.items():
        if level and abs(l - level) / level <= MA_PROXIMITY_PCT:
            ma_notes.append(label)

    # Recovery
    n = len(df)
    forward  = min(idx + RECOVERY_BARS, n)
    future_h = float(df["High"].iloc[idx + 1: forward].max()) if idx + 1 < n else l
    recovery = round((future_h - l) / l * 100, 1)

    # Recovery shape classification — how quickly price recovers from the floor
    post_floor = df["Close"].iloc[idx: min(idx + RECOVERY_BARS, n)]
    if len(post_floor) >= 10 and l > 0:
        rec_5d = (float(post_floor.iloc[min(4, len(post_floor)-1)]) - l) / l * 100
        rec_25d = (float(post_floor.iloc[-1]) - l) / l * 100
        if rec_5d > 5.0:
            recovery_shape = "V-snap"    # sharp bounce — act within days
        elif rec_25d < 3.0:
            recovery_shape = "L-flat"    # zone may be breaking down — wait
        else:
            recovery_shape = "U-grind"   # slow recovery — accumulate over weeks
    else:
        recovery_shape = "—"

    # Nearest support zone
    nearest_zone = None
    nearest_dist = float("inf")
    for z in support_zones:
        dist = abs(l - z["price"]) / l
        if dist < nearest_dist:
            nearest_dist = dist
            nearest_zone = z
    at_support = nearest_dist <= SUPPORT_PROXIMITY

    return {
        "date":         date.strftime("%Y-%m-%d"),
        "price_low":    round(l, 2),
        "price_close":  round(c, 2),
        "pattern":      pattern,
        "wick_ratio":   m["wick_ratio"],   # lower wick / full range
        "close_pos":    m["close_position"],  # 1.0 = close at high
        "bullish":      m["bullish"],
        "volume":       vol,
        "vol_mult":     vol_mult,
        "high_vol":     vol_mult >= HIGH_VOLUME_MULT,
        "rsi":          rsi,
        "oversold":     rsi < 35 if rsi else False,
        "ma_notes":     ma_notes,
        "at_support":   at_support,
        "nearest_zone": nearest_zone,
        "nearest_dist_pct": round(nearest_dist * 100, 1),
        "recovery_pct": recovery,
        "recovery_shape": recovery_shape,
    }


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    rsi      = 100 - 100 / (1 + rs)
    return round(float(rsi.iloc[-1]), 1)


def _rolling_ma(close: pd.Series, n: int) -> float | None:
    if len(close) < n:
        return None
    return round(float(close.rolling(n).mean().iloc[-1]), 2)


# ---------------------------------------------------------------------------
# Support zone detection (simplified — swing low clusters from full history)
# ---------------------------------------------------------------------------

CLUSTER_PCT = 0.025

def _detect_support_zones(df: pd.DataFrame) -> list[dict]:
    lows = []
    window = 5
    n = len(df)
    for i in range(window, n - window):
        l = float(df["Low"].iloc[i])
        wl = df["Low"].iloc[i - window: i + window + 1]
        if float(wl.min()) >= l:
            lows.append(l)

    if not lows:
        return []

    lows_sorted = sorted(lows)
    groups: list[list[float]] = [[lows_sorted[0]]]
    for p in lows_sorted[1:]:
        ref = np.mean(groups[-1])
        if abs(p - ref) / ref <= CLUSTER_PCT:
            groups[-1].append(p)
        else:
            groups.append([p])

    zones = []
    for g in groups:
        zones.append({
            "price":   round(float(np.mean(g)), 0),
            "touches": len(g),
        })
    return zones


# ---------------------------------------------------------------------------
# Markdown report builders
# ---------------------------------------------------------------------------

def stars(f: dict) -> str:
    """Quick signal quality score based on how many signals aligned."""
    score = 0
    if f["wick_ratio"] >= 0.4:        score += 1
    if f["close_pos"]  >= 0.6:        score += 1
    if f["bullish"]:                   score += 1
    if f["high_vol"]:                  score += 1
    if f["oversold"]:                  score += 1
    if f["at_support"]:                score += 1
    if f["ma_notes"]:                  score += 1
    if "★★★" in f["pattern"]:         score += 2
    elif "★★" in f["pattern"]:        score += 1
    elif "★" in f["pattern"]:         score += 1
    return "★" * min(score, 5)


def write_ticker_report(ticker: str, floors: list[dict], df: pd.DataFrame, period: str = PERIOD) -> str:
    lines = [f"# {ticker} — Historical Floor Analysis\n"]
    lines.append(f"Period: {df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}  "
                 f"({len(df)} trading days, --period {period})\n")
    lines.append(f"Floors found: {len(floors)}  "
                 f"(local min + ≥{MIN_RECOVERY_PCT*100:.0f}% recovery within {RECOVERY_BARS} days)\n")
    lines.append("---\n")

    if not floors:
        lines.append("_No qualifying floors found in this period._\n")
        return "\n".join(lines)

    lines.append("| Date | Low | Recovery | Shape | Pattern | Wick% | Close pos | Vol mult | RSI | MA proximity | At support | Promoter | You | Score |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for f in floors:
        ma_str = ", ".join(f["ma_notes"]) if f["ma_notes"] else "—"
        sup_str = "✓" if f["at_support"] else f"~{f['nearest_dist_pct']}% away"
        pb = f.get("promoter_bought")
        pc = f.get("promoter_pct_change")
        ql = f.get("quarter_label", "—")
        if pb is True:
            promo_str = f"↑ {pc:+.2f}pp ({ql})"
        elif pb is False:
            promo_str = f"↓ {pc:+.2f}pp ({ql})"
        else:
            promo_str = "—"
        emojis = f.get("user_emojis", [])
        react_str = " ".join(emojis) if emojis else "—"
        lines.append(
            f"| {f['date']} | ₹{f['price_low']:,.0f} | +{f['recovery_pct']}% "
            f"| {f.get('recovery_shape', '—')} "
            f"| {f['pattern']} | {f['wick_ratio']:.0%} | {f['close_pos']:.0%} "
            f"| {f['vol_mult']}x | {f['rsi'] or '—'} | {ma_str} | {sup_str} "
            f"| {promo_str} | {react_str} | {stars(f)} |"
        )

    lines.append("\n---\n")
    lines.append("## Signal frequency across all floors\n")

    total = len(floors)
    def pct(n): return f"{n}/{total} ({n/total*100:.0f}%)"

    wick_strong   = sum(1 for f in floors if f["wick_ratio"] >= 0.4)
    close_high    = sum(1 for f in floors if f["close_pos"]  >= 0.6)
    bullish_c     = sum(1 for f in floors if f["bullish"])
    high_vol      = sum(1 for f in floors if f["high_vol"])
    oversold      = sum(1 for f in floors if f["oversold"])
    at_support    = sum(1 for f in floors if f["at_support"])
    has_ma        = sum(1 for f in floors if f["ma_notes"])
    strong_pat    = sum(1 for f in floors if "★★" in f["pattern"])

    # Promoter signal — denominator is floors WITH data, not total floors
    promo_data    = [f for f in floors if f.get("promoter_bought") is not None]
    promo_bought  = sum(1 for f in promo_data if f["promoter_bought"] is True)
    promo_sold    = sum(1 for f in promo_data if f["promoter_bought"] is False)
    promo_total   = len(promo_data)

    def promo_pct(n: int) -> str:
        if promo_total == 0:
            return "no data"
        return f"{n}/{promo_total} ({n/promo_total*100:.0f}% of floors with data)"

    # Recovery shape counts
    v_snap  = sum(1 for f in floors if f.get("recovery_shape") == "V-snap")
    u_grind = sum(1 for f in floors if f.get("recovery_shape") == "U-grind")
    l_flat  = sum(1 for f in floors if f.get("recovery_shape") == "L-flat")

    lines.append(f"- Lower wick ≥40% of candle range:  {pct(wick_strong)}")
    lines.append(f"- Close in upper 40% of range:      {pct(close_high)}")
    lines.append(f"- Bullish close (green candle):      {pct(bullish_c)}")
    lines.append(f"- High volume (≥1.5x avg):           {pct(high_vol)}")
    lines.append(f"- RSI ≤35 (oversold):                {pct(oversold)}")
    lines.append(f"- Within 3% of a support zone:       {pct(at_support)}")
    lines.append(f"- Near an MA (within 2.5%):          {pct(has_ma)}")
    lines.append(f"- Strong candlestick pattern (★★+):  {pct(strong_pat)}")
    lines.append(f"- Promoter net buying (same quarter): {promo_pct(promo_bought)}")
    lines.append(f"- Promoter net selling (same quarter): {promo_pct(promo_sold)}")
    lines.append(f"\n### Recovery shape profile\n")
    lines.append(f"- V-snap (>5% in 5 days):   {pct(v_snap)}")
    lines.append(f"- U-grind (slow 2-4 weeks): {pct(u_grind)}")
    lines.append(f"- L-flat (<3% in 25 days):  {pct(l_flat)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# User reaction helpers
# ---------------------------------------------------------------------------

_REACT_PRICE_TOL  = 0.05   # alert price within 5% of floor low
_REACT_DATE_DAYS  = 7      # alert fired within 7 days of floor date


def _attach_reactions(floors: list[dict], ticker: str) -> None:
    """
    For each floor, find the closest BUY alert in feedback.jsonl that fired
    within _REACT_DATE_DAYS days and _REACT_PRICE_TOL of the floor low.
    Attach the best reaction (positive > negative > engagement > watching)
    and the full list of emojis seen.

    Fields added to each floor dict:
        user_reaction   str | None   — best meaning (e.g. "profitable")
        user_emojis     list[str]    — all emojis reacted on the closest alert
    """
    store = FeedbackStore(FEEDBACK_LOG)
    records = store.load_for_ticker(ticker)

    # Keep only BUY records (reactions on SELL/WATCH are less relevant to floor quality)
    buy_records = [r for r in records if r.get("alert_type") == "BUY"
                   and not r.get("_removed")]

    for f in floors:
        floor_dt = pd.Timestamp(f["date"])
        floor_low = f["price_low"]

        best_meaning = None
        best_emojis: list[str] = []

        for r in buy_records:
            try:
                alert_dt = pd.Timestamp(r["fired_at"]).tz_localize(None)
            except Exception:
                continue

            alert_price = r.get("price")
            if alert_price is None:
                continue

            days_apart  = abs((alert_dt - floor_dt).days)
            price_diff  = abs(alert_price - floor_low) / floor_low

            if days_apart <= _REACT_DATE_DAYS and price_diff <= _REACT_PRICE_TOL:
                emoji   = r.get("emoji", "")
                meaning = r.get("meaning", "")
                best_emojis.append(emoji)

                # Priority: positive > negative > engagement > watching
                if meaning in POSITIVE_OUTCOMES:
                    best_meaning = meaning
                elif meaning in NEGATIVE_OUTCOMES and best_meaning not in POSITIVE_OUTCOMES:
                    best_meaning = meaning
                elif meaning in ENGAGEMENT and best_meaning is None:
                    best_meaning = meaning
                elif best_meaning is None:
                    best_meaning = meaning

        f["user_reaction"] = best_meaning
        f["user_emojis"]   = best_emojis


# ---------------------------------------------------------------------------
# Promoter signal helpers
# ---------------------------------------------------------------------------

def _load_promoter_cache(ticker: str) -> "pd.DataFrame | None":
    """
    Return quarterly promoter data for ticker.
    Primary: market.db promoter_holdings table (full history).
    Fallback: analysis/TICKER_promoter.csv (pre-DB behaviour).
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(OUTPUT_DIR.parent))
        import market_db as _mdb
        conn = _mdb.get_conn()
        df = _mdb.query_promoter_trend(conn, ticker, n_quarters=12)
        conn.close()
        if df is not None:
            return df
    except Exception:
        pass

    # Fallback to CSV
    cache_path = OUTPUT_DIR / f"{ticker}_promoter.csv"
    if not cache_path.exists():
        return None
    try:
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df
    except Exception as e:
        print(f"    Warning: could not load promoter cache: {e}")
        return None


def _quarter_end(dt: pd.Timestamp) -> pd.Timestamp:
    """Return the quarter-end date (Mar/Jun/Sep/Dec 31/30) for a given date."""
    m = dt.month
    y = dt.year
    if m <= 3:  return pd.Timestamp(y, 3, 31)
    if m <= 6:  return pd.Timestamp(y, 6, 30)
    if m <= 9:  return pd.Timestamp(y, 9, 30)
    return pd.Timestamp(y, 12, 31)


def _promoter_signal(floor_date: pd.Timestamp,
                     promoter_df: pd.DataFrame) -> dict:
    """
    Return the promoter net-buying/selling signal for the quarter containing
    floor_date.

    promoter_df index = quarter-end dates; 'promoter_pct' column = stake %.

    Returns:
        promoter_bought      True | False | None
                             True  = promoters net bought this quarter
                             False = promoters net sold
                             None  = no data or change within noise threshold
        promoter_pct_change  float | None  (positive = bought, negative = sold)
        quarter_label        "Jun 2024" style label, or "—" if no match
    """
    target_qe = _quarter_end(floor_date)

    # Find the closest quarter-end row in cache (allow ±15 days for data lag)
    diffs_days = pd.Series(
        [(idx - target_qe).days for idx in promoter_df.index],
        index=promoter_df.index
    ).abs()
    nearest_pos = int(diffs_days.values.argmin())
    if diffs_days.iloc[nearest_pos] > 15 or nearest_pos == 0:
        return {"promoter_bought": None, "promoter_pct_change": None,
                "quarter_label": "—"}

    current_pct = float(promoter_df["promoter_pct"].iloc[nearest_pos])
    prior_pct   = float(promoter_df["promoter_pct"].iloc[nearest_pos - 1])
    change      = round(current_pct - prior_pct, 2)
    label       = promoter_df.index[nearest_pos].strftime("%b %Y")

    # Treat moves < 0.05pp as rounding noise
    if abs(change) < 0.05:
        return {"promoter_bought": None, "promoter_pct_change": 0.0,
                "quarter_label": label}

    return {
        "promoter_bought":     change > 0,
        "promoter_pct_change": change,
        "quarter_label":       label,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(stocks: list[StockConfig], period: str = PERIOD, use_reactions: bool = False):
    OUTPUT_DIR.mkdir(exist_ok=True)
    all_findings: dict[str, list[dict]] = {}

    for stock in stocks:
        print(f"  Analysing {stock.ticker}…")
        try:
            df = _load_ohlc_cached(stock.ticker, stock.yf_symbol, period)
        except Exception as e:
            print(f"    ERROR fetching data: {e}")
            continue

        if df.empty or len(df) < 30:
            print(f"    Not enough data, skipping.")
            continue

        # Compute MAs on full history
        close = df["Close"]
        ma_levels = {
            "20d MA":  _rolling_ma(close, 20),
            "50d MA":  _rolling_ma(close, 50),
            "200d MA": _rolling_ma(close, 200),
        }

        atr_series = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - df["Close"].shift()).abs(),
            (df["Low"]  - df["Close"].shift()).abs(),
        ], axis=1).max(axis=1)
        atr = float(atr_series.ewm(span=14, adjust=False).mean().iloc[-1])

        support_zones = _detect_support_zones(df)
        floor_indices = find_floors(df)

        floors = []
        for idx in floor_indices:
            # Compute MAs up to this candle for contextual accuracy
            ma_at_floor = {
                "20d MA":  _rolling_ma(df["Close"].iloc[:idx+1], 20),
                "50d MA":  _rolling_ma(df["Close"].iloc[:idx+1], 50),
                "200d MA": _rolling_ma(df["Close"].iloc[:idx+1], 200),
            }
            f = analyse_floor(df, idx, atr, ma_at_floor, support_zones)
            floors.append(f)

        # Attach promoter signal to each floor (additive — never overwrites core fields)
        promoter_df = _load_promoter_cache(stock.ticker)
        if promoter_df is not None:
            print(f"    Promoter cache found — annotating floors…")
            for f in floors:
                sig = _promoter_signal(pd.Timestamp(f["date"]), promoter_df)
                f.update(sig)
        else:
            for f in floors:
                f["promoter_bought"]     = None
                f["promoter_pct_change"] = None
                f["quarter_label"]       = "—"

        # Attach user reaction feedback if requested
        if use_reactions:
            print(f"    Attaching reaction feedback…")
            _attach_reactions(floors, stock.ticker)
        else:
            for f in floors:
                f["user_reaction"] = None
                f["user_emojis"]   = []

        all_findings[stock.ticker] = floors

        # Write per-stock report
        report = write_ticker_report(stock.ticker, floors, df, period)
        out_path = OUTPUT_DIR / f"{stock.ticker}_floors.md"
        out_path.write_text(report, encoding="utf-8")
        print(f"    ✓  {len(floors)} floors → {out_path}")

    write_floor_signals(all_findings)


def write_floor_signals(all_findings: dict[str, list[dict]]):
    """Write/update FLOOR_SIGNALS.md with synthesised cross-stock findings."""
    lines = [
        "# FLOOR_SIGNALS — What the Data Shows Before a Floor\n",
        f"_Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n",
        "This file is updated by `retrospective_analysis.py` and iterated on manually.\n",
        "It describes the signals that were most consistently present at historical price floors",
        "across the active stock portfolio. Use this to calibrate alert level generation in Step 1.\n",
        "---\n",
        "## Per-stock signal frequencies\n",
    ]

    all_floors = []
    for ticker, floors in all_findings.items():
        all_floors.extend(floors)
        if not floors:
            lines.append(f"### {ticker}: no qualifying floors in 2Y window\n")
            continue
        total = len(floors)
        def pct(n): return f"{n/total*100:.0f}%"
        wick   = sum(1 for f in floors if f["wick_ratio"] >= 0.4)
        closeh = sum(1 for f in floors if f["close_pos"]  >= 0.6)
        hvol   = sum(1 for f in floors if f["high_vol"])
        overs  = sum(1 for f in floors if f["oversold"])
        supp   = sum(1 for f in floors if f["at_support"])
        ma_c   = sum(1 for f in floors if f["ma_notes"])
        pat    = sum(1 for f in floors if "★★" in f["pattern"])

        promo_data   = [f for f in floors if f.get("promoter_bought") is not None]
        promo_n      = len(promo_data)
        promo_bought = sum(1 for f in promo_data if f["promoter_bought"] is True)
        promo_sold   = sum(1 for f in promo_data if f["promoter_bought"] is False)
        def ppct(n): return f"{n}/{promo_n} ({n/promo_n*100:.0f}%)" if promo_n else "no data"

        lines.append(f"### {ticker} ({total} floors)\n")
        lines.append(f"| Signal | Frequency |")
        lines.append(f"|---|---|")
        lines.append(f"| Lower wick ≥40% of range | {pct(wick)} |")
        lines.append(f"| Close in upper 40% of range | {pct(closeh)} |")
        lines.append(f"| High volume (≥1.5x avg) | {pct(hvol)} |")
        lines.append(f"| RSI ≤35 at floor | {pct(overs)} |")
        lines.append(f"| Within 3% of support zone | {pct(supp)} |")
        lines.append(f"| Near an MA | {pct(ma_c)} |")
        lines.append(f"| Strong candlestick pattern | {pct(pat)} |")
        lines.append(f"| Promoter net buying (same quarter) | {ppct(promo_bought)} |")
        lines.append(f"| Promoter net selling (same quarter) | {ppct(promo_sold)} |\n")

    # Cross-stock aggregate
    if all_floors:
        total = len(all_floors)
        lines.append("---\n## Cross-stock aggregate\n")
        lines.append(f"Total floors analysed: {total} across {len(all_findings)} stocks\n")

        def cpct(n): return f"{n}/{total} ({n/total*100:.0f}%)"
        lines.append(f"- Lower wick ≥40%:      {cpct(sum(1 for f in all_floors if f['wick_ratio'] >= 0.4))}")
        lines.append(f"- Close in top 40%:     {cpct(sum(1 for f in all_floors if f['close_pos']  >= 0.6))}")
        lines.append(f"- High volume:          {cpct(sum(1 for f in all_floors if f['high_vol']))}")
        lines.append(f"- RSI ≤35:              {cpct(sum(1 for f in all_floors if f['oversold']))}")
        lines.append(f"- At support zone:      {cpct(sum(1 for f in all_floors if f['at_support']))}")
        lines.append(f"- Near an MA:           {cpct(sum(1 for f in all_floors if f['ma_notes']))}")
        lines.append(f"- Strong pattern:       {cpct(sum(1 for f in all_floors if '★★' in f['pattern']))}")

        # Promoter aggregate — denominator = floors with data only
        ap_data   = [f for f in all_floors if f.get("promoter_bought") is not None]
        ap_n      = len(ap_data)
        ap_bought = sum(1 for f in ap_data if f["promoter_bought"] is True)
        ap_sold   = sum(1 for f in ap_data if f["promoter_bought"] is False)
        def apct(n): return f"{n}/{ap_n} ({n/ap_n*100:.0f}%)" if ap_n else "no data"
        lines.append(f"- Promoter net buying:  {apct(ap_bought)}")
        lines.append(f"- Promoter net selling: {apct(ap_sold)}")

    lines.append("\n---\n## Instructions for Step 1 (to be filled after analysis)\n")
    lines.append("_Update this section manually after reviewing the per-stock floor reports._\n")
    lines.append("When the data supports it, add specific instructions here for how to use")
    lines.append("these signals in alert level generation (e.g. 'prefer zones where ≥2 of")
    lines.append("the top signals are present', 'discount support zones not confirmed by")
    lines.append("a wick-rejection candle', etc.).\n")

    out = Path("FLOOR_SIGNALS.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  ✓  Synthesised findings → {out}")


if __name__ == "__main__":
    all_stocks = load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)
    raw_args = sys.argv[1:]

    # Parse --react flag
    use_reactions = "--react" in raw_args
    if use_reactions:
        raw_args.remove("--react")
    elif "--no-react" in raw_args:
        raw_args.remove("--no-react")

    # Parse --period flag
    period = PERIOD
    if "--period" in raw_args:
        idx = raw_args.index("--period")
        if idx + 1 < len(raw_args):
            period = raw_args[idx + 1]
            raw_args = raw_args[:idx] + raw_args[idx + 2:]
        else:
            print("Error: --period requires a value (e.g. --period 5y)")
            sys.exit(1)

    valid_periods = {"1y", "2y", "3y", "5y", "max"}
    if period not in valid_periods:
        print(f"Error: unknown period '{period}'. Valid: {', '.join(sorted(valid_periods))}")
        sys.exit(1)

    ticker_args = [a.upper() for a in raw_args]
    if ticker_args:
        known = {s.ticker: s for s in all_stocks}
        stocks = []
        for t in ticker_args:
            if t in known:
                stocks.append(known[t])
            else:
                # Auto-construct a minimal StockConfig for tickers not yet in stocks/
                from alert_bot.parser import StockConfig
                stocks.append(StockConfig(ticker=t, yf_symbol=f"{t}.NS", name=t, core_pct=0))
                print(f"  Note: {t} not in stocks/ — using auto-config ({t}.NS, no alert levels)")
    else:
        stocks = all_stocks

    if use_reactions:
        print("Reaction feedback: enabled (--react)\n")

    print(f"Running retrospective floor analysis on {len(stocks)} stock(s) [{period}]…\n")
    run(stocks, period=period, use_reactions=use_reactions)
    print("\nDone. Review analysis/TICKER_floors.md and FLOOR_SIGNALS.md.")
