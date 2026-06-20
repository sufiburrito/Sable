#!/usr/bin/env python3
"""
Historical alert level backtest.

Simulates every alert level in a stock's config against historical daily Close
prices and computes forward return statistics — win rates, drawdowns, time to
recovery.

Usage:
    python3 backtest_levels.py TICKER [--period 5y] [--telegram] [--min-entries 2]

Valid periods: 2y, 3y, 5y, max  (default: 5y)
Output: analysis/TICKER_backtest.md
        analysis/TICKER_backtest.json  (sidecar for live bot floor hints)
"""
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from alert_bot.config import STOCKS_DIR, EXCLUDED_MD_FILES
from alert_bot.ohlc_cache import load_ohlc_cached
from alert_bot.parser import AlertLevel, StockConfig, load_all_stocks

OUTPUT_DIR = Path("analysis")
COOLDOWN_DAYS = 10   # min trading days between entries on the same level


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data(ticker: str, yf_symbol: str, period: str) -> pd.DataFrame:
    return load_ohlc_cached(ticker, yf_symbol, period)


# ---------------------------------------------------------------------------
# Crossing replication (mirrors engine.py _crosses exactly)
# ---------------------------------------------------------------------------

def _crosses(level: AlertLevel, prev: float, curr: float) -> bool:
    if level.alert_type == "BUY":
        return prev > level.upper and curr <= level.upper
    elif level.alert_type == "SELL":
        return prev < level.lower and curr >= level.lower
    elif level.alert_type == "WATCH":
        dropped_in = prev > level.upper and curr <= level.upper
        rose_in    = prev < level.lower and curr >= level.lower
        return dropped_in or rose_in
    return False


# ---------------------------------------------------------------------------
# Per-level simulation
# ---------------------------------------------------------------------------

def simulate_level(df: pd.DataFrame, level: AlertLevel) -> list[dict]:
    """
    Walk day-by-day over Close prices, detect crossings, compute forward stats.
    Returns a list of entry dicts (one per triggered crossing).
    """
    closes = df["Close"].values
    lows   = df["Low"].values
    dates  = df.index
    n      = len(df)

    entries = []
    last_entry_idx = -COOLDOWN_DAYS - 1  # allow first entry immediately

    for i in range(1, n):
        prev_c = float(closes[i - 1])
        curr_c = float(closes[i])

        if not _crosses(level, prev_c, curr_c):
            continue

        # Enforce 10-day cooldown between entries on the same level
        if i - last_entry_idx < COOLDOWN_DAYS:
            continue

        last_entry_idx = i
        entry_price = curr_c
        entry_date  = dates[i]

        # Forward return windows: 21d / 63d / 126d / 252d (trading days)
        def fwd_return(window: int) -> float | None:
            fwd_idx = i + window
            if fwd_idx >= n:
                return None
            return round((float(closes[fwd_idx]) - entry_price) / entry_price * 100, 2)

        r21  = fwd_return(21)
        r63  = fwd_return(63)
        r126 = fwd_return(126)
        r252 = fwd_return(252)

        # Max drawdown from entry to first Close >= entry_price (or end of data)
        # Uses daily Low to capture intraday depth
        breakeven_idx = None
        max_dd = 0.0
        for j in range(i + 1, n):
            low_j = float(lows[j])
            dd = (low_j - entry_price) / entry_price * 100
            if dd < max_dd:
                max_dd = dd
            if float(closes[j]) >= entry_price:
                breakeven_idx = j
                break

        days_to_green = (breakeven_idx - i) if breakeven_idx is not None else None

        # MFE (Maximum Favorable Excursion): peak return within 126 trading
        # days (~6 months).  Uses daily High to capture the best possible
        # exit — answers "how far did the winner run before pulling back?"
        highs = df["High"].values
        mfe_end = min(i + 126, n)
        if mfe_end > i + 1:
            peak_price = float(np.max(highs[i + 1 : mfe_end]))
            mfe = round((peak_price - entry_price) / entry_price * 100, 2)
        else:
            mfe = None

        entries.append({
            "date":          entry_date.strftime("%Y-%m-%d"),
            "entry_price":   round(entry_price, 2),
            "return_21d":    r21,
            "return_63d":    r63,
            "return_126d":   r126,
            "return_252d":   r252,
            "max_drawdown":  round(max_dd, 2),
            "days_to_green": days_to_green,
            "mfe_126d":      mfe,
        })

    return entries


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------

