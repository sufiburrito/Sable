#!/usr/bin/env python3
"""
multibagger_screener.py — Three-phase deep inference pipeline for multibagger candidates.

Phase 1: Technical Momentum
    Minervini Trend Template (8 criteria), IBD RS percentile, Mansfield RS,
    Weinstein Stage, Volume Contraction (VCP), RSI.

Phase 2: Smart Money Cross-Reference
    market.db insider_trades + party_profiles — promoter accumulation,
    coordinated buys, very_high confidence parties. Cross-ref discovery_watchlist.

Phase 3: Fundamental Quality (top 20 candidates only)
    Screener.in via fetch_fundamentals (60-day TTL, silent skip if unavailable).
    Partial Piotroski F-Score + ROCE quality + revenue/profit growth flags.

Usage:
    python3 multibagger_screener.py              # full run (all 3 phases)
    python3 multibagger_screener.py --quick      # Phase 1+2 only, no Screener.in
    python3 multibagger_screener.py --ticker IDEAFORGE  # single-ticker debug

Output: data/multibagger_scan.json + console table.
Run from project root (relative paths: analysis/, data/).
"""

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# Reuse project modules — no duplication of logic
from alert_bot.ohlc_cache import load_ohlc_cached
from alert_bot.confidence import _weinstein_stage, _compute_rsi, _load_nifty
import market_db as mdb

# ── Paths ─────────────────────────────────────────────────────────────────────

UNIVERSE_PATH  = Path("data/stock_universe.json")
WATCHLIST_PATH = Path("data/discovery_watchlist.json")
OUTPUT_PATH    = Path("data/multibagger_scan.json")

# ── Constants ─────────────────────────────────────────────────────────────────

# Phase 1 gate: must satisfy at least one condition to advance to Phase 2
_MIN_MINERVINI  = 4    # Minervini criteria passed (out of 8)
_MIN_IBD_RANK   = 65   # IBD RS percentile rank (0-100)

# Number of candidates sent to Phase 3 (Screener.in fetch)
_PHASE3_TOP_N   = 20

# Insider tiers in market.db that indicate promoter/director activity
_PROMOTER_TIERS = {"promoter", "director"}

# Theme bucket keywords that align with the user's active portfolio sectors
_ALIGNED_KEYWORDS = {
    "defense", "defence", "it", "telecom", "power", "infrastructure",
    "mining", "pharma", "agriculture", "agrochemical", "finance",
    "electric vehicle", "ev", "renewable",
}


# ── Universe loader ───────────────────────────────────────────────────────────

def _load_universe() -> list[dict]:
    """
    Flatten stock_universe.json into [{ticker, sector_bucket, tier_label}].
    Skips tickers listed in _meta.portfolio_excluded.
    """
    data     = json.loads(UNIVERSE_PATH.read_text())
    excluded = set(data.get("_meta", {}).get("portfolio_excluded", []))
    stocks   = []
    for bucket, tiers in data.get("themes", {}).items():
        for tier_label in ("tier1", "tier2"):
            for entry in tiers.get(tier_label, []):
                ticker = entry.get("ticker", "").upper().strip()
                if ticker and ticker not in excluded:
                    stocks.append({
                        "ticker":        ticker,
                        "sector_bucket": bucket,
                        "tier_label":    tier_label,
                    })
    return stocks


def _sector_aligned(sector_bucket: str) -> bool:
    """True when the theme bucket overlaps the user's active portfolio sectors."""
    low = sector_bucket.lower()
    return any(kw in low for kw in _ALIGNED_KEYWORDS)


# ── Technical helpers ─────────────────────────────────────────────────────────

def _roc(close: np.ndarray, n: int) -> float:
    """Rate-of-change over n periods as a percentage. Returns 0.0 if insufficient data."""
    if len(close) < n + 1:
        return 0.0
    return float((close[-1] / close[-(n + 1)] - 1) * 100.0)


