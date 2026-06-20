#!/usr/bin/env python3
"""
Fetch today's FII/DII cash market flows from NSE and store in market.db.

Primary source: `nselib.capital_market.capital_market_data.fii_dii_trading_activity()`
hits NSE's API endpoint (`/api/fiidiiTradeReact`) with a primed-cookie session —
more resilient to Akamai than raw Playwright. Fallback path is the previous
Playwright scraper in `browser_utils.scrape_nse_fii_dii`; kept for one release
in case nselib regresses.

Also updates the `fii_dii` block in data/macro_signals.json in-place,
preserving Claude's macro_themes and signals sections (Python only owns fii_dii).

Usage:
    python3 fetch_fii_dii.py          # fetch today's data
    python3 fetch_fii_dii.py --stats  # print last 30 days from DB
    python3 fetch_fii_dii.py --force  # re-fetch even if today's row exists

Called automatically by LOOP_PROMPT.md Step A7 after each morning digest.
Idempotent — skips the scrape if today's data is already in the DB (unless --force).
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import market_db as mdb

DATA_DIR            = Path("data")
MACRO_SIGNALS_FILE  = DATA_DIR / "macro_signals.json"


def _today_already_fetched(conn) -> bool:
    """True if today's date already has a row in daily_fii_dii."""
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT 1 FROM daily_fii_dii WHERE date = ?", (today,)
    ).fetchone()
    return row is not None


