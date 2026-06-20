"""
News scraper for the discovery pipeline (Phase 2).

Three sources:
  1. ET Markets (RSS) — ~50 entries/day, decent summaries
  2. LiveMint (RSS) — ~35 entries/day, teaser summaries
  3. MoneyControl (HTML scrape) — ~25 entries/day, RSS is dead since 2024

For entries that match tickers or themes, the scraper fetches the full
article body (~500 words) so signals contain real substance, not teasers.

Extracts tickers, matches to sectors, generates templated causal chains
for known patterns.  Novel signals get flagged as needs_review for
weekly Claude processing on Sundays.

Zero daily token cost — all Python, no Claude calls.
Sunday cost: ~5-15 unmatched headlines reviewed by Claude (tiny).

Output: data/news_signals.json  (sector-keyed, supersede-on-update)
State:  data/news_scraper_state.json  (seen headline hashes)

Usage:
    python3 news_scraper.py          # manual test run
    # Called from alert_bot/main.py daily after market close
"""
import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_DATA_DIR = Path("data")
_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
}
_HTTP_TIMEOUT = 10

# ---------------------------------------------------------------------------
# Feed sources
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    {
        "name": "ET Markets",
        "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    },
    {
        "name": "LiveMint",
        "url": "https://www.livemint.com/rss/markets",
    },
]

# MoneyControl RSS is dead (last entry April 2024).  We scrape their
# markets + stocks news pages instead — same headlines, just HTML.
MC_PAGES = [
    "https://www.moneycontrol.com/news/business/markets/",
    "https://www.moneycontrol.com/news/business/stocks/",
]

# ---------------------------------------------------------------------------
# State management — track which headlines we've already processed
# ---------------------------------------------------------------------------

_STATE_FILE = _DATA_DIR / "news_scraper_state.json"


def _load_state() -> dict:
    """Load seen headline hashes + metadata."""
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen_hashes": [], "last_run": None}


def _save_state(state: dict) -> None:
    """Persist state.  Keep only last 2000 hashes (rolling window)."""
    state["seen_hashes"] = state["seen_hashes"][-2000:]
    state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _strip_html(text: str) -> str:
    """Remove HTML tags from RSS summary text."""
    return re.sub(r'<[^>]+>', '', text).strip()