def _phase1_technical(ticker: str, nifty_df: "pd.DataFrame | None") -> "dict | None":
    """
    Compute all Phase 1 technical metrics for one ticker.
    Returns None on OHLC fetch failure or < 252 bars (1 year of trading days).
    """
    try:
        df = load_ohlc_cached(ticker, f"{ticker}.NS", period="2y")
    except Exception as exc:
        print(f"  [{ticker}] OHLC fetch failed: {exc}")
        return None

    if df is None or len(df) < 252:
        n = len(df) if df is not None else 0
        print(f"  [{ticker}] only {n} bars — need 252, skipping")
        return None

    close = df["Close"].values
    price = float(close[-1])

    # ── Minervini Trend Template — 8 binary criteria ──────────────────────────
    # Source: Mark Minervini "Trade Like a Stock Market Wizard" — all must be met
    # for a "perfect template"; we count passes for a gradient signal.
    ma50  = pd.Series(close).rolling(50).mean()
    ma150 = pd.Series(close).rolling(150).mean()
    ma200 = pd.Series(close).rolling(200).mean()

    ma50_now  = float(ma50.iloc[-1])
    ma150_now = float(ma150.iloc[-1])
    ma200_now = float(ma200.iloc[-1])
    # 200-day MA slope: compare to 20 trading days ago
    ma200_20d = float(ma200.iloc[-21]) if len(ma200) >= 21 and not pd.isna(ma200.iloc[-21]) else float("nan")

    week52_low  = float(df["Low"].tail(252).min())
    week52_high = float(df["High"].tail(252).max())

    criteria = [
        price > ma50_now,                                      # C1: price > 50-day MA
        price > ma150_now,                                     # C2: price > 150-day MA
        price > ma200_now,                                     # C3: price > 200-day MA
        ma50_now  > ma150_now,                                 # C4: 50-MA > 150-MA
        ma150_now > ma200_now,                                 # C5: 150-MA > 200-MA
        not np.isnan(ma200_20d) and ma200_now > ma200_20d,    # C6: 200-MA trending up
        price >= week52_low  * 1.30,                           # C7: ≥ 30% above 52w low
        price >= week52_high * 0.75,                           # C8: within 25% of 52w high
    ]
    minervini_passes = sum(criteria)

    # ── IBD Relative Strength raw score ───────────────────────────────────────
    # Weighted 4-period ROC formula from Investor's Business Daily.
    # 63/126/189/252 = ~3/6/9/12 months in trading days.
    # Ranked to percentile (0-100) after all tickers are scanned.
    ibd_rs_raw = (0.40 * _roc(close, 63)
                + 0.20 * _roc(close, 126)
                + 0.20 * _roc(close, 189)
                + 0.20 * _roc(close, 252))

    # ── Mansfield Relative Strength ────────────────────────────────────────────
    # RP = (stock_close / nifty_close) × 100
    # Mansfield RS = (RP / SMA_52w(RP)) − 1
    # Positive = stock outperforming Nifty on a trend basis.
    mansfield_rs       = None
    mansfield_positive = False
    if nifty_df is not None:
        try:
            stock_close = df[["Close"]].rename(columns={"Close": "stock"})
            # df index is tz-naive DatetimeIndex from load_ohlc_cached
            stock_close.index = pd.to_datetime(stock_close.index)
            if stock_close.index.tz is not None:
                stock_close.index = stock_close.index.tz_localize(None)

            nifty_close = nifty_df.set_index("Date")[["Close"]].rename(columns={"Close": "nifty"})
            # nifty_df.Date is parsed as tz-naive by _load_nifty()
            nifty_close.index = pd.to_datetime(nifty_close.index)

            aligned = stock_close.join(nifty_close, how="inner")
            if len(aligned) >= 252:
                rp    = (aligned["stock"] / aligned["nifty"]) * 100.0
                sma52 = rp.rolling(252).mean()
                last_sma = sma52.iloc[-1]
                if not pd.isna(last_sma) and last_sma != 0:
                    mansfield_rs       = float(rp.iloc[-1] / last_sma - 1)
                    mansfield_positive = mansfield_rs > 0
        except Exception:
            pass  # Mansfield skipped silently — not a hard failure

    # ── Volume Contraction (VCP coiling signal) ────────────────────────────────
    # Minervini VCP: price tightening + volume declining = spring loading.
    vol_10d = float(df["Volume"].tail(10).mean())
    vol_50d = float(df["Volume"].tail(50).mean())
    volume_contraction = vol_50d > 0 and vol_10d < vol_50d

    # ── Weinstein Stage (reused from confidence.py) ───────────────────────────
    stage, stage_desc = _weinstein_stage(df)

    # ── RSI (reused from confidence.py) ───────────────────────────────────────
    rsi = round(float(_compute_rsi(close)), 1)

    return {
        "minervini_passes":   minervini_passes,
        "minervini_criteria": criteria,        # kept for --ticker debug output
        "ibd_rs_raw":         ibd_rs_raw,
        "ibd_rs_rank":        None,            # filled by percentile ranking after all tickers
        "mansfield_rs":       mansfield_rs,
        "mansfield_positive": mansfield_positive,
        "weinstein_stage":    stage,
        "weinstein_desc":     stage_desc,
        "volume_contraction": volume_contraction,
        "rsi":                rsi,
        "current_price":      price,
        "week52_high":        week52_high,
        "week52_low":         week52_low,
    }