def _update_macro_signals_fii_dii(data: dict):
    """
    Merge the fii_dii block into macro_signals.json, preserving everything else.
    If the file doesn't exist, create a minimal one with just the fii_dii block.
    Python owns only the fii_dii key — Claude owns macro_themes, signals, etc.
    """
    existing = {}
    if MACRO_SIGNALS_FILE.exists():
        try:
            existing = json.loads(MACRO_SIGNALS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    existing["fii_dii"] = {
        "date":        data.get("date"),
        "fii_net_cr":  data.get("fii_net_cr"),
        "dii_net_cr":  data.get("dii_net_cr"),
        "fii_buy_cr":  data.get("fii_buy_cr"),
        "fii_sell_cr": data.get("fii_sell_cr"),
        "dii_buy_cr":  data.get("dii_buy_cr"),
        "dii_sell_cr": data.get("dii_sell_cr"),
        "fii_mtd_cr":  data.get("fii_mtd_cr"),
        "dii_mtd_cr":  data.get("dii_mtd_cr"),
        "source":      data.get("source", "NSE-playwright"),
    }

    MACRO_SIGNALS_FILE.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _print_stats(conn):
    rows = mdb.query_fii_dii(conn, days=30)
    if not rows:
        print("No FII/DII data in DB yet.")
        return
    print(f"\n{'Date':<12} {'FII Net':>10} {'DII Net':>10} {'FII MTD':>12} {'Source':<18}")
    print("─" * 65)
    for r in rows:
        fii     = f"{r['fii_net_cr']:+.0f}" if r["fii_net_cr"] is not None else "—"
        dii     = f"{r['dii_net_cr']:+.0f}" if r["dii_net_cr"] is not None else "—"
        mtd     = f"{r['fii_mtd_cr']:+.0f}" if r["fii_mtd_cr"] is not None else "—"
        source  = r.get("source") or "—"
        print(f"{r['date']:<12} {fii:>10} {dii:>10} {mtd:>12} {source:<18}")
    print()


def _fetch_via_nselib() -> dict | None:
    """
    Primary path: nselib's session-with-primed-cookies hit to NSE's FII/DII API.

    Returns the 10-field row shape `upsert_fii_dii` expects, or None if the
    library is unavailable / the call fails / the schema drifts. Returning
    None signals the caller to try the fallback path; raising would block
    the loop's idempotent retry.

    Schema (as of nselib 2.5.1, NSE provisional endpoint):
        columns: buyValue, sellValue, netValue, category, date
        category values: "FII/FPI", "DII"
        date format: "01-Jun-2026" (DD-MMM-YYYY) — sourced from NSE itself
        amounts: floats, Crores INR
        MTD: not provided by this endpoint (stays None; backfilled by Stage 2)
    """
    # nselib 2.5.1 packaging bug: fii_dii_trading_activity is defined in
    # capital_market_data.py but not re-exported in capital_market/__init__.py,
    # so we import the deep path. If this gets fixed upstream we can shorten
    # the import; harmless either way.
    try:
        from nselib.capital_market.capital_market_data import fii_dii_trading_activity
    except ImportError as e:
        print(f"FII/DII (nselib): not installed or import failed — {e}")
        return None

    try:
        df = fii_dii_trading_activity()
    except Exception as e:
        print(f"FII/DII (nselib): fetch raised {type(e).__name__}: {e}")
        return None

    required_cols = {"buyValue", "sellValue", "netValue", "category", "date"}
    missing = required_cols - set(df.columns)
    if missing:
        print(f"FII/DII (nselib): schema drift — missing columns {missing}; have {list(df.columns)}")
        return None

    fii_rows = df[df["category"] == "FII/FPI"]
    dii_rows = df[df["category"] == "DII"]
    if fii_rows.empty or dii_rows.empty:
        print(f"FII/DII (nselib): schema drift — categories present: {df['category'].unique().tolist()}")
        return None
    fii = fii_rows.iloc[0]
    dii = dii_rows.iloc[0]

    try:
        # NSE returns DD-MMM-YYYY (e.g. "01-Jun-2026"); normalize to ISO.
        api_date = datetime.strptime(str(fii["date"]), "%d-%b-%Y").date().isoformat()
    except (ValueError, TypeError) as e:
        print(f"FII/DII (nselib): date parse failed for {fii['date']!r} — {e}")
        return None

    return {
        "date":        api_date,
        "fii_net_cr":  float(fii["netValue"]),
        "dii_net_cr":  float(dii["netValue"]),
        "fii_buy_cr":  float(fii["buyValue"]),
        "fii_sell_cr": float(fii["sellValue"]),
        "dii_buy_cr":  float(dii["buyValue"]),
        "dii_sell_cr": float(dii["sellValue"]),
        "fii_mtd_cr":  None,
        "dii_mtd_cr":  None,
        "source":      "nselib-nse",
    }


def _fetch_via_playwright() -> dict | None:
    """
    Deprecated fallback. NSE+Playwright is structurally unreliable (Akamai).
    Kept for one release in case nselib regresses; remove in Stage 2.
    """
    try:
        from browser_utils import scrape_nse_fii_dii
    except ImportError:
        print(
            "FII/DII (playwright fallback): playwright not installed — cannot scrape.\n"
            "  pip install playwright && python3 -m playwright install chromium"
        )
        return None

    print("FII/DII: nselib path returned None — falling back to deprecated Playwright scraper.")
    data = scrape_nse_fii_dii(headless=True)
    if data is None:
        print("FII/DII (playwright fallback): scrape failed — see browser_utils logs")
        return None
    data["source"] = "NSE-playwright"
    return data


def fetch(force: bool = False) -> bool:
    """
    Fetch today's FII/DII row, store in DB, update macro_signals.json.
    Tries nselib first, Playwright fallback if nselib unavailable / failed.
    Returns True if data was fetched, False if skipped (already fresh) or failed.
    """
    conn = mdb.get_conn()

    if not force and _today_already_fetched(conn):
        print(f"FII/DII: today's data already in DB — skipping (use --force to override)")
        conn.close()
        return False

    print("FII/DII: fetching via nselib (primary)...")
    data = _fetch_via_nselib()
    if data is None:
        data = _fetch_via_playwright()
        if data is None:
            print("FII/DII: both primary and fallback paths failed — no row written.")
            conn.close()
            return False

    mdb.upsert_fii_dii(conn, data)
    conn.commit()
    conn.close()

    _update_macro_signals_fii_dii(data)

    fii = data.get("fii_net_cr")
    dii = data.get("dii_net_cr")
    print(
        f"FII/DII: stored for {data['date']} via {data['source']} — "
        f"FII {fii:+.0f} Cr  DII {dii:+.0f} Cr"
        if (fii is not None and dii is not None)
        else f"FII/DII: stored for {data['date']} via {data['source']} (some fields missing)"
    )
    print(f"  macro_signals.json fii_dii block updated.")
    return True


if __name__ == "__main__":
    force = "--force" in sys.argv
    stats = "--stats" in sys.argv

    if stats:
        conn = mdb.get_conn()
        _print_stats(conn)
        conn.close()
    else:
        fetch(force=force)