def _hash_headline(title: str) -> str:
    """Deterministic hash of a headline for dedup."""
    return hashlib.sha256(title.strip().lower().encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Stock universe loader — builds ticker set + company name → ticker map
# ---------------------------------------------------------------------------

def _load_universe() -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    """
    Returns:
        ticker_set:   {"ONGC": "Oil & Gas / Energy", ...}  — ticker → sector
        name_map:     {"tata steel": "TATASTEEL", ...}      — lowercase name fragment → ticker
        sector_tickers: {"Oil & Gas / Energy": ["ONGC", ...]} — sector → tickers list
    """
    path = _DATA_DIR / "stock_universe.json"
    if not path.exists():
        return {}, {}, {}

    data = json.loads(path.read_text(encoding="utf-8"))
    themes = data.get("themes", {})

    ticker_set: dict[str, str] = {}
    name_map: dict[str, str] = {}
    sector_tickers: dict[str, list[str]] = {}

    for theme_name, theme_data in themes.items():
        tickers_in_sector = []
        for tier_key in ("tier1", "tier2"):
            for stock in theme_data.get(tier_key, []):
                ticker = stock.get("ticker", "")
                note = stock.get("note", "")
                if not ticker:
                    continue

                ticker_set[ticker] = theme_name
                tickers_in_sector.append(ticker)

                # Build name fragments for matching headlines to tickers.
                # E.g. "Tata Steel" from note "Largest private steelmaker..."
                # We extract the ticker itself and common name forms.
                name_map[ticker.lower()] = ticker

                # Extract company-like names from the note field.
                # Many notes start with the company name or mention it.
                # We also add the ticker as-is for direct matching.
                if note:
                    # Some notes mention brand names or subsidiaries
                    # e.g. "Royal Enfield" for EICHERMOT, "Fevicol" for PIDILITIND
                    # We'll match these via keyword extraction in headlines
                    pass

        sector_tickers[theme_name] = tickers_in_sector

    # Also load portfolio stocks from the excluded list for matching
    # (portfolio stocks in CLAUDE.md Active Stocks table)
    portfolio_map = {
        "ANANTRAJ": "Real Estate & Construction",
        "BBOX": "Telecom & Digital",
        "BDL": "Defense & Aerospace",
        "CGPOWER": "Infrastructure & Capex",
        "HBLENGINE": "Defense & Aerospace",
        "HINDCOPPER": "Metals & Mining",
        "RAILTEL": "Telecom & Digital",
        "SHARDACROP": "Chemicals & Specialty",
        "SPARC": "Pharma & Healthcare",
        "STLTECH": "Telecom & Digital",
        "SUZLON": "Renewable Energy & Green",
        "SUVEN": "Pharma & Healthcare",
    }
    for t, s in portfolio_map.items():
        if t not in ticker_set:
            ticker_set[t] = s

    return ticker_set, name_map, sector_tickers


# ---------------------------------------------------------------------------
# Ticker extraction from headlines
# ---------------------------------------------------------------------------

# Common company name → ticker mappings that RSS headlines use
# (headlines say "Tata Steel" not "TATASTEEL")
_HEADLINE_NAME_MAP = {
    "tata steel": "TATASTEEL",
    "tata motors": "TATAMOTORS",
    "tata power": "TATAPOWER",
    "tata comm": "TATACOMM",
    "reliance": "RELIANCE",
    "infosys": "INFY",
    "wipro": "WIPRO",
    "hcl tech": "HCLTECH",
    "tech mahindra": "TECHM",
    "hdfc bank": "HDFCBANK",
    "icici bank": "ICICIBANK",
    "kotak bank": "KOTAKBANK",
    "kotak mahindra": "KOTAKBANK",
    "axis bank": "AXISBANK",
    "state bank": "SBIN",
    "sbi": "SBIN",
    "bajaj auto": "BAJAJ-AUTO",
    "bajaj finance": "BAJFINANCE",
    "maruti suzuki": "MARUTI",
    "maruti": "MARUTI",
    "mahindra": "M&M",
    "m&m": "M&M",
    "sun pharma": "SUNPHARMA",
    "dr reddy": "DRREDDY",
    "cipla": "CIPLA",
    "divis lab": "DIVISLAB",
    "pidilite": "PIDILITIND",
    "hindustan unilever": "HINDUNILVR",
    "hul": "HINDUNILVR",
    "itc": "ITC",
    "nestle": "NESTLEIND",
    "britannia": "BRITANNIA",
    "adani green": "ADANIGREEN",
    "adani ports": "ADANIPORTS",
    "adani enterprises": "ADANIENT",
    "jsw steel": "JSWSTEEL",
    "hindalco": "HINDALCO",
    "vedanta": "VEDL",
    "nmdc": "NMDC",
    "ongc": "ONGC",
    "bpcl": "BPCL",
    "ioc": "IOC",
    "gail": "GAIL",
    "hal": "HAL",
    "bel": "BEL",
    "mazagon": "MAZAGON",
    "cochin shipyard": "COCHINSHIP",
    "l&t": "LT",
    "larsen": "LT",
    "siemens": "SIEMENS",
    "abb": "ABB",
    "irctc": "IRCTC",
    "dlf": "DLF",
    "godrej properties": "GODREJPROP",
    "dixon": "DIXON",
    "bharti airtel": "BHARTIARTL",
    "airtel": "BHARTIARTL",
    "suzlon": "SUZLON",
    "nhpc": "NHPC",
    "manappuram": "MANAPPURAM",
    "upl": "UPL",
    "royal enfield": "EICHERMOT",
    "eicher": "EICHERMOT",
    "goldbees": "GOLDBEES",
    "nifty": "_NIFTY",       # market-wide, not a stock
    "sensex": "_SENSEX",     # market-wide, not a stock
}


def _extract_tickers(headline: str, ticker_set: dict[str, str]) -> list[str]:
    """
    Extract stock tickers mentioned in a headline.

    Strategy:
    1. Check for direct ticker mentions (e.g. "TATASTEEL" in headline)
    2. Check for company name mentions (e.g. "Tata Steel" in headline)

    Returns list of matched tickers (may be empty).
    """
    headline_lower = headline.lower()
    found: set[str] = set()

    # Strategy 1: direct ticker match (word boundary)
    for ticker in ticker_set:
        # Only match tickers >= 3 chars to avoid false positives (e.g. "IT" matching everywhere)
        if len(ticker) >= 3:
            pattern = r'\b' + re.escape(ticker) + r'\b'
            if re.search(pattern, headline, re.IGNORECASE):
                found.add(ticker)

    # Strategy 2: company name match
    for name, ticker in _HEADLINE_NAME_MAP.items():
        if name in headline_lower:
            if not ticker.startswith("_"):  # skip market indices
                found.add(ticker)

    return sorted(found)


# ---------------------------------------------------------------------------
# Keyword extraction for template matching
# ---------------------------------------------------------------------------

# Keywords that map to market themes.  Each keyword group represents
# a recognizable market narrative that has predictable sector effects.
_THEME_KEYWORDS: dict[str, list[str]] = {
    "crude_up": ["crude oil", "brent", "oil price", "oil surge", "crude surge",
                  "oil rally", "opec cut", "opec+", "oil rises", "crude hits",
                  "oil hits", "petroleum price"],
    "crude_down": ["oil falls", "crude falls", "oil drops", "crude drops",
                   "oil slump", "crude slump", "brent falls", "brent drops"],
    "rate_cut": ["rate cut", "repo rate cut", "rbi cuts", "monetary easing",
                 "dovish rbi", "rate reduction", "policy rate"],
    "rate_hike": ["rate hike", "repo rate hike", "rbi hikes", "monetary tightening",
                  "hawkish rbi", "rate increase"],
    "fii_outflow": ["fii sell", "fii outflow", "foreign outflow", "fpi sell",
                    "fpi outflow", "foreign investors sell", "fii pullout"],
    "fii_inflow": ["fii buy", "fii inflow", "foreign inflow", "fpi buy",
                   "fpi inflow", "foreign investors buy"],
    "rupee_weak": ["rupee falls", "rupee weakens", "rupee hits low", "inr deprec",
                   "rupee slides", "rupee slips", "dollar strengthens against rupee"],
    "rupee_strong": ["rupee rises", "rupee strengthens", "rupee gains",
                     "inr appreci", "rupee recovers"],
    "gold_up": ["gold price", "gold surge", "gold rally", "gold hits",
                "gold rises", "gold record", "gold all-time"],
    "gold_down": ["gold falls", "gold drops", "gold slump", "gold declines"],
    "budget": ["union budget", "fiscal deficit", "budget allocation", "budget 2026",
               "budget 2027", "defence budget", "defense budget", "capex allocation"],
    "defense_order": ["defence order", "defense order", "defense contract",
                      "defence contract", "military order", "missile order",
                      "navy order", "iaf order", "army order"],
    "pmi_strong": ["pmi expan", "manufacturing pmi", "pmi above 50", "pmi rises",
                   "factory output", "industrial production rises"],
    "pmi_weak": ["pmi contract", "pmi below 50", "pmi falls", "pmi declines",
                 "factory output falls", "industrial production falls"],
    "earnings_strong": ["profit rises", "profit jumps", "revenue grows", "earnings beat",
                        "strong quarter", "record profit", "pat grows", "ebitda grows"],
    "earnings_weak": ["profit falls", "profit drops", "revenue declines", "earnings miss",
                      "weak quarter", "profit dips", "pat falls", "ebitda declines"],
    "geopolitical": ["geopolitical", "military tension", "border tension",
                     "war", "conflict escalat", "sanctions", "strait of hormuz",
                     "south china sea", "taiwan strait"],
    "inflation_high": ["inflation rises", "inflation spikes", "cpi rises",
                       "wpi rises", "food inflation", "inflation above"],
    "inflation_low": ["inflation falls", "inflation eases", "cpi falls",
                      "wpi falls", "inflation below", "disinflation"],
    "it_deal": ["it deal", "outsourcing deal", "digital transformation deal",
                "cloud deal", "ai deal", "tech partnership"],
    "pharma_fda": ["fda approv", "usfda", "us fda", "anda approv",
                   "drug approv", "clinical trial", "phase 3"],
    "monsoon": ["monsoon", "rainfall", "kharif", "rabi", "crop output",
                "agricultural output", "farm output"],
    "china_stimulus": ["china stimulus", "pboc", "china rate cut", "china easing",
                       "beijing stimulus", "china growth"],
    "us_fed": ["fed rate", "federal reserve", "fomc", "powell", "us rate",
               "fed cut", "fed hike", "fed pause"],
    "ipo_listing": ["ipo", "listing", "grey market premium", "gmp",
                    "public offer", "book building"],
    "ev_policy": ["ev policy", "electric vehicle policy", "fame subsidy",
                  "ev subsidy", "battery policy", "charging infrastructure"],
    "pli_scheme": ["pli scheme", "production linked incentive", "pli approv",
                   "pli benefit"],
    "realty_demand": ["housing demand", "home sales", "real estate demand",
                      "property sales", "registration", "rera"],
    "5g_telecom": ["5g", "spectrum auction", "telecom tariff", "arpu",
                   "tower", "fiber rollout", "broadband"],
    "solar_wind": ["solar capacity", "wind capacity", "renewable capacity",
                   "green energy", "solar tender", "wind tender"],
    "steel_price": ["steel price", "hrc price", "steel demand", "steel output",
                    "iron ore price"],
    "copper_price": ["copper price", "copper demand", "copper surplus",
                     "copper deficit", "copper hits"],
}


def _extract_themes(headline: str) -> list[str]:
    """Match headline against known theme keywords.  Returns list of theme keys."""
    headline_lower = headline.lower()
    matched = []
    for theme, keywords in _THEME_KEYWORDS.items():
        for kw in keywords:
            if kw in headline_lower:
                matched.append(theme)
                break  # one keyword match per theme is enough
    return matched


# ---------------------------------------------------------------------------
# Template library — sector × theme → causal chain
# ---------------------------------------------------------------------------
# Each template is keyed by (sector, theme).  {tickers} placeholder gets
# filled with affected tickers from the headline or sector roster.
# Templates encode the SECOND-ORDER reasoning: not "oil is up" but
# "oil up → this is what it means for THIS sector."

CAUSAL_TEMPLATES: dict[tuple[str, str], str] = {
    # Oil & Gas
    ("Oil & Gas / Energy", "crude_up"):
        "Rising crude → higher realizations for upstream E&P ({tickers}). "
        "Downstream refining margins may compress if crack spreads narrow.",
    ("Oil & Gas / Energy", "crude_down"):
        "Falling crude → upstream revenue pressure ({tickers}). "
        "Refiners benefit from inventory gains + wider spreads.",

    # Chemicals — petrochemical feedstock cost
    ("Chemicals & Specialty", "crude_up"):
        "Rising crude → petrochemical feedstock cost pressure across "
        "chemical value chain ({tickers}). Watch for margin guidance revisions.",
    ("Chemicals & Specialty", "crude_down"):
        "Falling crude → raw material cost relief for specialty chemicals ({tickers}). "
        "Margin expansion possible if product prices hold.",

    # Renewable Energy
    ("Renewable Energy & Green", "crude_up"):
        "Crude surge reinforces long-term renewable substitution thesis ({tickers}). "
        "Policy tailwind intact — higher fossil costs = faster green transition.",
    ("Renewable Energy & Green", "solar_wind"):
        "New renewable capacity additions / tenders benefit equipment makers "
        "and project developers ({tickers}). Order book visibility improving.",
    ("Renewable Energy & Green", "ev_policy"):
        "EV / green energy policy support extends to renewable infrastructure ({tickers}). "
        "Charging + grid integration demand grows alongside EV adoption.",

    # Banking — rate sensitivity
    ("Banking & Financials", "rate_cut"):
        "Rate cut → NIM pressure near-term but loan growth accelerates ({tickers}). "
        "Treasury gains from bond portfolio mark-to-market.",
    ("Banking & Financials", "rate_hike"):
        "Rate hike → NIM expansion near-term ({tickers}). "
        "But credit growth may slow as borrowing costs rise.",
    ("Banking & Financials", "fii_outflow"):
        "FII outflows pressure bank stocks with high FII holding ({tickers}). "
        "Fundamental impact minimal — watch for valuation re-entry points.",
    ("Banking & Financials", "fii_inflow"):
        "FII inflows provide buying support for large-cap financials ({tickers}). "
        "Liquidity-driven re-rating possible.",

    # IT Services
    ("IT Services & SaaS", "rupee_weak"):
        "Weak rupee → revenue tailwind for IT exporters ({tickers}). "
        "Each ₹1 depreciation adds ~1.5-2% to margins.",
    ("IT Services & SaaS", "rupee_strong"):
        "Strong rupee → headwind for IT revenue in ₹ terms ({tickers}). "
        "Cross-currency hedging may partially offset.",
    ("IT Services & SaaS", "earnings_weak"):
        "Weak IT earnings/guidance → near-term sector headwind for {tickers}. "
        "Capex moderation signals downstream demand impact.",
    ("IT Services & SaaS", "earnings_strong"):
        "Strong IT earnings signal healthy tech spending ({tickers}). "
        "Deal pipeline strength matters more than current quarter.",
    ("IT Services & SaaS", "us_fed"):
        "US rate decision impacts IT client budgets ({tickers}). "
        "Rate cuts = tech spending recovery; hikes = continued caution.",
    ("IT Services & SaaS", "it_deal"):
        "Major IT deal win signals sector demand health ({tickers}). "
        "Large deal TCV growth typically leads revenue acceleration by 2-3 quarters.",

    # Defense
    ("Defense & Aerospace", "budget"):
        "Defense budget expansion → order pipeline growth for {tickers}. "
        "Execution typically weighted H2 of fiscal year.",
    ("Defense & Aerospace", "defense_order"):
        "New defense order → direct revenue visibility for {tickers}. "
        "Order-to-revenue conversion typically 2-3 years for complex systems.",
    ("Defense & Aerospace", "geopolitical"):
        "Geopolitical tension → accelerated defense procurement cycle. "
        "Benefits missile systems, electronic warfare, naval platforms ({tickers}).",

    # Infrastructure & Capex
    ("Infrastructure & Capex", "pmi_strong"):
        "Manufacturing PMI expansion → capex cycle intact for {tickers}. "
        "Order book visibility improving across power, T&D, construction.",
    ("Infrastructure & Capex", "pmi_weak"):
        "Weak PMI signals capex cycle softening — watchlist pressure for {tickers}. "
        "Government spending may compensate if private capex pauses.",
    ("Infrastructure & Capex", "budget"):
        "Budget capex allocation → direct order book impact for {tickers}. "
        "Infrastructure EPC benefits from multi-year visibility.",
    ("Infrastructure & Capex", "rate_cut"):
        "Rate cut → lower project financing costs for capex-heavy names ({tickers}). "
        "Positive for leveraged infrastructure companies.",

    # Metals & Mining
    ("Metals & Mining", "steel_price"):
        "Steel price movement impacts margins for producers ({tickers}). "
        "Demand from infra + auto is the key driver of domestic realizations.",
    ("Metals & Mining", "copper_price"):
        "Copper price movement affects miners and downstream users ({tickers}). "
        "EV + renewable transition is structural demand tailwind for copper.",
    ("Metals & Mining", "china_stimulus"):
        "China stimulus → commodity demand expectations rise ({tickers}). "
        "China consumes ~50% of global metals — stimulus = price support.",
    ("Metals & Mining", "crude_up"):
        "Rising crude → higher energy costs for metal smelters ({tickers}). "
        "Power-intensive aluminium and zinc most affected.",

    # Pharma
    ("Pharma & Healthcare", "pharma_fda"):
        "FDA approval / ANDA clearance → revenue catalyst for {tickers}. "
        "US generics market entry adds recurring revenue stream.",
    ("Pharma & Healthcare", "rupee_weak"):
        "Weak rupee benefits pharma exporters ({tickers}). "
        "~40-60% revenue is USD-denominated for large Indian pharma.",

    # Auto & EV
    ("Auto & EV", "ev_policy"):
        "EV policy support → demand catalyst for electric vehicle makers ({tickers}). "
        "Subsidy + charging infra buildout accelerates adoption curve.",
    ("Auto & EV", "rate_cut"):
        "Rate cut → auto loan EMIs drop → demand stimulus for {tickers}. "
        "Two-wheeler segment most sensitive to rate changes.",
    ("Auto & EV", "monsoon"):
        "Good monsoon → rural income boost → tractor + two-wheeler demand ({tickers}). "
        "M&M and Bajaj Auto are primary rural consumption proxies.",
    ("Auto & EV", "pli_scheme"):
        "PLI scheme benefits for auto components / EV manufacturing ({tickers}). "
        "Localization incentive strengthens domestic supply chain.",

    # FMCG
    ("FMCG & Consumption", "monsoon"):
        "Monsoon outcome drives rural consumption outlook ({tickers}). "
        "Good rains → agri income → discretionary FMCG spend in Tier 3+ cities.",
    ("FMCG & Consumption", "inflation_high"):
        "High inflation → input cost pressure on FMCG margins ({tickers}). "
        "Pricing power determines who can pass through.",
    ("FMCG & Consumption", "inflation_low"):
        "Easing inflation → margin relief for FMCG ({tickers}). "
        "Volume recovery more important than price-led growth.",

    # Real Estate
    ("Real Estate & Construction", "rate_cut"):
        "Rate cut → home loan EMI reduction → demand boost for {tickers}. "
        "Affordable housing segment most rate-sensitive.",
    ("Real Estate & Construction", "realty_demand"):
        "Housing demand / registration data signals market health for {tickers}. "
        "Sustained demand at current prices supports developer valuations.",

    # Telecom
    ("Telecom & Digital", "5g_telecom"):
        "5G / telecom development benefits infrastructure + equipment companies ({tickers}). "
        "ARPU expansion and fiber rollout are key revenue drivers.",

    # Railway
    ("Railway & Logistics", "budget"):
        "Budget railway allocation → order pipeline for rolling stock and infra ({tickers}). "
        "Vande Bharat + station modernization are multi-year themes.",
    ("Railway & Logistics", "pmi_strong"):
        "Strong PMI → freight volumes rise → benefits container logistics ({tickers}). "
        "DFC commissioning amplifies the volume-to-profit conversion.",

    # Agriculture
    ("Agriculture & Fertilizers", "monsoon"):
        "Monsoon forecast impacts agri input demand ({tickers}). "
        "Good rains = higher pesticide + fertilizer consumption in kharif season.",
    ("Agriculture & Fertilizers", "crude_up"):
        "Rising crude → higher input costs for agrochemicals ({tickers}). "
        "Petrochemical-derived intermediates become expensive.",

    # Semiconductor
    ("Semiconductor & Electronics", "pli_scheme"):
        "PLI scheme extends to electronics manufacturing ({tickers}). "
        "Incentivizes domestic production of PCBs, modules, components.",

    # Cross-sector themes
    ("Banking & Financials", "gold_up"):
        "Rising gold benefits gold-backed lenders ({tickers}). "
        "AUM growth + lower LTV risk on existing loan book.",
    ("Metals & Mining", "rupee_weak"):
        "Weak rupee makes Indian metal exports more competitive ({tickers}). "
        "Domestic prices also firm up on import parity adjustment.",
}


# ---------------------------------------------------------------------------
# Impact scoring
# ---------------------------------------------------------------------------

_EVENT_BONUS_MAP = {
    # +2 events
    "merge|acqui|stake sale|demerger|takeover": 2,
    r"\bsebi\b|rbi circular|usfda|drdo approval|nclt|debt default|\bfir\b|court order": 2,
    r"\brbi rate\b|repo rate|budget|gdp data|\binflation\b|cpi data": 2,
    r"\bfed\b|us fed|china gdp|opec|oil cartel|EM flows": 2,
    "ipo listing|ipo allotment|grey market premium|gmp": 2,
    # +1 events
    r"q[1-4] result|quarterly|earnings|revenue guidance|analyst day": 1,
    "dividend|buyback|stock split|rights issue": 1,
    r"\bpli\b|government tender|procurement|sector policy": 1,
    "rating upgrade|rating downgrade|analyst target|price target": 1,
    "promoter buy|promoter sell|bulk deal|block deal|insider buy": 1,
}

_BULLISH_KW = frozenset(["beat", "surge", "rise", "gain", "rally", "record", "strong",
                          "raised", "upgrade", "positive", "win", "approve", "award"])
_BEARISH_KW = frozenset(["miss", "fall", "drop", "cut", "weak", "negative", "loss",
                          "concern", "default", "fraud", "suspend", "halt", "downgrade"])
_SOCIAL_SOURCES = ("telegram", "twitter", "reddit", "whatsapp", "instagram")
_AUTHORITATIVE_SOURCES = ("bseindia.com", "nseindia.com", "sebi.gov.in", "rbi.org.in")
_MACRO_SECTORS  = ("MACRO", "ALL", "MULTI", "GLOBAL")


def _score_news_impact(headline: str, source: str, sector: str, causal_chain: str) -> int:
    """
    Score news impact 1-10 per docs/news_methodology.md formula.
    base(3) + event_type_bonus + sentiment_bonus + breadth_bonus - reliability_penalty
    """
    score = 3

    hl_lower = (headline or "").lower()
    bonus = 0
    for pattern, val in _EVENT_BONUS_MAP.items():
        if re.search(pattern, hl_lower):
            bonus = max(bonus, val)
    score += bonus

    words = set(hl_lower.split())
    if words & _BULLISH_KW or words & _BEARISH_KW:
        score += 1

    if (sector or "").upper() in _MACRO_SECTORS:
        score += 1

    src_lower = (source or "").lower()
    if any(s in src_lower for s in _SOCIAL_SOURCES):
        score -= 1
    if any(s in src_lower for s in _AUTHORITATIVE_SOURCES):
        score += 1

    return max(1, min(10, score))


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

def _generate_signal(
    headline: str,
    summary: str,
    source: str,
    date_str: str,
    tickers: list[str],
    themes: list[str],
    ticker_set: dict[str, str],
    sector_tickers: dict[str, list[str]],
) -> list[dict]:
    """
    Generate one or more news signals from a headline.

    For each (sector, theme) combination found, try to find a causal template.
    If a template exists → generate a fully-formed signal.
    If no template → flag as needs_review for Sunday Claude processing.

    Returns a list of signal dicts ready for merging into news_signals.json.
    """
    signals = []

    # Determine affected sectors (from extracted tickers)
    affected_sectors: dict[str, list[str]] = {}
    for t in tickers:
        sector = ticker_set.get(t, "")
        if sector:
            affected_sectors.setdefault(sector, []).append(t)

    # If we matched themes but no specific tickers, fan out ONLY for
    # genuinely macro themes (crude, rates, FII flows, etc.).
    # Company-specific themes (earnings, deals, FDA) are noise without
    # a ticker match — "some company's profit rose" doesn't tell us
    # which sector to apply it to.
    _MACRO_THEMES = {
        "crude_up", "crude_down", "rate_cut", "rate_hike",
        "fii_outflow", "fii_inflow", "rupee_weak", "rupee_strong",
        "gold_up", "gold_down", "budget", "geopolitical",
        "inflation_high", "inflation_low", "pmi_strong", "pmi_weak",
        "monsoon", "china_stimulus", "us_fed", "ev_policy",
        "pli_scheme", "solar_wind", "steel_price", "copper_price",
    }
    if themes and not tickers:
        macro_themes = [t for t in themes if t in _MACRO_THEMES]
        for theme in macro_themes:
            for (tmpl_sector, tmpl_theme), _ in CAUSAL_TEMPLATES.items():
                if tmpl_theme == theme and tmpl_sector not in affected_sectors:
                    sector_t = sector_tickers.get(tmpl_sector, [])
                    if sector_t:
                        affected_sectors[tmpl_sector] = sector_t[:4]

    # Generate signals for each sector × theme combination.
    # Only produce a signal when (sector, theme) has a template OR the
    # ticker was explicitly mentioned in the headline (worth reviewing).
    for sector, sector_specific_tickers in affected_sectors.items():
        for theme in themes:
            key = f"{_slugify(sector)}-{theme}"
            ticker_str = ", ".join(sector_specific_tickers[:5])

            template = CAUSAL_TEMPLATES.get((sector, theme))
            if template:
                causal_chain = template.format(tickers=ticker_str)
                needs_review = False
            else:
                # No template — only flag for review if tickers were
                # directly mentioned (not sector-level fan-out)
                has_direct_ticker = any(t in tickers for t in sector_specific_tickers)
                if not has_direct_ticker:
                    continue  # skip — no template + no direct ticker = noise
                causal_chain = None
                needs_review = True

            signals.append({
                "key": key,
                "sector": sector,
                "theme": theme,
                "headline": headline,
                "summary": summary,
                "source": source,
                "date": date_str,
                "causal_chain": causal_chain,
                "affected_tickers": sector_specific_tickers[:5],
                "needs_review": needs_review,
                "impact_score": _score_news_impact(headline, source, sector, causal_chain or ""),
            })

    # Headlines with tickers but no matched themes → needs_review
    if tickers and not themes:
        for sector, sector_specific_tickers in affected_sectors.items():
            h = _hash_headline(headline)[:8]
            signals.append({
                "key": f"unmatched-{h}",
                "sector": sector,
                "theme": "unmatched",
                "headline": headline,
                "summary": summary,
                "source": source,
                "date": date_str,
                "causal_chain": None,
                "affected_tickers": sector_specific_tickers[:5],
                "needs_review": True,
                "impact_score": _score_news_impact(headline, source, sector, ""),
            })

    return signals


def _slugify(text: str) -> str:
    """Convert 'Oil & Gas / Energy' → 'oil-gas-energy'."""
    text = text.lower()
    text = re.sub(r'[&/]', ' ', text)
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'\s+', '-', text.strip())
    return text