# ── Phase 2: Smart money ──────────────────────────────────────────────────────

def _phase2_smart_money(ticker: str, conn: sqlite3.Connection,
                         watchlist_map: dict) -> dict:
    """
    Compute insider_signal (0-4) from market.db and cross-ref discovery_watchlist.

    Uses net position per entity to separate genuine accumulation from
    arbitrage round-trips (entities that buy and sell matched quantities).

    Signal levels (based on GENUINE accumulators only):
      0 = no buys, or all buying dominated by matched arbitrage (arb_ratio > 0.8)
      1 = any net-positive buy in last 60 days (bulk/block deal)
      2 = promoter/director tier OR very_high confidence party
      +1 = 3+ genuinely net-positive entities (coordinated), capped at 4
    """
    import market_db as _mdb
    net = _mdb.query_net_accumulation(conn, ticker, days=60)

    genuine       = net["genuine_accumulators"]   # [{party_name, net_cr}]
    arb_ratio     = net["arbitrage_ratio"]
    net_value_cr  = net["net_value_cr"]

    insider_signal = 0
    insider_reason = ""
    insider_detail = ""

    # Arbitrage-dominated: gross buying is round-trip noise, not conviction
    if arb_ratio > 0.8 and not genuine:
        discovery_conviction = watchlist_map.get(ticker, 0)
        return {
            "insider_signal":       0,
            "insider_reason":       "arb_dominated",
            "insider_detail":       f"arb {arb_ratio:.0%} · net ₹{net_value_cr:.0f} Cr",
            "discovery_conviction": discovery_conviction,
        }

    if genuine:
        # Pull tier + confidence for the genuine accumulators
        party_names = [g["party_name"] for g in genuine]
        placeholders = ",".join("?" * len(party_names))
        profiles = {r["party_name"]: dict(r) for r in conn.execute(f"""
            SELECT it.party_name, it.tier, pp.confidence
            FROM   insider_trades it
            LEFT JOIN party_profiles pp ON it.party_name = pp.party_name
            WHERE  it.ticker = ? AND it.party_name IN ({placeholders})
            GROUP BY it.party_name
        """, [ticker] + party_names).fetchall()}

        is_promoter   = any(profiles.get(g["party_name"], {}).get("tier") in _PROMOTER_TIERS for g in genuine)
        has_very_high = any(profiles.get(g["party_name"], {}).get("confidence") == "very_high" for g in genuine)

        if is_promoter or has_very_high:
            insider_signal = 2
            insider_reason = "promoter_accumulation"
        else:
            insider_signal = 1
            insider_reason = "bulk_block_deal"

        if len(genuine) >= 3:
            insider_signal = min(4, insider_signal + 1)
            insider_reason = "coordinated_institutional"

        # Show top genuine accumulators in detail (not gross totals)
        top_names = " · ".join(f"{g['party_name'].split()[0]} ₹{g['net_cr']:.0f}Cr" for g in genuine[:3])
        entity_word = "entity" if len(genuine) == 1 else "entities"
        latest_date = conn.execute(
            "SELECT MAX(date) FROM insider_trades WHERE ticker=? AND date >= date('now','-60 days')", (ticker,)
        ).fetchone()[0] or ""
        insider_detail = (f"net ₹{net_value_cr:.0f} Cr · {len(genuine)} {entity_word} · "
                          f"latest {latest_date} · {top_names}")

    discovery_conviction = watchlist_map.get(ticker, 0)

    return {
        "insider_signal":       insider_signal,
        "insider_reason":       insider_reason,
        "insider_detail":       insider_detail,
        "discovery_conviction": discovery_conviction,
    }


