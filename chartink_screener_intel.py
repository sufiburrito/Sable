#!/usr/bin/env python3
"""
chartink_screener_intel.py — Chartink community screener intelligence pipeline.

Phase 1: Browse category pages → classify screeners (SHORT_TERM | MULTIBAGGER | SKIP)
Phase 2: Visit each selected screener → read filter logic + like count + download CSV
Phase 3: Convergence analysis + technical enrichment (Minervini, IBD RS, insider signal)
Phase 4: Write SCREENER_FINDS.md with two ranked sections

Usage:
    python3 chartink_screener_intel.py           # Full run
    python3 chartink_screener_intel.py --browse  # Phase 1 only — catalogue screeners
    python3 chartink_screener_intel.py --fast    # Phase 1+2 only — no OHLC fetch

Run from project root. Screener catalogue cached in data/chartink_screener_state.json
for 7 days so re-runs skip Phase 1 unless --refresh-catalogue is passed.
"""

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from alert_bot.ohlc_cache import load_ohlc_cached
from alert_bot.confidence import _weinstein_stage, _compute_rsi, _load_nifty
import market_db as mdb

# ── Paths ─────────────────────────────────────────────────────────────────────

CHARTINK_BASE   = "https://chartink.com"
STATE_PATH      = Path("data/chartink_screener_state.json")
OUTPUT_PATH     = Path("SCREENER_FINDS.md")
CSV_DIR         = Path("analysis/chartink_csvs")
MULTIBAGGER_PATH = Path("data/multibagger_scan.json")
WATCHLIST_PATH  = Path("data/discovery_watchlist.json")

# ── Categories to browse (in priority order) ──────────────────────────────────

CATEGORIES = [
    ("/screeners/top-loved-screeners",      "top_loved",  5),   # most popular, max 5 pages
    ("/screeners/fundamental-screeners",    "fundamental", 3),   # MULTIBAGGER focus
    ("/screeners/bullish-screeners",        "bullish",     2),   # SHORT_TERM focus
    ("/screeners/range-breakouts-screeners","breakouts",   2),   # SHORT_TERM focus
    ("/screeners/crossovers-screeners",     "crossovers",  1),   # mixed
]

# Per-run limits to avoid Chartink rate limiting
MAX_SCREENERS_PER_TYPE = 15  # max SHORT_TERM + max MULTIBAGGER screeners to CSV-download
DELAY_BETWEEN_PAGES    = 2.5  # seconds between page loads
MIN_LIKES_SHORT_TERM   = 300  # momentum screeners with fewer likes are low quality
MIN_LIKES_MULTIBAGGER  = 30   # fundamental screeners are inherently less popular — lower bar

# ── Keyword classifiers ───────────────────────────────────────────────────────

_SHORT_TERM_KW = {
    "breakout", "momentum", "volume spike", "near high", "btst", "crossover",
    "ema cross", "macd", "rsi breakout", "52 week high", "all time high", "nr7",
    "short term", "swing", "potential breakout", "strong stocks", "bull run",
    "bullish momentum", "moving average", "supertrend", "trending stocks",
    "weekly breakout", "monthly breakout",
}
_MULTIBAGGER_KW = {
    "roce", "debt free", "zero debt", "debt-free", "sales growth", "profit growth",
    "eps growth", "eps", "earning per share", "quality", "compounder", "emerging",
    "growth stock", "high roe", "multibagger", "booming", "turnaround",
    "fundamentally strong", "low pe", "price earning", "pe ratio",
    "high growth", "revenue growth", "profit jump", "sales jump",
    "cash rich", "dividend", "small cap growth", "mid cap growth",
    "undervalued", "book value", "below book", "low debt", "fundamental",
    "holding", "long term", "wealth creation", "compounding", "hidden gem",
}
_SKIP_KW = {
    "intraday", "5 min", "15 min", "bearish", "short sell",
    "pe buy", "ce buy", "options", "f&o", "fno", "scalping",
    "operator", "circuit",
    # Explicitly intraday-named screens
    "open high", "open low", "open=high", "open=low", "morning scanner",
    "cpr", "9:15", "9:30",
}
# Screener titles that are purely intraday even without keyword matches
_SKIP_TITLES = {
    "breaking days high - 5 mins", "nks best buy stocks for intraday",
    "buy 100% accuracy - morning scanner scan at 9:30",
    "perfect sell (short)", "stbt stocks new",
}