def compute_entry_stats(entries: list[dict], min_entries: int = 2) -> dict:
    n = len(entries)
    insufficient = n < min_entries

    if n == 0:
        return {
            "n":              0,
            "insufficient":   True,
            "win_rate_6m":    None,
            "median_6m":      None,
            "median_1y":      None,
            "median_dd":      None,
            "p75_dd":         None,
            "median_days":    None,
        }

    r126 = [e["return_126d"] for e in entries if e["return_126d"] is not None]
    r252 = [e["return_252d"] for e in entries if e["return_252d"] is not None]
    dds  = [e["max_drawdown"] for e in entries]
    dtg  = [e["days_to_green"] for e in entries if e["days_to_green"] is not None]
    mfes = [e["mfe_126d"] for e in entries if e["mfe_126d"] is not None]

    win_rate_6m = round(sum(1 for r in r126 if r > 0) / len(r126) * 100) if r126 else None

    # Expectancy: (win_rate × avg_win) - (loss_rate × avg_loss)
    # The single number that answers "on average, how much do I make per
    # entry at this level?" over a 6-month horizon.
    wins_6m   = [r for r in r126 if r > 0]
    losses_6m = [r for r in r126 if r <= 0]
    if wins_6m and losses_6m:
        wr = len(wins_6m) / len(r126)
        expectancy = round(
            wr * float(np.mean(wins_6m)) - (1 - wr) * abs(float(np.mean(losses_6m))),
            2,
        )
    else:
        expectancy = None

    return {
        "n":              n,
        "insufficient":   insufficient,
        "win_rate_6m":    win_rate_6m,
        "median_6m":      round(float(np.median(r126)), 1) if r126 else None,
        "median_1y":      round(float(np.median(r252)), 1) if r252 else None,
        "median_dd":      round(float(np.median(dds)), 1),
        "p75_dd":         round(float(np.percentile(dds, 75)), 1),
        "median_days":    int(np.median(dtg)) if dtg else None,
        "mfe_6m":         round(float(np.median(mfes)), 1) if mfes else None,
        "expectancy":     expectancy,
    }


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _fmt_pct(v: float | None, suffix: str = "%") -> str:
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v}{suffix}"


def _fmt_dd(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.1f}%"


def _level_label(level: AlertLevel) -> str:
    return f"₹{level.price_str.replace('₹','').strip()} {level.signal}"


def _conviction_star(stats: dict, all_buy_stats: list[dict]) -> bool:
    """True if this level has the best combined score (entries × win rate)."""
    if not all_buy_stats:
        return False
    def score(s: dict) -> float:
        n = s["n"]
        w = s["win_rate_6m"] or 0
        return n * w
    this_score = score(stats)
    best_score = max(score(s) for s in all_buy_stats)
    return this_score > 0 and this_score >= best_score


def _build_table(levels_results: list[tuple[AlertLevel, list[dict], dict]],
                 min_entries: int) -> list[str]:
    """
    Return markdown table rows for a group of levels.
    levels_results: list of (level, entries, stats) tuples.
    """
    lines = []
    lines.append("| Level | Entries | Win% (6M) | Median 6M | Median 1Y | Worst drawdown | Median days to green |")
    lines.append("|-------|---------|-----------|-----------|-----------|----------------|---------------------|")

    buy_stats = [s for lvl, _, s in levels_results if lvl.alert_type == "BUY"]

    for level, entries, stats in levels_results:
        label  = _level_label(level)
        n      = stats["n"]
        insuff = stats["insufficient"]
        star   = " ★" if level.alert_type == "BUY" and _conviction_star(stats, buy_stats) else ""

        if n == 0:
            lines.append(f"| {label} | 0 | — | — insufficient history — | — | — | — |")
        elif insuff:
            win_s = f"{stats['win_rate_6m']}%" if stats["win_rate_6m"] is not None else "—"
            lines.append(
                f"| {label} | {n} ⚠ | {win_s} | {_fmt_pct(stats['median_6m'])} | "
                f"{_fmt_pct(stats['median_1y'])} | {_fmt_dd(stats['median_dd'])} | "
                f"{stats['median_days'] or '—'}d |"
            )
        else:
            win_s = f"{stats['win_rate_6m']}%" if stats["win_rate_6m"] is not None else "—"
            lines.append(
                f"| {label}{star} | {n} | {win_s} | {_fmt_pct(stats['median_6m'])} | "
                f"{_fmt_pct(stats['median_1y'])} | {_fmt_dd(stats['median_dd'])} | "
                f"{stats['median_days'] or '—'}d |"
            )

    return lines