# ── Phase 3: Fundamental quality ─────────────────────────────────────────────

def _phase3_fundamentals(ticker: str, conn: sqlite3.Connection) -> dict:
    """
    Attempt Screener.in fetch (respects 60-day TTL), then score from market.db.

    Partial Piotroski F-Score (4 of 9 criteria — limited by available data):
      F1: latest quarterly net profit > 0
      F3: ROCE improving year-over-year
      F5: debt/equity decreasing year-over-year
      F8: revenue growing year-over-year

    ROCE quality (0-3): ≥20% = 2pts, 15-20% = 1pt, improving = +1pt
    Growth flags (0-3): revenue CAGR ≥15%, operating leverage, D/E < 0.5

    Returns null scores silently when fundamentals are unavailable.
    """
    # Attempt Screener.in fetch — no-op on cache hit, silent skip on failure
    try:
        from fetch_fundamentals import fetch_fundamentals
        fetch_fundamentals(ticker, force=False)
    except Exception as exc:
        print(f"  [{ticker}] Screener.in skipped: {exc}")

    annual = conn.execute("""
        SELECT period, roce_pct, debt_equity, revenue_cr, net_profit_cr
        FROM   fundamentals
        WHERE  ticker      = ?
          AND  period_type = 'annual'
        ORDER  BY period DESC
        LIMIT  4
    """, (ticker,)).fetchall()
    annual = [dict(r) for r in annual]

    q_profit = conn.execute("""
        SELECT net_profit_cr FROM fundamentals
        WHERE  ticker      = ?
          AND  period_type = 'quarterly'
        ORDER  BY period DESC
        LIMIT  1
    """, (ticker,)).fetchone()

    if not annual:
        return {"fundamental_score": None, "piotroski_partial": None, "roce_latest": None}

    latest = annual[0]
    prior  = annual[1] if len(annual) > 1 else {}
    oldest = annual[-1] if len(annual) >= 3 else {}

    roce_latest = latest.get("roce_pct")

    # ── Partial Piotroski ─────────────────────────────────────────────────────
    f1 = int(bool(q_profit and q_profit[0] is not None and q_profit[0] > 0))

    f3 = int(
        roce_latest is not None
        and prior.get("roce_pct") is not None
        and roce_latest > prior["roce_pct"]
    )
    f5 = int(
        latest.get("debt_equity") is not None
        and prior.get("debt_equity") is not None
        and latest["debt_equity"] < prior["debt_equity"]
    )
    f8 = int(
        latest.get("revenue_cr") is not None
        and prior.get("revenue_cr") is not None
        and prior["revenue_cr"] > 0
        and latest["revenue_cr"] > prior["revenue_cr"]
    )
    piotroski_partial = f1 + f3 + f5 + f8  # 0-4

    # ── ROCE quality ──────────────────────────────────────────────────────────
    roce_score = 0
    if roce_latest is not None:
        if roce_latest >= 20:
            roce_score = 2
        elif roce_latest >= 15:
            roce_score = 1
        if prior.get("roce_pct") is not None and roce_latest > prior["roce_pct"]:
            roce_score += 1  # improving ROCE is +1 regardless of level

    # ── Growth flags ──────────────────────────────────────────────────────────
    growth_score = 0

    # Revenue CAGR over available annual periods
    if (len(annual) >= 3
            and latest.get("revenue_cr") and oldest.get("revenue_cr")
            and oldest["revenue_cr"] > 0):
        n_years = len(annual) - 1
        try:
            cagr = (latest["revenue_cr"] / oldest["revenue_cr"]) ** (1.0 / n_years) - 1
            if cagr >= 0.15:
                growth_score += 1
        except (ZeroDivisionError, ValueError):
            pass

    # Operating leverage: profit growth exceeds revenue growth
    if (latest.get("net_profit_cr") and prior.get("net_profit_cr")
            and latest.get("revenue_cr") and prior.get("revenue_cr")
            and prior["net_profit_cr"] > 0 and prior["revenue_cr"] > 0):
        profit_g  = latest["net_profit_cr"] / prior["net_profit_cr"] - 1
        revenue_g = latest["revenue_cr"]    / prior["revenue_cr"]    - 1
        if profit_g > revenue_g:
            growth_score += 1

    # Conservative balance sheet
    if latest.get("debt_equity") is not None and latest["debt_equity"] < 0.5:
        growth_score += 1

    fundamental_score = piotroski_partial + roce_score + growth_score  # 0-10

    return {
        "fundamental_score": fundamental_score,
        "piotroski_partial":  piotroski_partial,
        "roce_latest":        roce_latest,
    }