def _classify(title: str, desc: str) -> str:
    """Return SHORT_TERM, MULTIBAGGER, or SKIP based on title + description."""
    text = (title + " " + desc).lower()

    # Hard skip on exact title matches
    if title.lower() in _SKIP_TITLES:
        return "SKIP"

    # Hard skips first
    for kw in _SKIP_KW:
        if kw in text:
            return "SKIP"

    short_hits = sum(1 for kw in _SHORT_TERM_KW  if kw in text)
    multi_hits  = sum(1 for kw in _MULTIBAGGER_KW if kw in text)

    if short_hits == 0 and multi_hits == 0:
        return "SKIP"  # no signal either way — skip
    if multi_hits > short_hits:
        return "MULTIBAGGER"
    return "SHORT_TERM"


# ── Playwright helpers ────────────────────────────────────────────────────────

def _make_browser():
    """Launch a Playwright browser with realistic user-agent."""
    from playwright.sync_api import sync_playwright
    try:
        from playwright_stealth import Stealth
        pw_ctx = Stealth().use_sync(sync_playwright())
    except ImportError:
        pw_ctx = sync_playwright()
    p       = pw_ctx.__enter__()
    browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
    ctx     = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
    )
    return pw_ctx, browser, ctx


def _safe_goto(page, url: str, wait: str = "load", timeout: int = 30000) -> bool:
    """Navigate to URL, returning False on timeout instead of raising."""
    try:
        page.goto(url, wait_until=wait, timeout=timeout)
        page.wait_for_timeout(int(DELAY_BETWEEN_PAGES * 1000))
        return True
    except Exception as exc:
        print(f"  [timeout/error] {url}: {exc!s:.80}")
        return False


def _extract_like_count(page) -> int:
    """Parse 'N people love this' from a screener page."""
    try:
        text = page.inner_text("body")
        m = re.search(r"([\d,]+)\s+people\s+love", text, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return 0


def _extract_logic_summary(page) -> str:
    """
    Extract the human-readable filter conditions from a screener page.
    Chartink renders them as visible text between 'passes all of the below filters'
    and 'Run Scan'.
    """
    try:
        text = page.inner_text("body")
        start = text.lower().find("passes")
        end   = text.lower().find("run scan")
        if start > 0 and end > start:
            raw = text[start:end].strip()
            # Collapse whitespace
            raw = re.sub(r"\s+", " ", raw)
            return raw[:400]
    except Exception:
        pass
    return ""


# ── Phase 1: Browse categories and classify screeners ─────────────────────────

def phase1_browse(page, refresh: bool = False) -> list[dict]:
    """
    Browse configured category pages and return a catalogue of classified screeners.
    Cached to STATE_PATH; skips if cache is <7 days old unless refresh=True.
    """
    if not refresh and STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        age   = (datetime.now() - datetime.fromisoformat(state["browsed_at"])).days
        if age < 7:
            print(f"Using cached screener catalogue ({age}d old, {len(state['screeners'])} screeners)")
            return state["screeners"]

    print("Phase 1: Browsing Chartink screener categories…")
    catalogue = []
    seen_slugs = set()

    for cat_path, cat_name, max_pages in CATEGORIES:
        print(f"  Category: {cat_name}")
        for page_num in range(1, max_pages + 1):
            sep    = "?" if "?" not in cat_path else "&"
            url    = f"{CHARTINK_BASE}{cat_path}{sep}page={page_num}" if page_num > 1 else f"{CHARTINK_BASE}{cat_path}"
            ok     = _safe_goto(page, url)
            if not ok:
                break

            links = page.query_selector_all("a")
            found = 0
            for lnk in links:
                href = lnk.get_attribute("href") or ""
                # Chartink uses /screener/{slug} (singular) for community screeners
                if not href.startswith("/screener/"):
                    continue
                slug  = href[len("/screener/"):].rstrip("/")
                title = lnk.inner_text().strip()
                if not slug or not title or slug in seen_slugs:
                    continue

                # Try to get description from parent element text
                desc = ""
                try:
                    parent_text = lnk.evaluate(
                        "el => el.parentElement ? el.parentElement.innerText : ''"
                    ).strip()
                    desc = parent_text.replace(title, "").strip()[:250]
                except Exception:
                    pass

                tag = _classify(title, desc)
                catalogue.append({
                    "slug":     slug,
                    "title":    title,
                    "desc":     desc,
                    "category": cat_name,
                    "type":     tag,
                    "likes":    0,           # filled in Phase 2 when we visit the page
                    "url":      f"{CHARTINK_BASE}/screener/{slug}",
                })
                seen_slugs.add(slug)
                found += 1

            print(f"    Page {page_num}: {found} screeners found")
            if found == 0:
                break

    # Save catalogue
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps({
        "browsed_at": datetime.now().isoformat(),
        "screeners":  catalogue,
    }, indent=2))

    counts = Counter(s["type"] for s in catalogue)
    print(f"  Catalogue: {len(catalogue)} screeners — "
          f"SHORT_TERM={counts['SHORT_TERM']} MULTIBAGGER={counts['MULTIBAGGER']} SKIP={counts['SKIP']}")
    return catalogue