def format_report(ticker: str, results: list[tuple[AlertLevel, list[dict], dict]],
                  df: pd.DataFrame, period: str) -> str:
    run_date  = datetime.now().strftime("%Y-%m-%d")
    data_from = df.index[0].strftime("%Y-%m-%d")
    data_to   = df.index[-1].strftime("%Y-%m-%d")

    lines = [
        f"# Backtest: {ticker} — Level Validation",
        f"_Period: {period} | Data: {data_from} → {data_to} | Run: {run_date}_",
        "",
        f"⚠ = fewer than min_entries. ★ = highest conviction (sample × win rate).",
        "",
    ]

    for atype, heading, sort_rev in [
        ("BUY",   "## BUY Level Performance",  False),
        ("SELL",  "## SELL Level Performance", True),
        ("WATCH", "## WATCH Level Performance", False),
    ]:
        group = [(lvl, ents, stats) for lvl, ents, stats in results
                 if lvl.alert_type == atype]
        if not group:
            continue

        # Sort: BUY ascending by price (deep first), SELL descending
        group.sort(key=lambda x: x[0].lower, reverse=sort_rev)

        lines.append(heading)
        lines.extend(_build_table(group, min_entries=2))
        lines.append("")

    # Key findings
    lines.append("## Key Findings")

    buy_results = [(lvl, ents, stats) for lvl, ents, stats in results
                   if lvl.alert_type == "BUY" and stats["n"] > 0]

    if buy_results:
        # Strongest: best n × win_rate
        def buy_score(x):
            s = x[2]
            return (s["n"] * (s["win_rate_6m"] or 0))

        strongest = max(buy_results, key=buy_score)
        sl, _, ss = strongest
        lines.append(
            f"- **Strongest BUY:** {_level_label(sl)} "
            f"({ss['n']} entries, {ss['win_rate_6m']}% win at 6M, "
            f"median {_fmt_pct(ss['median_6m'])})"
        )

        # Best risk/reward: lowest absolute drawdown among well-sampled levels
        sampled = [(lvl, ents, s) for lvl, ents, s in buy_results if not s["insufficient"]]
        if sampled:
            best_rr = min(sampled, key=lambda x: abs(x[2]["median_dd"] or 0))
            rl, _, rs = best_rr
            lines.append(
                f"- **Best risk/reward:** {_level_label(rl)} "
                f"(worst drawdown {_fmt_dd(rs['median_dd'])}, "
                f"fastest to green {rs['median_days'] or '—'}d)"
            )
    else:
        lines.append("- No BUY entries found in this period.")

    insuff = [(lvl, stats) for lvl, _, stats in results if stats["n"] == 0]
    if insuff:
        labels = ", ".join(_level_label(lvl) for lvl, _ in insuff)
        lines.append(f"- **Insufficient history (0 entries):** {labels}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram summary
# ---------------------------------------------------------------------------

def send_telegram_summary(ticker: str,
                          results: list[tuple[AlertLevel, list[dict], dict]],
                          period: str) -> None:
    buy_results = [(lvl, ents, stats) for lvl, ents, stats in results
                   if lvl.alert_type == "BUY"]
    buy_results.sort(key=lambda x: x[0].lower)

    def score(s): return s["n"] * (s["win_rate_6m"] or 0)
    best_score = max((score(s) for _, _, s in buy_results), default=0)

    buy_lines = []
    for lvl, _, stats in buy_results:
        label = f"₹{lvl.price_str.replace('₹','').strip()}"
        n     = stats["n"]
        if n == 0:
            buy_lines.append(f"{lvl.signal} {label}  — insufficient history")
        else:
            star = " ★" if score(stats) >= best_score and best_score > 0 else ""
            win  = f"{stats['win_rate_6m']}% win 6M" if stats["win_rate_6m"] is not None else "—"
            dd   = f"worst dip {_fmt_dd(stats['median_dd'])}" if stats["median_dd"] is not None else ""
            buy_lines.append(f"{lvl.signal} {label}  {n} entries · {win} · {dd}{star}")

    buy_block = "\n".join(buy_lines) if buy_lines else "(no BUY levels)"

    text = (
        f"📊 <b>{ticker}</b> — Backtest ({period})\n\n"
        f"<b>BUY levels:</b>\n{buy_block}\n\n"
        f"★ = highest conviction (sample + win rate)\n"
        f"Full report: analysis/{ticker}_backtest.md"
    )

    if len(text) > 4096:
        text = text[:4090] + "…"

    subprocess.run(["python3", "send_message.py", text], check=True)
    print(f"  Discord message sent ({len(text)} chars)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(stock: StockConfig, period: str, min_entries: int,
        send_telegram: bool) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    print(f"  Loading data for {stock.ticker} ({period})…")
    df = load_data(stock.ticker, stock.yf_symbol, period)

    if df.empty or len(df) < 30:
        print(f"  Not enough data for {stock.ticker}, aborting.")
        return

    print(f"  {len(df)} trading days ({df.index[0].date()} → {df.index[-1].date()})")
    print(f"  Simulating {len(stock.levels)} alert level(s)…")

    results: list[tuple[AlertLevel, list[dict], dict]] = []
    for level in stock.levels:
        entries = simulate_level(df, level)
        stats   = compute_entry_stats(entries, min_entries=min_entries)
        results.append((level, entries, stats))
        n_str   = str(stats["n"]) + (" ⚠" if stats["insufficient"] else "")
        print(f"    {level.alert_type:5s} {level.price_str:15s} → {n_str} entries")

    report = format_report(stock.ticker, results, df, period)
    out_path = OUTPUT_DIR / f"{stock.ticker}_backtest.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\n  ✓  Report written → {out_path}")

    write_json_sidecar(stock.ticker, results, period)

    if send_telegram:
        send_telegram_summary(stock.ticker, results, period)