# ── Scoring and conviction tier ───────────────────────────────────────────────

def _composite_score(c: dict) -> float:
    """
    Weighted composite across all screens.
    Max theoretical: 8 + 5 + 1 + 2 + 1 + 8 + 5 = 30
    """
    fs = c.get("fundamental_score") or 0
    return (c["minervini_passes"]             * 1.0   # 0-8
          + (c["ibd_rs_rank"] or 0)           / 20.0  # 0-5  (rank 100 → 5 pts)
          + (1.0 if c["mansfield_positive"] else 0.0) # 0-1
          + (2.0 if c["weinstein_stage"] == 2 else 0.0) # 0-2
          + (1.0 if c["volume_contraction"] else 0.0) # 0-1
          + c["insider_signal"]               * 2.0   # 0-8
          + fs                                * 0.5)  # 0-5  (score 10 → 5 pts)


def _assign_tier(c: dict) -> str:
    miner   = c["minervini_passes"]
    rs      = c["ibd_rs_rank"] or 0
    insider = c["insider_signal"]
    fs      = c.get("fundamental_score")

    if miner >= 7 and rs >= 75 and insider >= 1:
        return "HIGH CONVICTION"
    if miner >= 5 and rs >= 60:
        return "MOMENTUM SETUP"
    if fs is not None and fs >= 7 and (miner >= 4 or insider >= 2):
        return "FUNDAMENTAL GEM"
    return "EARLY WATCH"


def _build_narrative(c: dict) -> str:
    parts = []
    if c["insider_signal"] >= 2:
        parts.append(c["insider_reason"].replace("_", " ").title())
    parts.append(f"{c['minervini_passes']}/8 Minervini template")
    rs = c["ibd_rs_rank"] or 0
    if rs >= 50:
        parts.append(f"IBD RS {rs}th percentile")
    if c["weinstein_stage"] == 2:
        parts.append("Stage 2 advancing")
    if c["volume_contraction"]:
        parts.append("VCP coiling")
    if c.get("fundamental_score") is not None:
        parts.append(f"F-score {c['fundamental_score']}/10")
    return " + ".join(parts) if parts else "Technical pattern only"


# ── Console table ─────────────────────────────────────────────────────────────

_TIER_COLOR = {
    "HIGH CONVICTION": "\033[33m",  # amber
    "MOMENTUM SETUP":  "\033[36m",  # teal
    "FUNDAMENTAL GEM": "\033[35m",  # magenta
    "EARLY WATCH":     "\033[90m",  # dim grey
}
_RESET = "\033[0m"
_BOLD  = "\033[1m"