# ── Phase 2: Visit selected screeners, get like counts + logic + CSV ──────────

def phase2_download(page, catalogue: list[dict]) -> list[dict]:
    """
    For each selected screener (top SHORT_TERM + top MULTIBAGGER):
    visit the page, extract like count + logic summary + CSV of stocks.
    Returns enriched screener records with 'tickers' list.
    """
    CSV_DIR.mkdir(parents=True, exist_ok=True)

    # Select top candidates of each type (prefer high-liked screeners from top_loved)
    def _priority(s):
        # Top-loved screeners appear earlier in the catalogue (higher priority)
        cat_order = {"top_loved": 0, "fundamental": 1, "bullish": 2,
                     "breakouts": 3, "crossovers": 4}
        return (cat_order.get(s["category"], 9), catalogue.index(s))

    short_term  = [s for s in catalogue if s["type"] == "SHORT_TERM"]
    multibagger = [s for s in catalogue if s["type"] == "MULTIBAGGER"]

    short_term.sort(key=_priority)
    multibagger.sort(key=_priority)

    selected = (short_term[:MAX_SCREENERS_PER_TYPE]
              + multibagger[:MAX_SCREENERS_PER_TYPE])

    print(f"\nPhase 2: Downloading CSVs for {len(selected)} selected screeners…")
    enriched = []

    for s in selected:
        print(f"  [{s['type'][:2]}] {s['title'][:55]}…", end=" ", flush=True)
        ok = _safe_goto(page, s["url"])
        if not ok:
            print("skipped (load failed)")
            continue

        # Extract like count + logic
        s["likes"]         = _extract_like_count(page)
        s["logic_summary"] = _extract_logic_summary(page)

        # Per-type likes threshold: fundamental screens are niche → lower bar
        min_likes = MIN_LIKES_MULTIBAGGER if s["type"] == "MULTIBAGGER" else MIN_LIKES_SHORT_TERM
        if s["likes"] == 0 or s["likes"] < min_likes:
            # likes==0 means parse failed (Cloudflare block / DOM change) — treat as unknown quality, skip
            print(f"skipped ({s['likes']} likes < {min_likes})")
            continue

        # Download CSV
        tickers = _download_csv(page, s["slug"])
        s["tickers"] = tickers
        print(f"{s['likes']} likes · {len(tickers)} stocks")
        enriched.append(s)

    # Update catalogue cache with like counts
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        like_map = {s["slug"]: s["likes"] for s in enriched}
        for item in state["screeners"]:
            if item["slug"] in like_map:
                item["likes"] = like_map[item["slug"]]
        STATE_PATH.write_text(json.dumps(state, indent=2))

    return enriched