def write_json_sidecar(ticker: str,
                       results: list[tuple["AlertLevel", list[dict], dict]],
                       period: str) -> None:
    """
    Write analysis/TICKER_backtest.json — structured data consumed by
    alert_bot/floor_context.py to add floor/ceiling hints to live alerts.
    """
    levels_data = {}
    for level, _entries, stats in results:
        levels_data[level.price_str] = {
            "alert_type":   level.alert_type,
            "signal":       level.signal,
            "lower":        level.lower,
            "upper":        level.upper,
            # Spread the full stats dict — no more data loss between
            # compute_entry_stats() and what downstream consumers can see.
            **stats,
        }

    payload = {
        "period":   period,
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "levels":   levels_data,
    }
    json_path = OUTPUT_DIR / f"{ticker}_backtest.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    print(f"  ✓  JSON sidecar written → {json_path}")


if __name__ == "__main__":
    args = sys.argv[1:]

    # --telegram
    send_telegram = "--telegram" in args
    if send_telegram:
        args.remove("--telegram")

    # --period VALUE
    period = "5y"
    if "--period" in args:
        idx = args.index("--period")
        if idx + 1 >= len(args):
            print("Error: --period requires a value (e.g. --period 5y)")
            sys.exit(1)
        period = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    valid_periods = {"2y", "3y", "5y", "max"}
    if period not in valid_periods:
        print(f"Error: unknown period '{period}'. Valid: {', '.join(sorted(valid_periods))}")
        sys.exit(1)

    # --min-entries VALUE
    min_entries = 2
    if "--min-entries" in args:
        idx = args.index("--min-entries")
        if idx + 1 >= len(args):
            print("Error: --min-entries requires a value")
            sys.exit(1)
        try:
            min_entries = int(args[idx + 1])
        except ValueError:
            print("Error: --min-entries must be an integer")
            sys.exit(1)
        args = args[:idx] + args[idx + 2:]

    # Positional: TICKER
    if not args:
        print("Usage: python3 backtest_levels.py TICKER [--period 5y] [--telegram] [--min-entries 2]")
        sys.exit(1)

    ticker_arg = args[0].upper()

    all_stocks = load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)
    known      = {s.ticker: s for s in all_stocks}

    if ticker_arg in known:
        stock = known[ticker_arg]
    else:
        from alert_bot.parser import StockConfig
        print(f"  Note: {ticker_arg} not in stocks/ — using auto-config ({ticker_arg}.NS, no alert levels)")
        stock = StockConfig(ticker=ticker_arg, yf_symbol=f"{ticker_arg}.NS",
                            name=ticker_arg, core_pct=0)

    if not stock.levels:
        print(f"  No alert levels found for {stock.ticker}. Nothing to backtest.")
        sys.exit(0)

    print(f"\nBacktesting {stock.ticker} alert levels [{period}]…\n")
    run(stock, period=period, min_entries=min_entries, send_telegram=send_telegram)
    print("\nDone.")