def _print_table(candidates: list[dict], universe_size: int, phase1_count: int):
    print(f"\n{_BOLD}MULTIBAGGER SCREEN  {date.today()}{_RESET}")
    print(f"Universe: {universe_size}  |  Phase 1 survivors: {phase1_count}  "
          f"|  Top candidates: {len(candidates)}")
    print()

    header = (f"{'RK':<4} {'TICKER':<12} {'TIER':<20} {'SCORE':>6}  "
              f"{'MIN':>4}  {'RS':>4}  {'INS':>5}  {'SECTOR'}")
    print(_BOLD + header + _RESET)
    print("─" * 76)

    for i, c in enumerate(candidates, 1):
        color   = _TIER_COLOR.get(c["tier"], "")
        ins_bar = "●" * c["insider_signal"] + "○" * (4 - c["insider_signal"])
        rs_str  = str(c["ibd_rs_rank"]) if c["ibd_rs_rank"] is not None else "—"
        bucket  = c["sector_bucket"][:24]
        align   = " ✓" if c["sector_aligned"] else ""
        print(f"{i:<4} {c['ticker']:<12} "
              f"{color}{c['tier']:<20}{_RESET}  "
              f"{c['composite_score']:>5.1f}  "
              f"{c['minervini_passes']}/8  "
              f"{rs_str:>4}  "
              f"{ins_bar}  "
              f"{bucket}{align}")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def run(quick: bool = False, single_ticker: "str | None" = None):
    # ── Load universe ─────────────────────────────────────────────────────────
    print(f"Loading universe from {UNIVERSE_PATH}…")
    universe = _load_universe()
    if single_ticker:
        upper = single_ticker.upper()
        universe_filtered = [u for u in universe if u["ticker"] == upper]
        if not universe_filtered:
            universe_filtered = [{"ticker": upper, "sector_bucket": "Unknown",
                                   "tier_label": "tier2"}]
        universe = universe_filtered
        print(f"Single-ticker debug: {upper}")
    print(f"Universe: {len(universe)} stocks")

    # ── Load Nifty for Mansfield RS ───────────────────────────────────────────
    print("Loading Nifty 50 cache for Mansfield RS…")
    nifty_df = _load_nifty()
    if nifty_df is None:
        print("  WARNING: Nifty cache not found — Mansfield RS will be None for all tickers")

    # ────────────────────────────────────────────────────────────────────────
    # Phase 1: Technical Momentum Screen
    # ────────────────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("Phase 1: Technical Momentum Screen")
    print(f"{'─'*60}")

    phase1: dict[str, dict] = {}
    for stock in universe:
        ticker = stock["ticker"]
        sys.stdout.write(f"  {ticker:<14}")
        sys.stdout.flush()
        metrics = _phase1_technical(ticker, nifty_df)
        if metrics is None:
            print("  skipped")
            continue
        metrics["sector_bucket"] = stock["sector_bucket"]
        metrics["tier_label"]    = stock["tier_label"]
        phase1[ticker] = metrics
        print(f"  Minervini {metrics['minervini_passes']}/8  RSI {metrics['rsi']:.0f}  "
              f"IBD raw {metrics['ibd_rs_raw']:+.1f}")

    # Percentile-rank IBD RS raw scores across the full scanned universe
    if phase1:
        sorted_by_rs = sorted(phase1.keys(), key=lambda t: phase1[t]["ibd_rs_raw"])
        n = len(sorted_by_rs)
        for rank, ticker in enumerate(sorted_by_rs):
            phase1[ticker]["ibd_rs_rank"] = round((rank / max(n - 1, 1)) * 100)

    # Phase 1 gate
    survivors: dict[str, dict] = {}
    for ticker, m in phase1.items():
        if m["minervini_passes"] >= _MIN_MINERVINI or (m["ibd_rs_rank"] or 0) >= _MIN_IBD_RANK:
            survivors[ticker] = m

    print(f"\nPhase 1 gate: {len(survivors)} survivors "
          f"(Minervini≥{_MIN_MINERVINI} OR IBD_RS≥{_MIN_IBD_RANK})")

    if not survivors and not single_ticker:
        print("No survivors — market may be in broad correction. Exiting.")
        return

    # In single-ticker mode, include the ticker even if it failed the gate
    if single_ticker:
        pool = phase1  # all scanned (just the one ticker)
    else:
        pool = survivors

    # ────────────────────────────────────────────────────────────────────────
    # Phase 2: Smart Money Cross-Reference
    # ────────────────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("Phase 2: Smart Money Cross-Reference (60-day window)")
    print(f"{'─'*60}")

    conn = mdb.get_conn()

    watchlist_map: dict[str, int] = {}
    if WATCHLIST_PATH.exists():
        wl            = json.loads(WATCHLIST_PATH.read_text())
        watchlist_map = {c["ticker"]: c.get("conviction", 0)
                         for c in wl.get("candidates", [])}

    combined: dict[str, dict] = {}
    for ticker, tech in pool.items():
        sm = _phase2_smart_money(ticker, conn, watchlist_map)
        combined[ticker] = {**tech, **sm}
        if sm["insider_signal"] > 0:
            print(f"  {ticker:<14} signal={sm['insider_signal']}  {sm['insider_detail']}")
        else:
            print(f"  {ticker:<14} no recent insider activity")

    # Sort by preliminary Phase 1+2 score for Phase 3 prioritisation
    def _prelim(item):
        m = item[1]
        return (m["minervini_passes"] * 1.0
              + (m["ibd_rs_rank"] or 0) / 20.0
              + m["insider_signal"] * 2.0)

    sorted_combined = sorted(combined.items(), key=_prelim, reverse=True)

    # ────────────────────────────────────────────────────────────────────────
    # Phase 3: Fundamental Quality
    # ────────────────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    if not quick:
        print(f"Phase 3: Fundamental Quality (top {_PHASE3_TOP_N} candidates)")
        print(f"{'─'*60}")
        top_n = [ticker for ticker, _ in sorted_combined[:_PHASE3_TOP_N]]
        for ticker in top_n:
            print(f"  {ticker}…")
            fund = _phase3_fundamentals(ticker, conn)
            combined[ticker].update(fund)
        # Fill remaining with nulls
        for ticker in combined:
            if "fundamental_score" not in combined[ticker]:
                combined[ticker].update({"fundamental_score": None,
                                          "piotroski_partial": None,
                                          "roce_latest": None})
    else:
        print("Phase 3: Skipped (--quick mode)")
        print(f"{'─'*60}")
        for ticker in combined:
            combined[ticker].update({"fundamental_score": None,
                                      "piotroski_partial": None,
                                      "roce_latest": None})

    conn.close()

    # ────────────────────────────────────────────────────────────────────────
    # Final scoring, tiering, ranking
    # ────────────────────────────────────────────────────────────────────────
    results: list[dict] = []
    for ticker, m in combined.items():
        # Drop the raw criteria list from JSON output — kept internally for debug
        criteria = m.pop("minervini_criteria", [])
        m["sector_aligned"]   = _sector_aligned(m["sector_bucket"])
        m["tier"]             = _assign_tier(m)
        m["composite_score"]  = round(_composite_score(m), 1)
        m["narrative"]        = _build_narrative(m)
        entry = {"ticker": ticker, **m}
        # Restore for possible debug print below
        entry["minervini_criteria"] = criteria
        results.append(entry)

    results.sort(key=lambda c: c["composite_score"], reverse=True)

    # ── Output ────────────────────────────────────────────────────────────────
    if single_ticker:
        c = results[0] if results else {}
        print(f"\n── {single_ticker.upper()} Debug ──")
        labels = ["C1:price>50MA", "C2:price>150MA", "C3:price>200MA",
                  "C4:50MA>150MA", "C5:150MA>200MA", "C6:200MA_rising",
                  "C7:+30%_vs_52wLow", "C8:within25%_52wHigh"]
        for label, passed in zip(labels, c.get("minervini_criteria", [])):
            print(f"  {'✓' if passed else '✗'} {label}")
        print()
        skip = {"minervini_criteria"}
        for k, v in c.items():
            if k not in skip:
                print(f"  {k}: {v}")
    else:
        _print_table(results, len(universe), len(survivors))

    # Strip minervini_criteria from JSON output — too verbose, debug-only
    for r in results:
        r.pop("minervini_criteria", None)

    output = {
        "scan_date":     str(date.today()),
        "universe_size": len(universe),
        "passed_phase1": len(survivors),
        "candidates":    results,
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, default=str))
    print(f"→ {OUTPUT_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Multibagger screener: Minervini + IBD RS + smart money + fundamentals"
    )
    parser.add_argument("--quick",  action="store_true",
                        help="Skip Phase 3 (no Screener.in fetch)")
    parser.add_argument("--ticker", metavar="TICKER",
                        help="Debug a single ticker (e.g. IDEAFORGE)")
    args = parser.parse_args()
    run(quick=args.quick, single_ticker=args.ticker)