def _download_csv(page, slug: str) -> list[str]:
    """
    Click the CSV button on a Chartink screener page and return list of NSE symbols.
    Falls back to parsing the visible results table if download fails.
    """
    csv_path = CSV_DIR / f"{slug}.csv"

    # Try download via button click
    try:
        from playwright.sync_api import Error as PWError
        with page.expect_download(timeout=15000) as dl_info:
            page.click('button[aria-label="CSV"]', timeout=8000)
        dl        = dl_info.value
        tmp_path  = dl.path()
        if tmp_path:
            import shutil
            shutil.copy(tmp_path, csv_path)
            return _parse_csv_symbols(csv_path)
    except Exception:
        pass  # fall through to text extraction

    # Fallback: parse visible Symbol column from page text
    return _extract_symbols_from_page(page)


def _parse_csv_symbols(csv_path: Path) -> list[str]:
    """Read Symbol column from a Chartink CSV file."""
    try:
        df = pd.read_csv(csv_path)
        # Chartink CSVs have columns: Sr., Stock Name, NSE Code, ...
        for col in ["NSE Code", "Symbol", "SYMBOL", "Nse Symbol"]:
            if col in df.columns:
                return [str(v).strip().upper() for v in df[col].dropna() if str(v).strip()]
        # If no recognised column, use second column (index 1)
        if len(df.columns) >= 2:
            return [str(v).strip().upper() for v in df.iloc[:, 1].dropna() if str(v).strip()]
    except Exception as exc:
        print(f"  CSV parse error ({csv_path.name}): {exc}")
    return []


def _extract_symbols_from_page(page) -> list[str]:
    """
    Fallback: extract NSE symbols from the results table on the screener page.
    Chartink shows: Sr. | Stock Name | Symbol | Close | %change | Volume
    """
    try:
        text = page.inner_text("body")
        # After "Symbol" header, each row has: number | stock name | SYMBOL | price | % | vol
        # Find the results block
        lines  = [l.strip() for l in text.split("\n") if l.strip()]
        syms   = []
        in_results = False
        for line in lines:
            if line == "Symbol" or line.startswith("Sr."):
                in_results = True
                continue
            if in_results:
                # Symbols are all-caps, 1-20 chars, no spaces, no special chars beyond &
                if re.match(r'^[A-Z][A-Z0-9&\-]{0,19}$', line):
                    syms.append(line)
            if line in ("BACKTEST HISTORY", "Terms of usage"):
                break
        return syms
    except Exception:
        return []


# ── Phase 3: Convergence + technical enrichment ───────────────────────────────