# ---------------------------------------------------------------------------
# Main scrape function
# ---------------------------------------------------------------------------

_SIGNALS_FILE = _DATA_DIR / "news_signals.json"


def _load_signals() -> dict:
    """Load existing news signals knowledgebase."""
    if _SIGNALS_FILE.exists():
        try:
            return json.loads(_SIGNALS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_updated": None, "signals": {}}


def _save_signals(data: dict) -> None:
    """Persist news signals knowledgebase."""
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    _SIGNALS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# Noise patterns — generic stock trackers, not actionable news.
# These appear in ET Markets RSS daily, ~30-40% of entries.
_NOISE_PATTERNS = [
    "share price live updates",
    "stock price live",
    "trading statistics",
    "current trading stat",
    "shows strong monthly",
    "shows promising monthly",
    "shows impressive monthly",
    "achieves notable monthly",
    "delivers robust monthly",
    "monthly return",
    "weekly return",
    "closing above vwap",
    "closing below vwap",
    "crossing above vwap",
    "crossing below vwap",
    "close crossing",
    "stocks to watch",      # generic listicles
    "top gainers",
    "top losers",
    "most active",
    "52-week high",
    "52-week low",
    "technical analysis",   # charting commentary, not news
]


def _is_noise(headline: str) -> bool:
    """Return True if headline is a generic price tracker with no news value."""
    hl = headline.lower()
    return any(pat in hl for pat in _NOISE_PATTERNS)


# ---------------------------------------------------------------------------
# MoneyControl HTML scraper (RSS is dead)
# ---------------------------------------------------------------------------

def _scrape_moneycontrol() -> list[dict]:
    """
    Scrape MoneyControl markets + stocks pages for article headlines and URLs.

    Returns a list of dicts with keys: title, link, source, date.
    We don't get summaries from the listing page — article body is fetched
    separately for matched entries.
    """
    entries = []
    seen_urls: set[str] = set()

    for page_url in MC_PAGES:
        try:
            resp = requests.get(page_url, headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"News scraper: MoneyControl page failed: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # MoneyControl article links live in <a> tags with /news/ in the href
        # and a numeric article ID at the end.
        for a_tag in soup.select('a[href*="/news/"]'):
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")

            # Filter: real articles have a numeric ID at the end,
            # a meaningful title, and haven't been seen yet
            if (not title or len(title) < 20 or href in seen_urls
                    or not any(c.isdigit() for c in href.split("/")[-1])):
                continue

            # Make URL absolute if needed
            if href.startswith("/"):
                href = f"https://www.moneycontrol.com{href}"

            seen_urls.add(href)
            entries.append({
                "title": title,
                "link": href,
                "source": "MoneyControl",
                "date": datetime.now().strftime("%Y-%m-%d"),
            })

    logger.info(f"News scraper: MoneyControl returned {len(entries)} entries")
    return entries


# ---------------------------------------------------------------------------
# Article body fetcher — gets the real content, not the teaser summary
# ---------------------------------------------------------------------------

# CSS selectors for article body extraction, tried in order.
# Each source has different HTML structure.
_ARTICLE_SELECTORS = [
    ".content_wrapper",        # MoneyControl
    "#article-main_content_text",  # MoneyControl alt
    "article",                 # ET Markets, LiveMint, generic fallback
    ".article_content",        # generic
    '[itemprop="articleBody"]', # structured data fallback
]


def _fetch_article_body(url: str) -> str:
    """
    Fetch the first ~500 words of an article's body text.

    Returns the extracted text, or empty string if fetch fails.
    We cap at ~500 words because that's enough to capture the substance
    (analyst quotes, earnings numbers, deal sizes) without storing
    entire articles.
    """
    if not url:
        return ""

    try:
        resp = requests.get(url, headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
    except Exception:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    # Try each selector until we find article content
    text = ""
    for sel in _ARTICLE_SELECTORS:
        body = soup.select_one(sel)
        if body:
            # Get clean text, strip boilerplate
            raw = body.get_text(separator=" ", strip=True)
            # Remove common junk phrases
            for junk in ["Also Read:", "Also read:", "Did our AI summary help?",
                         "Subscribe to", "Get updates on", "Gift this article"]:
                raw = raw.replace(junk, "")
            text = raw.strip()
            break

    if not text:
        # Last resort: concatenate all <p> tags with enough content
        paras = soup.find_all("p")
        text = " ".join(
            p.get_text(strip=True) for p in paras
            if len(p.get_text(strip=True)) > 30
        )

    # Cap at ~500 words to keep storage bounded
    words = text.split()
    if len(words) > 500:
        text = " ".join(words[:500])

    return text


def scrape() -> int:
    """
    Fetch all sources, diff against seen headlines, extract signals.
    For matched entries, fetch the full article body for richer context.

    Returns count of new signals added/updated.
    """
    state = _load_state()
    seen = set(state.get("seen_hashes", []))
    ticker_set, name_map, sector_tickers = _load_universe()

    if not ticker_set:
        logger.warning("News scraper: no stock_universe.json, skipping")
        return 0

    signals_db = _load_signals()
    new_count = 0

    # Phase 1: Collect raw entries from all sources into a uniform list.
    # Each entry has: title, link, source, date, rss_summary (may be empty).
    raw_entries: list[dict] = []

    # 1a. RSS feeds (ET Markets, LiveMint)
    for feed_info in RSS_FEEDS:
        feed_name = feed_info["name"]
        try:
            feed = feedparser.parse(feed_info["url"])
        except Exception as e:
            logger.warning(f"News scraper: failed to fetch {feed_name}: {e}")
            continue

        if not feed.entries:
            logger.info(f"News scraper: {feed_name} returned 0 entries")
            continue

        logger.info(f"News scraper: {feed_name} returned {len(feed.entries)} entries")

        for entry in feed.entries:
            title = entry.get("title", "").strip()
            if not title:
                continue

            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                try:
                    date_str = datetime(*published[:3]).strftime("%Y-%m-%d")
                except (TypeError, ValueError):
                    date_str = datetime.now().strftime("%Y-%m-%d")
            else:
                date_str = datetime.now().strftime("%Y-%m-%d")

            raw_entries.append({
                "title": title,
                "link": entry.get("link", ""),
                "source": feed_name,
                "date": date_str,
                "rss_summary": _strip_html(entry.get("summary", "")),
            })

    # 1b. MoneyControl (HTML scrape — RSS is dead)
    for mc_entry in _scrape_moneycontrol():
        raw_entries.append({
            "title": mc_entry["title"],
            "link": mc_entry["link"],
            "source": mc_entry["source"],
            "date": mc_entry["date"],
            "rss_summary": "",  # MC listing pages don't have summaries
        })

    # Phase 2: Dedup, filter noise, extract tickers/themes, fetch article
    # bodies for matched entries, generate signals.
    for entry in raw_entries:
        title = entry["title"]

        h = _hash_headline(title)
        if h in seen:
            continue
        seen.add(h)

        if _is_noise(title):
            continue

        # First pass: extract tickers/themes from headline + RSS summary.
        # This decides whether the entry is worth fetching the full article.
        rss_summary = entry.get("rss_summary", "")
        quick_text = f"{title} {rss_summary}"
        tickers = _extract_tickers(quick_text, ticker_set)
        themes = _extract_themes(quick_text)

        if not tickers and not themes:
            continue

        # This entry matched — fetch the full article body for real content.
        # The article body replaces the thin RSS summary/teaser.
        article_url = entry.get("link", "")
        article_body = _fetch_article_body(article_url)

        # Use article body as the summary (it's the real content).
        # Fall back to RSS summary if article fetch failed.
        summary = article_body if article_body else rss_summary

        # Second pass: re-extract tickers/themes from headline + full article.
        # The article body often mentions companies the headline doesn't.
        if article_body:
            full_text = f"{title} {article_body}"
            tickers = _extract_tickers(full_text, ticker_set)
            themes = _extract_themes(full_text)

        new_signals = _generate_signal(
            headline=title,
            summary=summary,
            source=entry["source"],
            date_str=entry["date"],
            tickers=tickers,
            themes=themes,
            ticker_set=ticker_set,
            sector_tickers=sector_tickers,
        )

        for sig in new_signals:
            key = sig.pop("key")
            signals_db["signals"][key] = sig
            new_count += 1

    # Persist state and signals
    state["seen_hashes"] = sorted(seen)
    _save_state(state)
    _save_signals(signals_db)

    logger.info(f"News scraper: {new_count} new signals written")
    return new_count


def get_needs_review(signals: list[dict]) -> list[dict]:
    """Return signals flagged for Sunday Claude review, filtered to impact >= 6."""
    return [s for s in signals if s.get("needs_review") and s.get("impact_score", 0) >= 6]


def update_reviewed_signal(key: str, causal_chain: str | None, delete: bool = False) -> None:
    """
    Update a needs_review signal after Claude review.
    If delete=True, removes the signal (it was noise).
    If causal_chain is provided, saves it and clears needs_review.
    """
    signals_db = _load_signals()
    if delete:
        signals_db["signals"].pop(key, None)
    elif key in signals_db["signals"]:
        signals_db["signals"][key]["causal_chain"] = causal_chain
        signals_db["signals"][key]["needs_review"] = False
    _save_signals(signals_db)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    n = scrape()
    print(f"\n{n} new signals processed.")

    signals_db = _load_signals()
    total = len(signals_db.get("signals", {}))
    needs_review = len([s for s in signals_db.get("signals", {}).values() if s.get("needs_review")])
    print(f"Total signals in knowledgebase: {total}")
    print(f"Needs review (Sunday): {needs_review}")

    if total > 0:
        print("\nSample signals:")
        for key, sig in list(signals_db["signals"].items())[:5]:
            review_flag = " [NEEDS REVIEW]" if sig.get("needs_review") else ""
            print(f"  {key}: {sig['headline'][:60]}...{review_flag}")
            if sig.get("causal_chain"):
                print(f"    → {sig['causal_chain'][:80]}...")
            print(f"    Tickers: {sig.get('affected_tickers', [])}")
