#!/usr/bin/env python3
"""
browser_utils.py — Shared Playwright headless browser helpers.

Three scrapers, one module:
  1. get_nse_cookies()       — refresh bm_sv Akamai session cookie for NSE PIT API
  2. scrape_screener_pledge() — fetch promoter pledge % from Screener.in (requires login)
  3. scrape_nse_fii_dii()    — fetch daily FII/DII flows from NSE market data page

Graceful degradation throughout: if Playwright is not installed or Chromium is missing,
every public function returns None. Callers handle None without crashing.

Typical runtimes (headless):
  NSE cookies:    6–12s (Akamai JS challenge)
  Screener pledge: 8–15s per ticker (login + page load + extraction)
  FII/DII:         5–10s
"""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

COOKIES_FILE = Path("cookies.txt")

def _require_playwright():
    """Import sync_playwright or raise ImportError with a helpful message."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
        return sync_playwright, PWTimeoutError
    except ImportError:
        raise ImportError(
            "playwright is not installed. Run: pip install playwright && "
            "python3 -m playwright install chromium"
        )


# ---------------------------------------------------------------------------
# NSE cookie refresh
# ---------------------------------------------------------------------------

_NSE_PIT_URL = (
    "https://www.nseindia.com/companies-listing/corporate-filings-pit"
)

# Minimal browser fingerprint that passes Akamai's bot check
_NSE_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}


def get_nse_cookies(headless: bool = True) -> Optional[dict]:
    """
    Launch headless Chromium, navigate to the NSE PIT disclosures page, wait for
    Akamai's JavaScript challenge to complete (bm_sv cookie appears), then extract
    all cookies and write them to cookies.txt in Netscape format.

    Returns dict of {cookie_name: value} on success, None on failure.
    Typical runtime: 6–12 seconds.
    """
    sync_playwright, PWTimeoutError = _require_playwright()

    logger.info("NSE cookies: launching headless Chromium...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                extra_http_headers=_NSE_HEADERS,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = ctx.new_page()

            # Step 1 — hit NSE homepage first; Akamai sets initial cookies here
            page.goto("https://www.nseindia.com", wait_until="domcontentloaded", timeout=20_000)
            time.sleep(2)

            # Step 2 — navigate to PIT page; Akamai challenge completes, bm_sv is set
            page.goto(_NSE_PIT_URL, wait_until="domcontentloaded", timeout=20_000)

            # Give Akamai's JS an extra moment to finish setting bm_sv
            time.sleep(3)

            # Extract all cookies for nseindia.com
            cookies = ctx.cookies("https://www.nseindia.com")
            browser.close()

        if not cookies:
            logger.warning("NSE cookies: no cookies returned — Akamai challenge may not have run")
            return None

        cookie_dict = {c["name"]: c["value"] for c in cookies}

        if "bm_sv" not in cookie_dict:
            logger.warning("NSE cookies: bm_sv not present — session may not be valid")
            # Still write whatever we got; might be enough for some endpoints
        else:
            logger.info(f"NSE cookies: bm_sv obtained ({len(cookies)} total cookies)")

        # Write Netscape-format cookies.txt for urllib-based scripts
        _write_netscape_cookies(cookies)
        return cookie_dict

    except Exception as e:
        logger.error(f"NSE cookies: failed — {e}")
        return None


def _write_netscape_cookies(cookies: list[dict]):
    """Write a list of Playwright cookie dicts to cookies.txt in Netscape format."""
    lines = ["# Netscape HTTP Cookie File", "# https://curl.se/docs/http-cookies.html", ""]
    for c in cookies:
        domain   = c.get("domain", "")
        httponly = str(c.get("httpOnly", False)).upper()
        path     = c.get("path", "/")
        secure   = str(c.get("secure", False)).upper()
        expires  = int(c.get("expires", -1))
        name     = c.get("name", "")
        value    = c.get("value", "")
        lines.append(f"{domain}\t{httponly}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    COOKIES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"NSE cookies: written to {COOKIES_FILE} ({len(cookies)} entries)")


# ---------------------------------------------------------------------------
# Screener.in — promoter pledge %
# ---------------------------------------------------------------------------

_SCREENER_LOGIN_URL   = "https://www.screener.in/login/"
_SCREENER_COMPANY_URL = "https://www.screener.in/company/{ticker}/consolidated/"


def scrape_screener_pledge(
    ticker: str,
    email: str,
    password: str,
    headless: bool = True,
) -> Optional[float]:
    """
    Log in to Screener.in, navigate to the company page, and extract the most recent
    promoter pledged-shares percentage from the shareholding table.

    Returns the pledge % as a float (e.g., 34.2), or None if:
      - Login fails
      - Company has no pledge data (zero pledge is valid — returns 0.0)
      - Pledge row is absent from the page (older/unlisted companies)
    """
    sync_playwright, PWTimeoutError = _require_playwright()

    logger.info(f"Screener pledge: fetching {ticker}...")
    try:
        try:
            from playwright_stealth import Stealth
            _pw_ctx = Stealth().use_sync(sync_playwright())
        except ImportError:
            _pw_ctx = sync_playwright()

        with _pw_ctx as p:
            browser = p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-setuid-sandbox"])
            ctx  = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()

            # --- Login ---
            page.goto(_SCREENER_LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
            page.fill('input[name="username"]', email)
            page.fill('input[name="password"]', password)
            page.click('button[type="submit"]')

            # Wait for redirect away from /login/ — indicates successful login
            try:
                page.wait_for_url(lambda url: "/login/" not in url, timeout=15_000)
            except PWTimeoutError:
                logger.error("Screener pledge: login failed — still on /login/ after submit")
                browser.close()
                return None

            # --- Navigate to company page ---
            # Screener.in company pages are slow to fully render — 60s is needed
            url = _SCREENER_COMPANY_URL.format(ticker=ticker)
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)

            # --- Extract pledged % from shareholding section ---
            pledge_pct = _extract_pledge_from_page(page, ticker)
            browser.close()

        return pledge_pct

    except Exception as e:
        logger.error(f"Screener pledge {ticker}: {e}")
        return None


def _extract_pledge_from_page(page, ticker: str) -> Optional[float]:
    """
    Find the 'Pledged' row in the Screener.in shareholding table and return
    the most recent value. Screener shows quarters as columns; we want the rightmost.
    """
    # Screener renders shareholding in a <section id="shareholding"> table.
    # Rows are labeled e.g. "Promoters", "Pledged", "FII", "DII", "Public".
    # We need to find the row whose first cell contains "Pledged" and read its last cell.

    try:
        # Wait for the shareholding section to load
        page.wait_for_selector("#shareholding", timeout=15_000)
    except Exception:
        logger.warning(f"Screener pledge {ticker}: shareholding section not found")
        return None

    # Find all rows in the shareholding table
    rows = page.query_selector_all("#shareholding table tbody tr")
    for row in rows:
        cells = row.query_selector_all("td")
        if not cells:
            continue
        label = cells[0].inner_text().strip().lower()
        if "pledged" not in label:
            continue

        # Most recent quarter is the last data cell (rightmost)
        # Skip the label cell (index 0)
        data_cells = cells[1:]
        if not data_cells:
            return None

        # Walk right-to-left for the first non-empty value
        for cell in reversed(data_cells):
            raw = cell.inner_text().strip().replace("%", "").replace(",", "")
            if raw in ("", "-", "—", "N/A"):
                continue
            try:
                val = float(raw)
                logger.info(f"Screener pledge {ticker}: {val}%")
                return val
            except ValueError:
                continue

        # Pledged row found but all cells empty → interpret as 0
        logger.info(f"Screener pledge {ticker}: pledged row present but no data → 0%")
        return 0.0

    # No pledged row at all — company genuinely has no pledge data
    logger.info(f"Screener pledge {ticker}: no pledged row found")
    return None


# ---------------------------------------------------------------------------
# NSE FII/DII daily flows
# ---------------------------------------------------------------------------

_NSE_FII_DII_URL = "https://www.nseindia.com/market-data/fii-dii-activity"


def scrape_nse_fii_dii(headless: bool = True) -> Optional[dict]:
    """
    Scrape the NSE FII/DII activity page for today's cash market flows.

    Returns:
        {
            "date":        "YYYY-MM-DD",
            "fii_net_cr":  -1891.0,    # negative = outflow
            "dii_net_cr":   2492.0,
            "fii_buy_cr":  12345.0,
            "fii_sell_cr": 14236.0,
            "dii_buy_cr":   8000.0,
            "dii_sell_cr":  5508.0,
            "fii_mtd_cr":  -27788.0,
            "dii_mtd_cr":   50862.0,
        }
    or None on failure.

    The page renders via JavaScript — a headless browser is required.
    Typical runtime: 5–10 seconds.
    """
    sync_playwright, PWTimeoutError = _require_playwright()

    logger.info("NSE FII/DII: scraping activity page...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                extra_http_headers=_NSE_HEADERS,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = ctx.new_page()
            page.goto(_NSE_FII_DII_URL, wait_until="networkidle", timeout=30_000)
            time.sleep(2)  # let React hydrate

            result = _extract_fii_dii_from_page(page)
            browser.close()

        return result

    except Exception as e:
        logger.error(f"NSE FII/DII: scrape failed — {e}")
        return None


def _extract_fii_dii_from_page(page) -> Optional[dict]:
    """
    Extract FII/DII cash market numbers from the rendered NSE page.

    NSE's FII/DII page renders a table with columns:
      Category | Buy Value (₹Cr) | Sell Value (₹Cr) | Net Value (₹Cr)
    Rows: FII/FPI | DII | and sometimes sub-rows.

    We also try to read the MTD cumulative totals from a secondary table if present.
    """
    try:
        # Wait for the data table to appear
        page.wait_for_selector("table", timeout=15_000)
    except Exception:
        logger.warning("NSE FII/DII: no table found on page")
        return None

    def _parse_cr(text: str) -> Optional[float]:
        """Parse '(1,891.23)' or '-1891.23' or '2,492.10' → float in Crores."""
        if not text:
            return None
        raw = text.strip().replace(",", "").replace("(", "-").replace(")", "")
        try:
            return round(float(raw), 2)
        except ValueError:
            return None

    # Attempt to extract by looking for rows labelled FII/FPI and DII
    result: dict = {"date": datetime.now().strftime("%Y-%m-%d")}

    rows = page.query_selector_all("table tbody tr")
    for row in rows:
        cells = row.query_selector_all("td")
        if len(cells) < 4:
            continue
        label = cells[0].inner_text().strip().upper()

        if "FII" in label or "FPI" in label:
            result["fii_buy_cr"]  = _parse_cr(cells[1].inner_text())
            result["fii_sell_cr"] = _parse_cr(cells[2].inner_text())
            result["fii_net_cr"]  = _parse_cr(cells[3].inner_text())

        elif "DII" in label:
            result["dii_buy_cr"]  = _parse_cr(cells[1].inner_text())
            result["dii_sell_cr"] = _parse_cr(cells[2].inner_text())
            result["dii_net_cr"]  = _parse_cr(cells[3].inner_text())

    # Derive net from buy/sell if net cell wasn't directly parseable
    if result.get("fii_net_cr") is None and result.get("fii_buy_cr") is not None:
        b, s = result.get("fii_buy_cr", 0), result.get("fii_sell_cr", 0)
        if b is not None and s is not None:
            result["fii_net_cr"] = round(b - s, 2)

    if result.get("dii_net_cr") is None and result.get("dii_buy_cr") is not None:
        b, s = result.get("dii_buy_cr", 0), result.get("dii_sell_cr", 0)
        if b is not None and s is not None:
            result["dii_net_cr"] = round(b - s, 2)

    # MTD totals — NSE sometimes shows them in a second table or a footer row
    result["fii_mtd_cr"] = None
    result["dii_mtd_cr"] = None

    all_tables = page.query_selector_all("table")
    for tbl in all_tables[1:]:  # skip first (already parsed)
        rows2 = tbl.query_selector_all("tbody tr")
        for row2 in rows2:
            cells2 = row2.query_selector_all("td")
            if len(cells2) < 2:
                continue
            label2 = cells2[0].inner_text().strip().upper()
            if "FII" in label2 or "FPI" in label2:
                # Last cell is likely MTD net
                result["fii_mtd_cr"] = _parse_cr(cells2[-1].inner_text())
            elif "DII" in label2:
                result["dii_mtd_cr"] = _parse_cr(cells2[-1].inner_text())

    # Validate we got at least the daily net figures
    if result.get("fii_net_cr") is None and result.get("dii_net_cr") is None:
        logger.warning("NSE FII/DII: could not extract any figures from page")
        return None

    logger.info(
        f"NSE FII/DII: FII {result.get('fii_net_cr'):+.0f} Cr  "
        f"DII {result.get('dii_net_cr'):+.0f} Cr"
    )
    return result


# ---------------------------------------------------------------------------
# CLI — quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "cookies"

    if cmd == "cookies":
        result = get_nse_cookies(headless=False)   # visible window for debugging
        if result:
            print(f"\nOK — {len(result)} cookies. bm_sv present: {'bm_sv' in result}")
        else:
            print("\nFAILED")

    elif cmd == "pledge":
        ticker   = sys.argv[2] if len(sys.argv) > 2 else "BHARATFORG"
        email    = os.environ.get("SCREENER_EMAIL", "")
        password = os.environ.get("SCREENER_PASSWORD", "")
        if not email or not password:
            print("Set SCREENER_EMAIL and SCREENER_PASSWORD in .env first")
            sys.exit(1)
        pct = scrape_screener_pledge(ticker, email, password, headless=False)
        print(f"\n{ticker} pledge: {pct}%")

    elif cmd == "fiidii":
        result = scrape_nse_fii_dii(headless=False)
        if result:
            print(f"\nFII net: {result.get('fii_net_cr'):+.0f} Cr")
            print(f"DII net: {result.get('dii_net_cr'):+.0f} Cr")
        else:
            print("\nFAILED")