def phase3_convergence(enriched_screeners: list[dict],
                        fast: bool = False) -> tuple[list[dict], list[dict]]:
    """
    Build convergence maps, enrich with technical + smart money data.
    Returns (short_term_results, multibagger_results).
    """
    print("\nPhase 3: Convergence analysis…")

    short_map: dict[str, list[str]] = {}  # ticker → [screen titles]
    multi_map: dict[str, list[str]] = {}

    for s in enriched_screeners:
        target = short_map if s["type"] == "SHORT_TERM" else multi_map
        for ticker in s.get("tickers", []):
            target.setdefault(ticker, []).append(s["title"])

    # Load cross-reference sets
    mb_scan_tickers = set()
    if MULTIBAGGER_PATH.exists():
        mb_data = json.loads(MULTIBAGGER_PATH.read_text())
        mb_scan_tickers = {c["ticker"] for c in mb_data.get("candidates", [])}

    watchlist_map: dict[str, int] = {}
    if WATCHLIST_PATH.exists():
        wl = json.loads(WATCHLIST_PATH.read_text())
        watchlist_map = {c["ticker"]: c.get("conviction", 0)
                         for c in wl.get("candidates", [])}

    # Technical enrichment
    nifty_df = _load_nifty()
    conn     = mdb.get_conn()

    def _enrich_group(freq_map: dict, min_screens: int = 2) -> list[dict]:
        # Sort by convergence count first
        sorted_tickers = sorted(freq_map.items(), key=lambda x: -len(x[1]))
        results = []
        for ticker, screens in sorted_tickers:
            if len(screens) < min_screens:
                continue  # only show convergent stocks

            rec = {
                "ticker":        ticker,
                "screen_count":  len(screens),
                "screens":       screens,
                "in_mb_scan":    ticker in mb_scan_tickers,
                "disc_conviction": watchlist_map.get(ticker, 0),
            }

            if not fast:
                tech = _technical_snapshot(ticker, nifty_df)
                sm   = _smart_money_snapshot(ticker, conn)
                rec.update(tech)
                rec.update(sm)
            else:
                rec.update({"minervini": None, "ibd_rs_raw": None, "stage": None,
                             "rsi": None, "price": None, "insider_signal": 0,
                             "insider_detail": ""})

            # Conviction score
            rec["conviction"] = (
                len(screens) * 3
                + (rec.get("minervini") or 0) * 0.5
                + rec.get("insider_signal", 0) * 2
                + (2 if rec["in_mb_scan"] else 0)
            )
            results.append(rec)

        results.sort(key=lambda r: -r["conviction"])
        return results

    print("  Enriching SHORT_TERM convergent stocks…")
    short_results = _enrich_group(short_map, min_screens=2)
    print("  Enriching MULTIBAGGER convergent stocks…")
    multi_results = _enrich_group(multi_map, min_screens=2)

    conn.close()
    print(f"  Short-term: {len(short_results)} convergent · Multibagger: {len(multi_results)} convergent")
    return short_results, multi_results


def _technical_snapshot(ticker: str, nifty_df) -> dict:
    """Fast technical snapshot — Minervini pass count, RSI, Weinstein stage."""
    try:
        df = load_ohlc_cached(ticker, f"{ticker}.NS", period="2y")
    except Exception:
        return {"minervini": None, "ibd_rs_raw": None, "stage": None,
                "rsi": None, "price": None}

    if df is None or len(df) < 100:
        return {"minervini": None, "ibd_rs_raw": None, "stage": None,
                "rsi": None, "price": None}

    close = df["Close"].values
    price = float(close[-1])

    # Minervini (only compute if enough data)
    minervini = None
    if len(close) >= 252:
        ma50  = pd.Series(close).rolling(50).mean()
        ma150 = pd.Series(close).rolling(150).mean()
        ma200 = pd.Series(close).rolling(200).mean()
        week52_low  = float(df["Low"].tail(252).min())
        week52_high = float(df["High"].tail(252).max())
        ma200_20d   = float(ma200.iloc[-21]) if len(ma200) >= 21 and not pd.isna(ma200.iloc[-21]) else float("nan")
        criteria = [
            price > float(ma50.iloc[-1]),
            price > float(ma150.iloc[-1]),
            price > float(ma200.iloc[-1]),
            float(ma50.iloc[-1])  > float(ma150.iloc[-1]),
            float(ma150.iloc[-1]) > float(ma200.iloc[-1]),
            not np.isnan(ma200_20d) and float(ma200.iloc[-1]) > ma200_20d,
            price >= week52_low  * 1.30,
            price >= week52_high * 0.75,
        ]
        minervini = sum(criteria)

    ibd_rs_raw = None
    if len(close) >= 252:
        def _roc(n):
            return float((close[-1] / close[-(n+1)] - 1) * 100) if len(close) >= n+1 else 0.0
        ibd_rs_raw = round(0.40*_roc(63) + 0.20*_roc(126) + 0.20*_roc(189) + 0.20*_roc(252), 1)

    stage, stage_desc = _weinstein_stage(df) if len(df) >= 150 else (0, "insufficient data")
    rsi = round(float(_compute_rsi(close)), 1)

    return {
        "minervini":  minervini,
        "ibd_rs_raw": ibd_rs_raw,
        "stage":      stage,
        "stage_desc": stage_desc,
        "rsi":        rsi,
        "price":      price,
    }


def _smart_money_snapshot(ticker: str, conn: sqlite3.Connection) -> dict:
    """60-day insider signal from market.db."""
    _PROMOTER_TIERS = {"promoter", "director"}
    rows = conn.execute("""
        SELECT it.tier, it.party_name, it.value_cr, pp.confidence
        FROM   insider_trades  it
        LEFT JOIN party_profiles pp ON it.party_name = pp.party_name
        WHERE  it.ticker = ? AND it.trade_type = 'buy'
          AND  it.date  >= date('now', '-60 days')
    """, (ticker,)).fetchall()
    rows = [dict(r) for r in rows]

    if not rows:
        return {"insider_signal": 0, "insider_detail": ""}

    is_promoter   = any(r["tier"] in _PROMOTER_TIERS      for r in rows)
    has_very_high = any(r.get("confidence") == "very_high" for r in rows)
    entities      = len({r["party_name"] for r in rows})
    total_cr      = sum(r.get("value_cr") or 0 for r in rows)

    signal = 2 if (is_promoter or has_very_high) else 1
    if entities >= 3:
        signal = min(4, signal + 1)

    return {
        "insider_signal": signal,
        "insider_detail": f"₹{total_cr:.0f} Cr · {entities} {'entity' if entities==1 else 'entities'}",
    }


# ── Phase 4: Write SCREENER_FINDS.md ─────────────────────────────────────────

def phase4_write_md(short_results: list[dict], multi_results: list[dict],
                     enriched_screeners: list[dict]):
    """Write SCREENER_FINDS.md — clean, actionable, two-section format."""

    def _ins_bar(n):
        n = n or 0
        return "●" * n + "○" * (4 - n)

    def _tech_cells(r):
        miner = f"{r['minervini']}/8" if r.get("minervini") is not None else "—"
        rsi   = str(r["rsi"])         if r.get("rsi") is not None else "—"
        stage = r.get("stage_desc", "—") or "—"
        price = f"₹{r['price']:,.2f}" if r.get("price") else "—"
        return miner, rsi, stage, price

    # Cross-reference: stocks in BOTH lists
    short_tickers = {r["ticker"] for r in short_results}
    multi_tickers = {r["ticker"] for r in multi_results}
    both          = short_tickers & multi_tickers

    # Screener quality index
    st_screens = [s for s in enriched_screeners if s["type"] == "SHORT_TERM"]
    mb_screens = [s for s in enriched_screeners if s["type"] == "MULTIBAGGER"]

    def _stars(likes):
        if likes >= 3000: return "★★★★★"
        if likes >= 1000: return "★★★★☆"
        if likes >= 500:  return "★★★☆☆"
        return "★★☆☆☆"

    lines = [
        "# SCREENER_FINDS.md",
        f"*Last updated: {date.today()} | "
        f"Screens analysed: {len(enriched_screeners)} | "
        f"Source: Chartink community*",
        "",
        "---",
        "",
    ]

    # ── SHORT-TERM PROFIT PLAYS ───────────────────────────────────────────────
    lines += [
        "## SHORT-TERM PROFIT PLAYS",
        "*Objective: enter, capture 10–30%, exit. Recycle profits into multibagger positions.*",
        "",
    ]

    if short_results:
        lines += [
            "| Ticker | Screens | Conviction | Minervini | RSI | Stage | Insider | Price | Cross-ref |",
            "|--------|---------|------------|-----------|-----|-------|---------|-------|-----------|",
        ]
        for r in short_results[:20]:
            miner, rsi, stage, price = _tech_cells(r)
            stage_short = stage.split("—")[0].strip() if "—" in stage else stage
            cross = "✦ MB scan" if r["in_mb_scan"] else ("✦ watchlist" if r["disc_conviction"] > 0 else "—")
            ins   = _ins_bar(r.get("insider_signal", 0))
            screens_str = ", ".join(r["screens"][:2]) + ("…" if len(r["screens"]) > 2 else "")
            lines.append(
                f"| **{r['ticker']}** | {r['screen_count']} | {r['conviction']:.1f} "
                f"| {miner} | {rsi} | {stage_short} | {ins} | {price} | {cross} |"
            )
        lines += [
            "",
            "<details>",
            "<summary>Screen membership detail</summary>",
            "",
        ]
        for r in short_results[:20]:
            lines.append(f"- **{r['ticker']}**: {', '.join(r['screens'])}")
        lines += ["", "</details>", ""]
    else:
        lines.append("*No convergent short-term stocks found in this run.*\n")

    lines += [
        "### Entry discipline",
        "- Enter only Stage 2 stocks (Weinstein) — Stage 3/4 breakouts are frequent fakeouts",
        "- RSI 50–70 at entry; trim aggressively above RSI 80",
        "- Position size: max 5% per name — this is satellite capital, not core",
        "- Exit rule: defined stop at prior swing low; target 10–30% or resistance",
        "",
        "---",
        "",
    ]

    # ── MULTIBAGGER CANDIDATES ────────────────────────────────────────────────
    lines += [
        "## MULTIBAGGER CANDIDATES",
        "*Objective: onboard to watchlist → full analysis → stocks/ config if thesis holds.*",
        "",
    ]

    if multi_results:
        lines += [
            "| Ticker | Screens | Conviction | Minervini | RSI | Stage | Insider | Price | Cross-ref |",
            "|--------|---------|------------|-----------|-----|-------|---------|-------|-----------|",
        ]
        for r in multi_results[:20]:
            miner, rsi, stage, price = _tech_cells(r)
            stage_short = stage.split("—")[0].strip() if "—" in stage else stage
            cross = "✦ MB scan" if r["in_mb_scan"] else ("✦ watchlist" if r["disc_conviction"] > 0 else "—")
            ins   = _ins_bar(r.get("insider_signal", 0))
            lines.append(
                f"| **{r['ticker']}** | {r['screen_count']} | {r['conviction']:.1f} "
                f"| {miner} | {rsi} | {stage_short} | {ins} | {price} | {cross} |"
            )
        lines += [
            "",
            "<details>",
            "<summary>Screen membership detail</summary>",
            "",
        ]
        for r in multi_results[:20]:
            lines.append(f"- **{r['ticker']}**: {', '.join(r['screens'])}")
        lines += ["", "</details>", ""]
    else:
        lines.append("*No convergent multibagger candidates found in this run.*\n")

    lines += [
        "### Onboarding checklist (before adding to stocks/)",
        "- Run `python3 multibagger_screener.py --ticker TICKER` for full technical score",
        "- Load or create `KNOWLEDGE_BASE/tickers/TICKER.md` with stage + thesis",
        "- Verify sector alignment with active portfolio themes",
        "- Check promoter pledge % (flag if > 30%)",
        "",
        "---",
        "",
    ]

    # ── BOTH LISTS ────────────────────────────────────────────────────────────
    if both:
        lines += [
            "## APPEARING IN BOTH LISTS ← highest priority review",
            "*These stocks pass both momentum/breakout screens AND fundamental quality screens.*",
            "",
        ]
        for ticker in sorted(both):
            lines.append(f"- **{ticker}**")
        lines += ["", "---", ""]

    # ── ALREADY IN MULTIBAGGER SCAN ───────────────────────────────────────────
    all_chartink = {r["ticker"] for r in short_results + multi_results}
    mb_cands     = json.loads(MULTIBAGGER_PATH.read_text()).get("candidates", []) \
                   if MULTIBAGGER_PATH.exists() else []
    confirmed    = all_chartink & {r["ticker"] for r in mb_cands}
    if confirmed:
        lines += [
            "## DOUBLE-CONFIRMED (Chartink + multibagger_scan.json)",
            "",
        ]
        for ticker in sorted(confirmed):
            lines.append(f"- **{ticker}** — appears in both Chartink screens and our deep multibagger pipeline")
        lines += ["", "---", ""]

    # ── SCREENER QUALITY INDEX ────────────────────────────────────────────────
    lines += [
        "## Screener Quality Index",
        "*Screens used this run, with our quality rating and logic summary.*",
        "",
        "### Short-Term Screens",
        "",
        "| Screen | Likes | Rating | Logic Summary |",
        "|--------|-------|--------|---------------|",
    ]
    for s in sorted(st_screens, key=lambda x: -x.get("likes", 0)):
        logic = (s.get("logic_summary") or "")[:80].replace("|", "·")
        lines.append(
            f"| [{s['title'][:40]}]({s['url']}) | {s.get('likes', '?'):,} "
            f"| {_stars(s.get('likes', 0))} | {logic} |"
        )

    lines += [
        "",
        "### Multibagger Screens",
        "",
        "| Screen | Likes | Rating | Logic Summary |",
        "|--------|-------|--------|---------------|",
    ]
    for s in sorted(mb_screens, key=lambda x: -x.get("likes", 0)):
        logic = (s.get("logic_summary") or "")[:80].replace("|", "·")
        lines.append(
            f"| [{s['title'][:40]}]({s['url']}) | {s.get('likes', '?'):,} "
            f"| {_stars(s.get('likes', 0))} | {logic} |"
        )

    lines += [
        "",
        "---",
        "",
        f"*Next run: after fresh insider data ingestion or weekly portfolio review.*",
    ]

    OUTPUT_PATH.write_text("\n".join(lines) + "\n")
    print(f"\n→ {OUTPUT_PATH}")


# ── Entry point ───────────────────────────────────────────────────────────────

def run(browse_only: bool = False, fast: bool = False, refresh: bool = False):
    pw_ctx, browser, ctx = _make_browser()
    page = ctx.new_page()

    try:
        catalogue = phase1_browse(page, refresh=refresh)

        if browse_only:
            counts = Counter(s["type"] for s in catalogue)
            print(f"\nCatalogue summary:")
            print(f"  SHORT_TERM:  {counts['SHORT_TERM']}")
            print(f"  MULTIBAGGER: {counts['MULTIBAGGER']}")
            print(f"  SKIP:        {counts['SKIP']}")
            print(f"\nTop MULTIBAGGER screens:")
            for s in [x for x in catalogue if x["type"] == "MULTIBAGGER"][:10]:
                print(f"  [{s['category']}] {s['title']}")
            print(f"\nTop SHORT_TERM screens:")
            for s in [x for x in catalogue if x["type"] == "SHORT_TERM"][:10]:
                print(f"  [{s['category']}] {s['title']}")
            return

        enriched = phase2_download(page, catalogue)

        if not enriched:
            print("No screeners yielded results. Try --refresh-catalogue or check connectivity.")
            return

        short_results, multi_results = phase3_convergence(enriched, fast=fast)
        phase4_write_md(short_results, multi_results, enriched)

    finally:
        browser.close()
        pw_ctx.__exit__(None, None, None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Chartink community screener intelligence — convergence + technical pipeline"
    )
    parser.add_argument("--browse",             action="store_true",
                        help="Phase 1 only: catalogue screeners without downloading CSVs")
    parser.add_argument("--fast",               action="store_true",
                        help="Skip OHLC technical enrichment (Phase 1+2 only)")
    parser.add_argument("--refresh-catalogue",  action="store_true",
                        help="Force re-browse even if catalogue cache is fresh")
    args = parser.parse_args()

    run(browse_only=args.browse, fast=args.fast, refresh=args.refresh_catalogue)
