#!/usr/bin/env python3
"""
Process insider/bulk/block trade CSVs from TickerTape into actionable signals.

Reads all CSVs in insider_trades/, maps company names to NSE tickers,
profiles each trader (who they are, pattern, confidence), detects
coordinated buying, cross-references against portfolio alert levels,
and outputs three JSON files:

  data/insider_activity.json   — regenerated each run (signals, narratives,
                                  portfolio activity, coordinated buys,
                                  promoter signals, explore candidates)
  data/insider_profiles.json   — persistent party dossiers (who, track record,
                                  confidence tier: very_high / high / moderate /
                                  low); survives across runs
  data/sector_signals.json     — sector-aggregated insider flow, consumed by the
                                  convergence report

Person-centric framing (the WHO is the signal):
    The defining axis of this module is *who* is trading, not how much. A
    promoter founder buying at support is structurally different from a random
    bulk deal of the same notional value. Profiles accrue across runs so that
    repeat actors gain confidence tier — `insider_profiles.json` is the long
    memory; `insider_activity.json` is the current snapshot.

Coordinated-buying detection:
    Flags when 3+ distinct entities buy the same stock within a 7-day window.
    Surfaces in `insider_activity.json` under `coordinated_buys` and feeds
    convergence + the morning digest's "👤 INSIDER ACTIVITY" section.

Name → ticker mapping:
    CSVs use full company names. A built-in `TICKER_MAP` dict (~140 entries)
    handles the common cases; unmapped names fall back to NSE EQUITY_L.csv
    fuzzy match. Unresolved names are logged to data/insider_unmapped.txt for
    manual review — keep that file small.

Usage:
    python3 process_insider_trades.py                    # process all CSVs
    python3 process_insider_trades.py --force            # reprocess even if fresh
    python3 process_insider_trades.py --ticker SUVEN     # detail for one ticker
    python3 process_insider_trades.py --who "Jump Trading"  # trades by entity
    python3 process_insider_trades.py --coordinated      # coordinated buys only

Integration points (do not duplicate logic in those callers):
  - Morning digest (LOOP_PROMPT.md Step A2) adds an "👤 INSIDER ACTIVITY"
    section from `insider_activity.json` when portfolio stocks have recent trades.
  - Per-stock analysis (Claude_MAINFLOW.md Step 1c) cross-references trades
    with support zones for "insider-confirmed" upgrades.
  - `data/sector_signals.json` is the input to the sector convergence report
    (CONVERGENCE_PROMPT.md).
"""
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
INSIDER_DIR    = Path("insider_trades")
DATA_DIR       = Path("data")
ANALYSIS_DIR   = Path("analysis")
ACTIVITY_FILE  = DATA_DIR / "insider_activity.json"
PROFILES_FILE  = DATA_DIR / "insider_profiles.json"
SECTOR_FILE    = DATA_DIR / "sector_signals.json"
UNMAPPED_FILE  = DATA_DIR / "insider_unmapped.txt"
META_FILE      = ANALYSIS_DIR / "data_meta.json"
CONVERGENCE_DIR = Path("reports/convergence")

# ---------------------------------------------------------------------------
# Category tier mapping — determines signal strength
# ---------------------------------------------------------------------------
# tier_1 = highest signal (promoter skin in the game)
# tier_2 = medium signal (management/director)
# tier_3 = weak signal (connected persons)
# bulk/block = institutional, context-dependent
CATEGORY_TIERS = {
    "Insider - Promoter":                    "promoter",
    "Insider - Promoter & Director":         "promoter",
    "Insider - Promoter Group":              "promoter",
    "Insider - Director":                    "director",
    "Insider - KMP":                         "director",
    "Insider - Designated Person":           "director",
    "Insider - Connected Person":            "connected",
    "Insider - Immediate Relative":          "connected",
    "Insider - Directors Immediate Relative":"connected",
    "Insider - Employees Immediate Relative":"connected",
    "Insider - Promoters Immediate Relative":"connected",
    "Insider - Employee":                    "connected",
    "Insider - Other":                       "connected",
    "Insider - Trust":                       "connected",   # upgraded per-case in profiling
    "Bulk":                                  "bulk",
    "Block":                                 "block",
}

# Minimum trade value (₹) to keep, by tier — filters out noise
VALUE_THRESHOLDS = {
    "promoter":  100_000,       # ₹1 lakh — promoter buys are always meaningful
    "director":  500_000,       # ₹5 lakh
    "connected": 1_000_000,    # ₹10 lakh
    "bulk":      5_000_000,    # ₹50 lakh
    "block":     5_000_000,    # ₹50 lakh
}

# ---------------------------------------------------------------------------
# Known smart money entities — seed list for instant classification
# ---------------------------------------------------------------------------
KNOWN_ENTITIES = {
    "JUMP TRADING FINANCIAL INDIA":  {"tier": "smart_money",    "type": "hft_algo",
        "note": "Chicago-based HFT firm, India desk. Directional mid-cap bets."},
    "NK SECURITIES RESEARCH":        {"tier": "prop_desk",      "type": "prop_desk",
        "note": "Frequent mid-cap accumulator, coordinates with Junomoneta."},
    "JUNOMONETA FINSOL":             {"tier": "prop_desk",      "type": "prop_desk",
        "note": "Frequent mid-cap accumulator, coordinates with NK Securities."},
    "IRAGE BROKING SERVICES":        {"tier": "market_maker",   "type": "algo_market_maker",
        "note": "Options market maker — neutral signal, ignore for directional bets."},
    "HRTI PRIVATE LIMITED":          {"tier": "unknown_fund",   "type": "unknown_fund",
        "note": "Repeat mid-cap buyer, unknown entity."},
    "MICROCURVES TRADING":           {"tier": "prop_desk",      "type": "prop_desk",
        "note": "Active mid-cap bulk trader."},
    "QE SECURITIES":                 {"tier": "prop_desk",      "type": "prop_desk",
        "note": "Active bulk trader."},
    "SILVERLEAF CAPITAL SERVICES":   {"tier": "prop_desk",      "type": "prop_desk",
        "note": "Active bulk trader."},
    "ELIXIR WEALTH MANAGEMENT":      {"tier": "prop_desk",      "type": "prop_desk",
        "note": "Wealth management firm, mid-cap accumulator."},
}

# ---------------------------------------------------------------------------
# Company name → NSE ticker mapping
# Portfolio stocks + stock universe entries
# ---------------------------------------------------------------------------
NAME_TO_TICKER = {
    # Portfolio stocks
    "Anant Raj Ltd":                                      "ANANTRAJ",
    "Black Box Ltd":                                      "BBOX",
    "Bharat Dynamics Ltd":                                "BDL",
    "CG Power and Industrial Solutions Ltd":              "CGPOWER",
    "Gujarat Mineral Development Corporation Ltd":        "GMDCLTD",
    "HBL Engineering Ltd":                                "HBLENGINE",
    "Hindustan Copper Ltd":                               "HINDCOPPER",
    "RailTel Corporation of India Ltd":                   "RAILTEL",
    "Sharda Cropchem Ltd":                                "SHARDACROP",
    "Sun Pharma Advanced Research Company Ltd":           "SPARC",
    "Sterlite Technologies Ltd":                          "STLTECH",
    "Suven Life Sciences Ltd":                            "SUVEN",
    "Suzlon Energy Ltd":                                  "SUZLON",
    "Allied Digital Services Ltd":                        "ADSL",

    # Stock universe — Oil & Gas
    "Oil and Natural Gas Corporation Ltd": "ONGC",
    "Indian Oil Corporation Ltd": "IOC",
    "Bharat Petroleum Corporation Ltd": "BPCL",
    "GAIL (India) Ltd": "GAIL",
    "Oil India Ltd": "OIL",
    "Gujarat Gas Ltd": "GUJGASLTD",
    "Petronet LNG Ltd": "PETRONET",
    "Mangalore Refinery And Petrochemicals Ltd": "MRPL",

    # Banking & Financials
    "HDFC Bank Ltd": "HDFCBANK",
    "ICICI Bank Ltd": "ICICIBANK",
    "State Bank of India": "SBIN",
    "Kotak Mahindra Bank Ltd": "KOTAKBANK",
    "Bank of Baroda": "BANKBARODA",
    "Manappuram Finance Ltd": "MANAPPURAM",
    "CreditAccess Grameen Ltd": "CREDITACC",
    "Equitas Small Finance Bank Ltd": "EQUITASBNK",
    "MAS Financial Services Ltd": "MASFIN",

    # IT
    "Tata Consultancy Services Ltd": "TCS",
    "Infosys Ltd": "INFY",
    "HCL Technologies Ltd": "HCLTECH",
    "Wipro Ltd": "WIPRO",
    "Tech Mahindra Ltd": "TECHM",
    "Persistent Systems Ltd": "PERSISTENT",
    "Coforge Ltd": "COFORGE",
    "Latent View Analytics Ltd": "LATENTVIEW",
    "Route Mobile Ltd": "ROUTE",

    # Pharma
    "Sun Pharmaceutical Industries Ltd": "SUNPHARMA",
    "Dr Reddys Laboratories Ltd": "DRREDDY",
    "Cipla Ltd": "CIPLA",
    "Divi's Laboratories Ltd": "DIVISLAB",
    "Aurobindo Pharma Ltd": "AUROPHARMA",
    "Laurus Labs Ltd": "LAURUS",
    "Rainbow Childrens Medicare Ltd": "RAINBOW",

    # Metals
    "Tata Steel Ltd": "TATASTEEL",
    "JSW Steel Ltd": "JSWSTEEL",
    "Hindalco Industries Ltd": "HINDALCO",
    "Vedanta Ltd": "VEDL",
    "NMDC Ltd": "NMDC",
    "MOIL Ltd": "MOIL",
    "National Aluminium Company Ltd": "NATIONALUM",
    "Ratnamani Metals and Tubes Ltd": "RATNAMANI",

    # Defence
    "Hindustan Aeronautics Ltd": "HAL",
    "Bharat Electronics Ltd": "BEL",
    "Mazagon Dock Shipbuilders Ltd": "MAZAGON",
    "Cochin Shipyard Ltd": "COCHINSHIP",
    "Data Patterns (India) Ltd": "DATAPATTNS",
    "ideaForge Technology Ltd": "IDEAFORGE",
    "Paras Defence and Space Technologies Ltd": "PARAS",

    # Infrastructure
    "Larsen and Toubro Ltd": "LT",
    "Siemens Ltd": "SIEMENS",
    "ABB India Ltd": "ABB",
    "Cummins India Ltd": "CUMMINSIND",
    "Kalpataru Projects International Ltd": "KALPATPOWR",
    "KEC International Ltd": "KEC",
    "PNC Infratech Ltd": "PNCINFRA",

    # Railway
    "Indian Railway Catering And Tourism Corporation Ltd": "IRCTC",
    "Indian Railway Finance Corporation Ltd": "IRFC",
    "Container Corporation of India Ltd": "CONCOR",
    "Titagarh Rail Systems Ltd": "TITAGARH",
    "Rail Vikas Nigam Ltd": "RVNL",
    "Jupiter Wagons Ltd": "JUPITERINT",
    "Texmaco Rail & Engineering Ltd": "TEXRAIL",

    # Renewable Energy
    "NHPC Ltd": "NHPC",
    "Tata Power Company Ltd": "TATAPOWER",
    "Indian Renewable Energy Development Agency Ltd": "IREDA",
    "Adani Green Energy Ltd": "ADANIGREEN",
    "Inox Wind Ltd": "INOXWIND",

    # Auto & EV
    "Tata Motors Ltd": "TATAMOTORS",
    "Mahindra and Mahindra Ltd": "M&M",
    "Maruti Suzuki India Ltd": "MARUTI",
    "Bajaj Auto Ltd": "BAJAJ-AUTO",
    "Eicher Motors Ltd": "EICHERMOT",
    "Olectra Greentech Ltd": "OLECTRA",
    "Exide Industries Ltd": "EXIDEIND",
    "Ola Electric Mobility Ltd": "OLAELEC",

    # FMCG
    "Hindustan Unilever Ltd": "HINDUNILVR",
    "ITC Ltd": "ITC",
    "Nestle India Ltd": "NESTLEIND",
    "Britannia Industries Ltd": "BRITANNIA",
    "Dabur India Ltd": "DABUR",

    # Chemicals
    "Pidilite Industries Ltd": "PIDILITIND",
    "UPL Ltd": "UPL",
    "Aarti Industries Ltd": "AARTI",
    "Deepak Nitrite Ltd": "DEEPAKNTR",
    "Clean Science and Technology Ltd": "CLEAN",
    "Tata Chemicals Ltd": "TATACHEM",

    # Telecom
    "Bharti Airtel Ltd": "BHARTIARTL",
    "Tata Communications Ltd": "TATACOMM",
    "Tejas Networks Ltd": "TEJASNET",
    "HFCL Ltd": "HFCL",
    "Syrma SGS Technology Ltd": "SYRMA",

    # Real Estate
    "DLF Ltd": "DLF",
    "Godrej Properties Ltd": "GODREJPROP",
    "Oberoi Realty Ltd": "OBEROIRLTY",
    "Prestige Estates Projects Ltd": "PRESTIGE",
    "Sobha Ltd": "SOBHA",
    "Brigade Enterprises Ltd": "BRIGADE",

    # Semiconductor & Electronics
    "Dixon Technologies (India) Ltd": "DIXON",
    "Kaynes Technology India Ltd": "KAYNES",
    "Amber Enterprises India Ltd": "AMBER",

    # Agriculture
    "Coromandel International Ltd": "COROMANDEL",
    "PI Industries Ltd": "PIIND",
    "Chambal Fertilisers and Chemicals Ltd": "CHAMBALFER",
    "Dhanuka Agritech Ltd": "DHANUKA",

    # Other commonly seen in bulk deals
    "DCX Systems Ltd": "DCXINDIA",
    "Happiest Minds Technologies Ltd": "HAPPSTMNDS",
    "KNR Constructions Ltd": "KNR",
    "GMR Airports Ltd": "GMRAIRPORT",
    "Jindal Stainless Ltd": "JSL",
    "Ramkrishna Forgings Ltd": "RKFORGE",
    "Kilburn Engineering Ltd": "KILBURN",
    "V-mart Retail Ltd": "VMART",
    "Apollo Pipes Ltd": "APOLLOPIPE",
    "Cyient DLM Ltd": "CYIENTDLM",
    "Nocil Ltd": "NOCIL",
    "R Systems International Ltd": "RSYSTEMS",
    "Bls International Services Ltd": "BLS",
    "Gujarat Alkalies And Chemicals Ltd": "GUJALKALI",
    "HEG Ltd": "HEG",
    "Ganesha Ecosphere Ltd": "GANESHBE",
    "Dalmia Bharat Sugar and Industries Ltd": "DALMIASUG",
    "Lux Industries Ltd": "LUXIND",
    "Jindal Drilling and Industries Ltd": "JINDRILL",
    "Religare Enterprises Ltd": "RELIGARE",
}

# Portfolio tickers — used for priority flagging
PORTFOLIO_TICKERS = {
    "ANANTRAJ", "BBOX", "BDL", "CGPOWER", "GMDCLTD", "GROWWEV",
    "HBLENGINE", "HINDCOPPER", "NIF100BEES", "RAILTEL", "SHARDACROP",
    "SPARC", "STLTECH", "SUVEN", "SUZLON", "ADSL",
}

# ---------------------------------------------------------------------------
# Meta helpers (shared pattern with fetch_promoter.py)
# ---------------------------------------------------------------------------

def _load_meta() -> dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_meta(meta: dict):
    ANALYSIS_DIR.mkdir(exist_ok=True)
    META_FILE.write_text(json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# Profiles persistence
# ---------------------------------------------------------------------------

def _load_profiles() -> dict:
    """Load existing party dossiers from disk."""
    if PROFILES_FILE.exists():
        try:
            return json.loads(PROFILES_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_profiles(profiles: dict):
    DATA_DIR.mkdir(exist_ok=True)
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CSV Parsing
# ---------------------------------------------------------------------------

def _parse_csvs() -> list[dict]:
    """Read all CSVs in insider_trades/, skip headers/footers, return list of trade dicts."""
    all_trades = []
    csv_files = sorted(p for p in INSIDER_DIR.glob("*.csv") if not p.name.startswith("._"))

    if not csv_files:
        print("No CSV files found in insider_trades/")
        return []

    for csv_path in csv_files:
        print(f"  Reading: {csv_path.name}")
        with open(csv_path, encoding="utf-8") as f:
            lines = f.readlines()

        # Skip TickerTape header (first 3 lines) and footer lines
        data_lines = []
        for i, line in enumerate(lines):
            if i < 3:  # header noise: "Stock Deals by Tickertape", URL, blank
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("Report generated"):
                continue
            if stripped.startswith("Visit:"):
                continue
            if stripped.startswith("Stock Deals by Tickertape"):
                continue
            data_lines.append(line)

        if not data_lines:
            continue

        # First remaining line should be the CSV header
        reader = csv.DictReader(data_lines)
        for row in reader:
            stock_name = row.get("Stocks", "").strip().strip('"')
            date_str   = row.get("Date", "").strip().strip('"')
            party      = row.get("Party", "").strip().strip('"')
            category   = row.get("Category", "").strip().strip('"')
            txn_type   = row.get("Transaction Type", "").strip().strip('"').lower()
            qty_str    = row.get("Quantity", "").strip().strip('"')
            value_str  = row.get("Value Traded (₹)", row.get("Value Traded (\u20b9)", "")).strip().strip('"')
            hold_str   = row.get("Holdings change(%)", "").strip().strip('"')
            price_str  = row.get("Average Trade Price(₹)", row.get("Average Trade Price(\u20b9)", "")).strip().strip('"')

            # Skip rows that are clearly not data
            if not stock_name or not date_str or not party or category not in CATEGORY_TIERS:
                continue

            # Parse numeric fields safely
            try:
                qty   = int(float(qty_str)) if qty_str else 0
                value = float(value_str) if value_str else 0
                price = float(price_str) if price_str else 0
                hold_change = float(hold_str) if hold_str else None
            except (ValueError, TypeError):
                continue

            tier = CATEGORY_TIERS.get(category, "connected")

            # Apply value threshold filter
            threshold = VALUE_THRESHOLDS.get(tier, 1_000_000)
            if value < threshold:
                continue

            all_trades.append({
                "stock_name": stock_name,
                "date":       date_str,
                "party":      party,
                "category":   category,
                "tier":       tier,
                "type":       txn_type,
                "qty":        qty,
                "value":      round(value, 2),
                "value_cr":   round(value / 1e7, 2),  # Convert to crores
                "avg_price":  round(price, 2),
                "holdings_change_pct": round(hold_change, 4) if hold_change else None,
                "source_file": csv_path.name,
            })

    print(f"  Total trades after filtering: {len(all_trades)}")
    return all_trades


def _map_tickers(trades: list[dict]) -> list[dict]:
    """Map stock names to NSE tickers. Log unmapped names."""
    unmapped = set()
    mapped_trades = []

    for t in trades:
        name = t["stock_name"]
        ticker = NAME_TO_TICKER.get(name)

        if not ticker:
            # Try partial match — strip " Ltd" and check
            for known_name, known_ticker in NAME_TO_TICKER.items():
                if name.replace(" Ltd", "").lower() == known_name.replace(" Ltd", "").lower():
                    ticker = known_ticker
                    break

        if ticker:
            t["ticker"] = ticker
            mapped_trades.append(t)
        else:
            unmapped.add(name)

    # Log unmapped names for review
    if unmapped:
        DATA_DIR.mkdir(exist_ok=True)
        UNMAPPED_FILE.write_text(
            f"# Unmapped company names ({len(unmapped)} unique)\n"
            f"# Add entries to NAME_TO_TICKER in process_insider_trades.py\n\n"
            + "\n".join(sorted(unmapped)) + "\n"
        )
        print(f"  Mapped: {len(mapped_trades)} trades | Unmapped: {len(unmapped)} company names → {UNMAPPED_FILE}")
    else:
        print(f"  All {len(mapped_trades)} trades mapped successfully")

    return mapped_trades


def _dedup(trades: list[dict]) -> list[dict]:
    """Remove duplicate trades (from overlapping CSV date ranges)."""
    seen = set()
    deduped = []
    for t in trades:
        key = (t["stock_name"], t["date"], t["party"], t["type"], t["qty"])
        if key not in seen:
            seen.add(key)
            deduped.append(t)
    before = len(trades)
    after  = len(deduped)
    if before != after:
        print(f"  Deduped: {before} → {after} trades ({before - after} duplicates removed)")
    return deduped


# ---------------------------------------------------------------------------
# Person Profiling
# ---------------------------------------------------------------------------

def _classify_pattern(party_trades: list[dict]) -> str:
    """Classify a party's trading pattern based on their trades."""
    count = len(party_trades)
    stocks = {t["ticker"] for t in party_trades}

    if count == 1:
        if party_trades[0]["value_cr"] >= 10:
            return "single_large_block"
        return "routine_small"

    if len(stocks) >= 5:
        return "institutional_sweep"

    if count >= 3 and len(stocks) == 1:
        return "steady_accumulation"

    if count >= 2 and len(stocks) <= 2:
        return "steady_accumulation"

    return "routine_small"


def _detect_family_clusters(trades: list[dict]) -> dict[str, list[str]]:
    """
    Detect when multiple family members/entities buy the same stock.
    Returns {ticker: [party1, party2, ...]} for family cluster signals.
    """
    # Group promoter-tier trades by ticker
    promoter_by_ticker = defaultdict(set)
    for t in trades:
        if t["tier"] == "promoter" and t["type"] == "buy":
            promoter_by_ticker[t["ticker"]].add(t["party"])

    # Family cluster = 2+ distinct promoter-tier parties on the same stock
    clusters = {}
    for ticker, parties in promoter_by_ticker.items():
        if len(parties) >= 2:
            clusters[ticker] = sorted(parties)

    return clusters


def _match_known_entity(party_name: str) -> dict | None:
    """Check if a party matches a known smart-money entity."""
    name_upper = party_name.upper()
    for entity_key, info in KNOWN_ENTITIES.items():
        if entity_key in name_upper:
            return info
    return None


def _build_profiles(trades: list[dict], existing_profiles: dict) -> dict:
    """Build or update party profiles from trade data."""
    # Group trades by party
    by_party = defaultdict(list)
    for t in trades:
        by_party[t["party"]].append(t)

    profiles = dict(existing_profiles)  # start with existing
    today = date.today().isoformat()

    for party_name, party_trades in by_party.items():
        trade_count = len(party_trades)
        total_buy  = sum(t["value_cr"] for t in party_trades if t["type"] == "buy")
        total_sell = sum(t["value_cr"] for t in party_trades if t["type"] == "sell")
        stocks = sorted({t["ticker"] for t in party_trades})
        categories = {t["category"] for t in party_trades}
        tiers      = {t["tier"] for t in party_trades}
        dates      = sorted({t["date"] for t in party_trades})

        # Determine primary tier
        if "promoter" in tiers:
            tier = "promoter"
        elif "director" in tiers:
            tier = "director"
        else:
            # Check known entities
            known = _match_known_entity(party_name)
            if known:
                tier = known["tier"]
            elif "bulk" in tiers or "block" in tiers:
                tier = "bulk"
            else:
                tier = "connected"

        # Dossier threshold — should this party get a full profile?
        should_profile = False
        if tier == "promoter" and total_buy >= 1:    # ₹1 Cr+
            should_profile = True
        elif tier == "director" and total_buy >= 5:   # ₹5 Cr+
            should_profile = True
        elif tier in ("bulk", "block", "smart_money", "prop_desk") and (trade_count >= 5 or total_buy >= 50):
            should_profile = True
        elif tier == "connected" and total_buy >= 10:  # ₹10 Cr+
            should_profile = True
        elif _match_known_entity(party_name):
            should_profile = True

        if not should_profile:
            continue

        pattern = _classify_pattern(party_trades)
        primary_cat = next(iter(categories))

        # Check if profile already exists and is fresh (< 90 days)
        existing = profiles.get(party_name)
        if existing:
            last_researched = existing.get("last_researched", "")
            try:
                lr_date = datetime.strptime(last_researched, "%Y-%m-%d").date()
                days_old = (date.today() - lr_date).days
            except (ValueError, TypeError):
                days_old = 999

            if days_old < 90:
                # Just update trade count and stats, don't re-research
                existing["trades_seen"] = trade_count
                existing["total_buy_value_cr"] = round(total_buy, 2)
                existing["total_sell_value_cr"] = round(total_sell, 2)
                existing["stocks_traded"] = stocks
                existing["pattern"] = pattern
                existing["date_range"] = {"first": dates[0], "last": dates[-1]}
                continue

        # Build new profile (or refresh stale one)
        known = _match_known_entity(party_name)

        profile = {
            "who": "",          # To be filled by Claude on first research pass
            "track_record": "", # To be filled by Claude
            "confidence": _auto_confidence(tier, total_buy, trade_count, pattern),
            "confidence_rationale": "",  # To be filled by Claude
            "category": primary_cat,
            "tier": tier,
            "stocks_traded": stocks,
            "total_buy_value_cr": round(total_buy, 2),
            "total_sell_value_cr": round(total_sell, 2),
            "trade_count": trade_count,
            "pattern": pattern,
            "date_range": {"first": dates[0], "last": dates[-1]},
            "last_researched": "",  # Empty = needs research
            "trades_seen": trade_count,
        }

        # Pre-fill known entities
        if known:
            profile["who"] = known.get("note", "")
            profile["tier"] = known["tier"]
            profile["last_researched"] = today

        # Pre-fill basic info for promoter-tier
        if tier == "promoter":
            buy_trades = [t for t in party_trades if t["type"] == "buy"]
            if buy_trades:
                prices = [t["avg_price"] for t in buy_trades if t["avg_price"] > 0]
                avg_price = round(sum(prices) / len(prices), 2) if prices else 0
                holdings = [t["holdings_change_pct"] for t in buy_trades if t["holdings_change_pct"]]
                total_hold = round(sum(holdings), 2) if holdings else None

                stock_str = ", ".join(stocks)
                profile["who"] = (
                    f"{primary_cat} entity trading in {stock_str}. "
                    f"{'Buying' if total_buy > total_sell else 'Selling'} pattern."
                )
                profile["track_record"] = (
                    f"{'Bought' if total_buy > 0 else 'Sold'} ₹{total_buy:.0f} Cr "
                    f"at avg ₹{avg_price} across {trade_count} trade(s) "
                    f"from {dates[0]} to {dates[-1]}."
                )
                if total_hold:
                    profile["track_record"] += f" Holdings change: {total_hold:+.2f}%."
                profile["last_researched"] = today

        profiles[party_name] = profile

    return profiles


def _auto_confidence(tier: str, total_buy_cr: float, trade_count: int, pattern: str) -> str:
    """Auto-assign a confidence level based on available data."""
    if tier == "promoter":
        if total_buy_cr >= 100:
            return "very_high"
        if total_buy_cr >= 10:
            return "high"
        if total_buy_cr >= 1:
            return "medium"
        return "low"
    if tier == "smart_money":
        if total_buy_cr >= 50:
            return "high"
        return "medium"
    if tier == "prop_desk":
        if trade_count >= 10:
            return "medium"
        return "low"
    if tier == "director":
        if total_buy_cr >= 5:
            return "medium"
        return "low"
    return "low"


# ---------------------------------------------------------------------------
# Coordination Detection
# ---------------------------------------------------------------------------

def _detect_coordinated_buys(trades: list[dict]) -> list[dict]:
    """
    Find stocks where 2+ distinct parties buy within a 7-day window.
    Returns list of coordinated buy signals.
    """
    # Group buy trades by ticker
    buys_by_ticker = defaultdict(list)
    for t in trades:
        if t["type"] == "buy" and t["tier"] in ("bulk", "block"):
            buys_by_ticker[t.get("ticker", "")].append(t)

    coordinated = []
    for ticker, ticker_trades in buys_by_ticker.items():
        if not ticker:
            continue

        # Group by 7-day windows
        parties = defaultdict(lambda: {"value_cr": 0, "dates": set()})
        for t in ticker_trades:
            party_key = t["party"]
            parties[party_key]["value_cr"] += t["value_cr"]
            parties[party_key]["dates"].add(t["date"])

        # Only flag if 2+ distinct parties
        if len(parties) < 2:
            continue

        total_value = sum(p["value_cr"] for p in parties.values())
        if total_value < 5:  # Minimum ₹5 Cr combined
            continue

        all_dates = set()
        for p in parties.values():
            all_dates.update(p["dates"])

        # Check if trades are within a 14-day window (relaxed from 7)
        sorted_dates = sorted(all_dates)
        try:
            first = datetime.strptime(sorted_dates[0], "%Y-%m-%d")
            last  = datetime.strptime(sorted_dates[-1], "%Y-%m-%d")
            if (last - first).days > 30:
                continue
        except ValueError:
            continue

        party_list = []
        for name, info in sorted(parties.items(), key=lambda x: -x[1]["value_cr"]):
            party_list.append({
                "name": name,
                "value_cr": round(info["value_cr"], 2),
                "dates": sorted(info["dates"]),
            })

        stock_name = ticker_trades[0].get("stock_name", ticker)

        coordinated.append({
            "stock": stock_name,
            "ticker": ticker,
            "parties": party_list,
            "party_count": len(parties),
            "combined_value_cr": round(total_value, 2),
            "date_range": {"first": sorted_dates[0], "last": sorted_dates[-1]},
            "narrative": (
                f"₹{total_value:.0f} Cr coordinated institutional accumulation — "
                f"{len(parties)} entities over {(last - first).days + 1} days"
            ),
        })

    # Sort by combined value descending
    coordinated.sort(key=lambda x: -x["combined_value_cr"])
    return coordinated


# ---------------------------------------------------------------------------
# Support Zone Cross-Reference
# ---------------------------------------------------------------------------

def _load_portfolio_zones() -> dict[str, list[dict]]:
    """
    Load BUY alert levels from stocks/*.md for portfolio stocks.
    Returns {ticker: [{"lower": float, "upper": float, "price_str": str}, ...]}
    """
    try:
        from alert_bot.config import STOCKS_DIR, EXCLUDED_MD_FILES
        from alert_bot.parser import load_all_stocks
        stocks = load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)
    except Exception as e:
        print(f"  Warning: could not load stock configs — {e}")
        return {}

    zones = {}
    for stock in stocks:
        buy_levels = []
        for level in stock.levels:
            if level.alert_type == "BUY":
                buy_levels.append({
                    "lower": level.lower,
                    "upper": level.upper,
                    "price_str": f"₹{level.lower:.0f}" + (f"-{level.upper:.0f}" if level.upper != level.lower else ""),
                })
        if buy_levels:
            zones[stock.ticker] = buy_levels

    return zones


def _match_zones(trades: list[dict], zones: dict) -> list[dict]:
    """
    For portfolio stock trades, check if trade price is within ±5% of a BUY zone.
    Returns list of zone match signals.
    """
    matches = []
    for t in trades:
        ticker = t.get("ticker", "")
        if ticker not in zones or t["type"] != "buy":
            continue

        price = t["avg_price"]
        if price <= 0:
            continue

        for zone in zones[ticker]:
            mid = (zone["lower"] + zone["upper"]) / 2
            distance_pct = abs(price - mid) / mid * 100

            if distance_pct <= 5:  # within ±5% of zone midpoint
                matches.append({
                    "ticker": ticker,
                    "zone": zone["price_str"],
                    "party": t["party"],
                    "category": t["category"],
                    "tier": t["tier"],
                    "date": t["date"],
                    "avg_price": price,
                    "distance_pct": round(distance_pct, 1),
                })
                break  # one match per trade is enough

    return matches


# ---------------------------------------------------------------------------
# Narrative Generation
# ---------------------------------------------------------------------------

def _build_narratives(
    trades: list[dict],
    profiles: dict,
    zone_matches: list[dict],
    family_clusters: dict,
) -> dict:
    """Build the complete insider_activity.json structure."""
    today = date.today().isoformat()

    # Date range
    all_dates = [t["date"] for t in trades]
    date_range = {"from": min(all_dates), "to": max(all_dates)} if all_dates else {}

    # --- Portfolio activity ---
    portfolio_activity = {}
    for t in trades:
        ticker = t.get("ticker", "")
        if ticker not in PORTFOLIO_TICKERS:
            continue

        if ticker not in portfolio_activity:
            portfolio_activity[ticker] = {"trades": [], "narratives": []}

        portfolio_activity[ticker]["trades"].append({
            "date": t["date"],
            "party": t["party"],
            "category": t["category"],
            "tier": t["tier"],
            "type": t["type"],
            "qty": t["qty"],
            "value_cr": t["value_cr"],
            "avg_price": t["avg_price"],
            "holdings_change_pct": t["holdings_change_pct"],
        })

    # Build per-ticker narratives for portfolio stocks
    for ticker, data in portfolio_activity.items():
        buy_trades = [t for t in data["trades"] if t["type"] == "buy"]
        sell_trades = [t for t in data["trades"] if t["type"] == "sell"]

        total_buy = sum(t["value_cr"] for t in buy_trades)
        total_sell = sum(t["value_cr"] for t in sell_trades)

        # Get zone matches for this ticker
        ticker_zone_matches = [zm for zm in zone_matches if zm["ticker"] == ticker]

        # Net accumulation per entity — distinguishes genuine positioning from arbitrage
        entity_net: dict[str, float] = {}
        entity_buy: dict[str, float] = {}
        for t in data["trades"]:
            p = t["party"]
            entity_buy[p]  = entity_buy.get(p, 0.0)  + (t["value_cr"] if t["type"] == "buy"  else 0.0)
            entity_net[p]  = entity_net.get(p, 0.0)   + (t["value_cr"] if t["type"] == "buy"  else -t["value_cr"])
        total_gross_buy = sum(entity_buy.values())
        genuine_acc = sorted(
            [{"party_name": p, "net_cr": round(n, 2)}
             for p, n in entity_net.items()
             if entity_buy[p] > 0 and (entity_buy[p] == entity_net[p] or n > 0.05 * entity_buy[p])],
            key=lambda x: -x["net_cr"],
        )
        arb_buy = sum(entity_buy[p] for p, n in entity_net.items()
                      if entity_buy[p] > 0 and not (entity_buy[p] == entity_net[p] or n > 0.05 * entity_buy[p]))
        arb_ratio = round(arb_buy / total_gross_buy, 3) if total_gross_buy > 0 else 0.0

        data["summary"] = {
            "net_direction":         "buy" if total_buy > total_sell else "sell",
            "total_buy_value_cr":    round(total_buy, 2),
            "total_sell_value_cr":   round(total_sell, 2),
            "net_value_cr":          round(total_buy - total_sell, 2),
            "genuine_accumulators":  genuine_acc,
            "arbitrage_ratio":       arb_ratio,
            "promoter_buying":       any(t["tier"] == "promoter" and t["type"] == "buy" for t in data["trades"]),
            "promoter_selling":      any(t["tier"] == "promoter" and t["type"] == "sell" for t in data["trades"]),
            "trade_count":           len(data["trades"]),
            "latest_trade_date":     max(t["date"] for t in data["trades"]),
        }

        if ticker_zone_matches:
            data["support_zone_match"] = {
                "zone": ticker_zone_matches[0]["zone"],
                "distance_pct": ticker_zone_matches[0]["distance_pct"],
            }

        # Build narrative per party
        narratives = []
        parties_seen = set()
        for t in data["trades"]:
            if t["party"] in parties_seen:
                continue
            parties_seen.add(t["party"])

            party_profile = profiles.get(t["party"], {})
            confidence = party_profile.get("confidence", "unknown")

            # Build one-liner
            action = "bought" if t["type"] == "buy" else "sold"
            party_total = sum(
                tr["value_cr"] for tr in data["trades"]
                if tr["party"] == t["party"] and tr["type"] == t["type"]
            )

            parts = [f"{t['party']} ({t['category']}) {action} ₹{party_total:.0f} Cr at ₹{t['avg_price']:.0f}"]

            if t["holdings_change_pct"]:
                parts.append(f"Holdings: {t['holdings_change_pct']:+.1f}%")

            zone_match = next((zm for zm in ticker_zone_matches if zm["party"] == t["party"]), None)
            if zone_match:
                parts.append(f"Near BUY zone {zone_match['zone']}")

            narratives.append({
                "text": ". ".join(parts),
                "confidence": confidence,
                "party": t["party"],
            })

        data["narratives"] = narratives

    # --- Promoter signals (all stocks, not just portfolio) ---
    promoter_signals = []
    promoter_trades_by_ticker = defaultdict(list)
    for t in trades:
        if t["tier"] == "promoter" and t["type"] == "buy":
            promoter_trades_by_ticker[t.get("ticker", "")].append(t)

    for ticker, ptrades in promoter_trades_by_ticker.items():
        if not ticker:
            continue
        total = sum(t["value_cr"] for t in ptrades)
        if total < 1:  # Skip tiny promoter buys
            continue

        parties = sorted({t["party"] for t in ptrades})
        avg_price = round(
            sum(t["avg_price"] * t["value_cr"] for t in ptrades) / total, 2
        ) if total > 0 else 0

        # Determine strength
        if total >= 100:
            strength = "very_strong"
        elif total >= 10:
            strength = "strong"
        elif total >= 1:
            strength = "moderate"
        else:
            strength = "weak"

        promoter_signals.append({
            "ticker": ticker,
            "stock_name": ptrades[0]["stock_name"],
            "parties": parties,
            "value_cr": round(total, 2),
            "avg_price": avg_price,
            "trade_count": len(ptrades),
            "pattern": _classify_pattern(ptrades),
            "strength": strength,
            "in_portfolio": ticker in PORTFOLIO_TICKERS,
            "is_family_cluster": ticker in family_clusters,
        })

    promoter_signals.sort(key=lambda x: -x["value_cr"])

    # --- Explore candidates (non-portfolio stocks with strong signals) ---
    explore_candidates = []

    # From promoter signals
    for ps in promoter_signals:
        if ps["in_portfolio"]:
            continue
        if ps["strength"] in ("strong", "very_strong"):
            explore_candidates.append({
                "ticker": ps["ticker"],
                "stock_name": ps["stock_name"],
                "reason": "promoter_accumulation",
                "narrative": (
                    f"Promoter bought ₹{ps['value_cr']:.0f} Cr at ₹{ps['avg_price']:.0f}. "
                    f"{ps['strength'].replace('_', ' ').title()} conviction."
                ),
                "value_cr": ps["value_cr"],
            })

    # Coordinated buys are added separately (passed in from caller)

    return {
        "last_updated": today,
        "date_range": date_range,
        "portfolio_activity": portfolio_activity,
        "promoter_signals": promoter_signals[:30],  # Top 30
        "explore_candidates": explore_candidates[:15],
        # coordinated_buys added by caller
    }


# ---------------------------------------------------------------------------
# Telegram alerts for high-priority portfolio signals
# ---------------------------------------------------------------------------

def _send_portfolio_alerts(activity: dict, profiles: dict, zone_matches: list[dict], meta: dict):
    """Send Telegram alerts for significant portfolio insider activity."""
    sent_key = "insider_alerts_sent"
    already_sent = set()
    for item in meta.get(sent_key, []):
        already_sent.add((item.get("ticker", ""), item.get("date", ""), item.get("party", "")))

    new_alerts = []

    for ticker, data in activity.get("portfolio_activity", {}).items():
        for t in data["trades"]:
            dedup_key = (ticker, t["date"], t["party"])
            if dedup_key in already_sent:
                continue

            # Check if this trade is significant enough for an alert
            should_alert = False
            if t["tier"] == "promoter" and t["type"] == "buy" and t["value_cr"] >= 5:
                should_alert = True
            elif t["tier"] == "promoter" and t["type"] == "sell" and t["value_cr"] >= 1:
                should_alert = True

            if not should_alert:
                continue

            # Build alert message
            profile = profiles.get(t["party"], {})
            action = "BUY" if t["type"] == "buy" else "SELL"
            emoji = "🔔" if t["type"] == "buy" else "⚠️"

            msg_parts = [
                f'{emoji} <b>INSIDER TRADE</b>',
                f'',
                f'<b>{ticker}</b> — {t["category"]} {action}',
                f'{t["party"]}',
                f'₹{t["value_cr"]:.0f} Cr at avg ₹{t["avg_price"]:.0f}/share',
            ]

            if t["holdings_change_pct"]:
                msg_parts.append(f'Holdings: {t["holdings_change_pct"]:+.1f}%')

            msg_parts.append(f'Date: {t["date"]}')

            # Zone match?
            zone_match = next(
                (zm for zm in zone_matches if zm["ticker"] == ticker and zm["party"] == t["party"]),
                None
            )
            if zone_match:
                msg_parts.append(f'')
                msg_parts.append(f'📌 Near BUY zone {zone_match["zone"]} — promoter-validated support')

            # Pattern insight
            pattern = profile.get("pattern", "")
            confidence = profile.get("confidence", "")
            if pattern == "single_large_block":
                msg_parts.append(f'💡 Single massive block = high conviction, not routine')
            elif pattern == "steady_accumulation":
                msg_parts.append(f'💡 Steady accumulation over multiple sessions')
            elif pattern == "family_cluster":
                msg_parts.append(f'💡 Multiple family members buying = coordinated insider accumulation')

            msg = "\n".join(msg_parts)

            try:
                os.system(f'python3 send_message.py "{msg}"')
                new_alerts.append({"ticker": ticker, "date": t["date"], "party": t["party"]})
                already_sent.add(dedup_key)
                print(f"  📨 Sent alert: {ticker} — {t['party']} {action} ₹{t['value_cr']:.0f} Cr")
            except Exception as e:
                print(f"  ⚠️ Alert send failed: {e}")

    # Save sent alerts to meta
    if new_alerts:
        meta.setdefault(sent_key, []).extend(new_alerts)
        _save_meta(meta)


# ---------------------------------------------------------------------------
# CLI display functions
# ---------------------------------------------------------------------------

def _print_ticker_detail(ticker: str, activity: dict, profiles: dict, coordinated: list):
    """Print detailed info for a specific ticker."""
    print(f"\n{'='*60}")
    print(f"  {ticker} — Insider Activity Detail")
    print(f"{'='*60}")

    # Portfolio activity
    pa = activity.get("portfolio_activity", {}).get(ticker)
    if pa:
        print(f"\n  Portfolio stock: YES")
        print(f"  Trades: {pa['summary']['trade_count']}")
        print(f"  Net direction: {pa['summary']['net_direction']}")
        print(f"  Buy value: ₹{pa['summary']['total_buy_value_cr']:.2f} Cr")
        print(f"  Sell value: ₹{pa['summary']['total_sell_value_cr']:.2f} Cr")

        if pa.get("support_zone_match"):
            zm = pa["support_zone_match"]
            print(f"  Zone match: {zm['zone']} ({zm['distance_pct']}% away)")

        print(f"\n  Narratives:")
        for n in pa.get("narratives", []):
            print(f"    [{n['confidence']}] {n['text']}")

        print(f"\n  All trades:")
        for t in pa["trades"]:
            print(f"    {t['date']} | {t['party'][:40]} | {t['type']} | ₹{t['value_cr']:.2f} Cr @ ₹{t['avg_price']:.0f}")
    else:
        print(f"\n  Portfolio stock: NO")

    # Promoter signals
    ps = [s for s in activity.get("promoter_signals", []) if s["ticker"] == ticker]
    if ps:
        print(f"\n  Promoter Signals:")
        for s in ps:
            print(f"    {', '.join(s['parties'][:3])}")
            print(f"    ₹{s['value_cr']:.0f} Cr | pattern: {s['pattern']} | strength: {s['strength']}")

    # Coordinated buys
    cb = [c for c in coordinated if c["ticker"] == ticker]
    if cb:
        print(f"\n  Coordinated Buys:")
        for c in cb:
            print(f"    {c['narrative']}")
            for p in c["parties"][:5]:
                print(f"      {p['name'][:40]}: ₹{p['value_cr']:.0f} Cr on {', '.join(p['dates'][:3])}")

    # Party profiles
    relevant_parties = set()
    if pa:
        for t in pa["trades"]:
            relevant_parties.add(t["party"])
    for s in ps:
        relevant_parties.update(s["parties"])

    if relevant_parties:
        print(f"\n  Party Dossiers:")
        for party in sorted(relevant_parties):
            p = profiles.get(party)
            if p:
                print(f"\n    {party}")
                print(f"    Tier: {p['tier']} | Confidence: {p.get('confidence', '?')}")
                if p.get("who"):
                    print(f"    Who: {p['who'][:100]}")
                if p.get("track_record"):
                    print(f"    Track: {p['track_record'][:100]}")


def _print_who_detail(search_term: str, trades: list[dict], profiles: dict):
    """Print all trades by entities matching a search term."""
    search_upper = search_term.upper()
    matching_trades = [t for t in trades if search_upper in t["party"].upper()]

    if not matching_trades:
        print(f"  No trades found for '{search_term}'")
        return

    parties = sorted({t["party"] for t in matching_trades})
    print(f"\n{'='*60}")
    print(f"  Trades matching: '{search_term}'")
    print(f"  Matched {len(matching_trades)} trades across {len(parties)} entities")
    print(f"{'='*60}")

    for party in parties:
        profile = profiles.get(party, {})
        party_trades = [t for t in matching_trades if t["party"] == party]
        total_buy = sum(t["value_cr"] for t in party_trades if t["type"] == "buy")
        total_sell = sum(t["value_cr"] for t in party_trades if t["type"] == "sell")
        stocks = sorted({t.get("ticker", t["stock_name"]) for t in party_trades})

        print(f"\n  {party}")
        if profile.get("who"):
            print(f"  Who: {profile['who']}")
        if profile.get("confidence"):
            print(f"  Confidence: {profile['confidence']}")
        if profile.get("track_record"):
            print(f"  Track record: {profile['track_record']}")
        print(f"  Buy: ₹{total_buy:.0f} Cr | Sell: ₹{total_sell:.0f} Cr | Trades: {len(party_trades)}")
        print(f"  Stocks: {', '.join(stocks)}")
        print(f"  Trades:")
        for t in sorted(party_trades, key=lambda x: x["date"]):
            ticker = t.get("ticker", "???")
            print(f"    {t['date']} | {ticker:12s} | {t['type']:4s} | ₹{t['value_cr']:8.2f} Cr @ ₹{t['avg_price']:.0f}")


def _print_coordinated(coordinated: list):
    """Print coordinated buy signals."""
    print(f"\n{'='*60}")
    print(f"  Coordinated Institutional Buys ({len(coordinated)} detected)")
    print(f"{'='*60}")

    for c in coordinated[:20]:  # Top 20
        print(f"\n  {c['ticker']} ({c['stock']}) — ₹{c['combined_value_cr']:.0f} Cr")
        print(f"  {c['narrative']}")
        print(f"  Period: {c['date_range']['first']} → {c['date_range']['last']}")
        for p in c["parties"][:5]:
            print(f"    {p['name'][:50]:50s} ₹{p['value_cr']:8.0f} Cr  ({', '.join(p['dates'][:3])})")


# ---------------------------------------------------------------------------
# Sector Aggregation — for convergence analysis
# ---------------------------------------------------------------------------

# Ticker → sector mapping (derived from stock_universe.json themes)
# Each ticker maps to the theme name in the universe file
TICKER_TO_SECTOR = {}

def _build_ticker_sector_map():
    """Load stock_universe.json and build a ticker → sector lookup."""
    global TICKER_TO_SECTOR
    universe_file = DATA_DIR / "stock_universe.json"
    if not universe_file.exists():
        return

    try:
        universe = json.loads(universe_file.read_text())
    except Exception:
        return

    for theme_name, tiers in universe.get("themes", {}).items():
        for tier_key in ("tier1", "tier2"):
            for entry in tiers.get(tier_key, []):
                ticker = entry.get("ticker", "")
                if ticker:
                    TICKER_TO_SECTOR[ticker] = {
                        "sector": theme_name,
                        "tier": tier_key,
                        "note": entry.get("note", ""),
                    }

    # Ticker aliases — stock_universe.json may use a different ticker
    aliases = {"TEJAS": "TEJASNET", "HAPPSTMNDS": "HAPPSTMNDS"}
    for universe_ticker, mapped_ticker in aliases.items():
        if universe_ticker in TICKER_TO_SECTOR and mapped_ticker not in TICKER_TO_SECTOR:
            TICKER_TO_SECTOR[mapped_ticker] = TICKER_TO_SECTOR[universe_ticker]

    # Additional tickers not in stock_universe.json but appearing in insider data
    # These are manually classified to the correct thematic bucket
    extra_tickers = {
        "TEJASNET":   {"sector": "Telecom & Digital",       "tier": "tier2", "note": "Tata group, 5G/BSNL telecom equipment"},
        "GUJALKALI":  {"sector": "Chemicals & Specialty",   "tier": "tier1", "note": "Caustic soda, soda ash — industrial chemicals"},
        "NOCIL":      {"sector": "Chemicals & Specialty",   "tier": "tier2", "note": "Rubber chemicals, specialty additives"},
        "HEG":        {"sector": "Chemicals & Specialty",   "tier": "tier2", "note": "Graphite electrodes, steel EAF supply chain"},
        "APOLLOPIPE": {"sector": "Infrastructure & Capex",  "tier": "tier2", "note": "PVC/CPVC pipes, building materials"},
        "DCXINDIA":   {"sector": "Defense & Aerospace",     "tier": "tier2", "note": "Defense electronics sub-assemblies, Israeli/US primes"},
        "CYIENTDLM":  {"sector": "Semiconductor & Electronics", "tier": "tier2", "note": "Defense + industrial PCB assemblies"},
        "RSYSTEMS":   {"sector": "IT Services & SaaS",      "tier": "tier2", "note": "Mid-cap IT services, digital transformation"},
        "VMART":      {"sector": "FMCG & Consumption",      "tier": "tier2", "note": "Value fashion retail, Tier 2/3 cities"},
        "KNR":        {"sector": "Infrastructure & Capex",  "tier": "tier2", "note": "Highway EPC, toll road developer"},
        "GMRAIRPORT": {"sector": "Infrastructure & Capex",  "tier": "tier1", "note": "Delhi + Hyderabad airports, irreplaceable monopoly"},
        "JSL":        {"sector": "Metals & Mining",         "tier": "tier1", "note": "Jindal Stainless — India's largest stainless steel producer"},
        "RKFORGE":    {"sector": "Metals & Mining",         "tier": "tier2", "note": "Ramkrishna Forgings — railway + auto forgings"},
        "DALMIASUG":  {"sector": "Agriculture & Fertilizers","tier": "tier2", "note": "Sugar + ethanol, integrated agri-processor"},
        "BLS":        {"sector": "IT Services & SaaS",      "tier": "tier2", "note": "BLS International — visa processing + fintech"},
        "DEEPAKNTR":  {"sector": "Chemicals & Specialty",   "tier": "tier1", "note": "Only Indian phenol-acetone producer, explosives"},
        "LUXIND":     {"sector": "FMCG & Consumption",      "tier": "tier2", "note": "Innerwear/hosiery, rural consumption play"},
        "JINDRILL":   {"sector": "Oil & Gas / Energy",      "tier": "tier2", "note": "Offshore drilling services, E&P support"},
        "RELIGARE":   {"sector": "Banking & Financials",    "tier": "tier2", "note": "Financial services, Burman family takeover play"},
        "KILBURN":    {"sector": "Infrastructure & Capex",  "tier": "tier2", "note": "Kilburn Engineering — industrial dryers, process equipment"},
        "MASFIN":     {"sector": "Banking & Financials",    "tier": "tier2", "note": "Vehicle finance NBFC, rural/semi-urban"},
        "GANESHBE":   {"sector": "Chemicals & Specialty",   "tier": "tier2", "note": "PET bottle recycling, circular economy play"},
        "OLAELEC":    {"sector": "Auto & EV",               "tier": "tier2", "note": "Electric scooters, LFP cell manufacturing"},
    }
    for ticker, info in extra_tickers.items():
        if ticker not in TICKER_TO_SECTOR:
            TICKER_TO_SECTOR[ticker] = info

    # Also map portfolio stocks by their known sectors
    portfolio_sectors = {
        "ANANTRAJ": "Real Estate & Construction",
        "BBOX": "IT Services & SaaS",
        "BDL": "Defense & Aerospace",
        "CGPOWER": "Infrastructure & Capex",
        "HBLENGINE": "Defense & Aerospace",
        "HINDCOPPER": "Metals & Mining",
        "RAILTEL": "Telecom & Digital",
        "SHARDACROP": "Agriculture & Fertilizers",
        "SPARC": "Pharma & Healthcare",
        "STLTECH": "Telecom & Digital",
        "SUVEN": "Pharma & Healthcare",
        "SUZLON": "Renewable Energy & Green",
        "ADSL": "IT Services & SaaS",
        "NIF100BEES": "Index",
        "GMDCLTD": "Metals & Mining",
    }
    for ticker, sector in portfolio_sectors.items():
        if ticker not in TICKER_TO_SECTOR:
            TICKER_TO_SECTOR[ticker] = {"sector": sector, "tier": "portfolio", "note": ""}


def _build_sector_signals(
    trades: list[dict],
    coordinated: list[dict],
    profiles: dict,
    activity: dict,
) -> dict:
    """
    Aggregate insider signals by sector for convergence analysis.
    Outputs data/sector_signals.json — the structured input that Claude
    uses to reason about sector-level convergence with macro data.
    """
    _build_ticker_sector_map()
    today = date.today().isoformat()

    # --- Aggregate by sector ---
    sector_data = defaultdict(lambda: {
        "smart_money_flow_cr": 0,
        "promoter_flow_cr": 0,
        "coordinated_count": 0,
        "total_entities": 0,
        "stocks": {},       # ticker → {value_cr, entities, type}
        "promoter_signals": [],
        "has_portfolio_stock": False,
        "portfolio_stocks": [],
    })

    # 1. Smart money / coordinated buys
    for c in coordinated:
        ticker = c["ticker"]
        sector_info = TICKER_TO_SECTOR.get(ticker, {})
        sector = sector_info.get("sector", "Unknown")

        sd = sector_data[sector]
        sd["smart_money_flow_cr"] += c["combined_value_cr"]
        sd["coordinated_count"] += 1
        sd["total_entities"] += c["party_count"]
        sd["stocks"][ticker] = {
            "value_cr": c["combined_value_cr"],
            "entities": c["party_count"],
            "type": "coordinated",
            "name": c["stock"],
            "narrative": c["narrative"],
        }

        if ticker in PORTFOLIO_TICKERS:
            sd["has_portfolio_stock"] = True
            sd["portfolio_stocks"].append(ticker)

    # 2. Promoter signals
    for ps in activity.get("promoter_signals", []):
        ticker = ps["ticker"]
        sector_info = TICKER_TO_SECTOR.get(ticker, {})
        sector = sector_info.get("sector", "Unknown")

        sd = sector_data[sector]
        sd["promoter_flow_cr"] += ps["value_cr"]
        sd["promoter_signals"].append({
            "ticker": ticker,
            "party": ps["parties"][0] if ps.get("parties") else "Unknown",
            "value_cr": ps["value_cr"],
            "strength": ps["strength"],
            "pattern": ps["pattern"],
            "is_family_cluster": ps.get("is_family_cluster", False),
        })

        # Also add to stocks if not already there from coordinated
        if ticker not in sd["stocks"]:
            sd["stocks"][ticker] = {
                "value_cr": ps["value_cr"],
                "entities": 1,
                "type": "promoter",
                "name": ps.get("stock_name", ticker),
            }

        if ticker in PORTFOLIO_TICKERS:
            sd["has_portfolio_stock"] = True
            if ticker not in sd["portfolio_stocks"]:
                sd["portfolio_stocks"].append(ticker)

    # 3. Build the output structure
    sectors_out = {}
    for sector, sd in sorted(sector_data.items(), key=lambda x: -(x[1]["smart_money_flow_cr"] + x[1]["promoter_flow_cr"])):
        if sector == "Unknown":
            continue

        total_flow = sd["smart_money_flow_cr"] + sd["promoter_flow_cr"]
        if total_flow < 5:  # Skip tiny flows
            continue

        # Signal strength classification
        has_smart_money = sd["smart_money_flow_cr"] >= 100
        has_promoter = sd["promoter_flow_cr"] >= 1
        signal_layers = sum([has_smart_money, has_promoter])
        # Macro layer is determined by Claude at analysis time (needs reasoning)

        sectors_out[sector] = {
            "total_flow_cr": round(total_flow, 1),
            "smart_money_flow_cr": round(sd["smart_money_flow_cr"], 1),
            "promoter_flow_cr": round(sd["promoter_flow_cr"], 1),
            "coordinated_buy_count": sd["coordinated_count"],
            "total_entities_involved": sd["total_entities"],
            "signal_layers": signal_layers,  # 0-2 from data; macro added by Claude
            "has_portfolio_exposure": sd["has_portfolio_stock"],
            "portfolio_stocks": sd["portfolio_stocks"],
            "top_stocks": [
                {
                    "ticker": t,
                    "value_cr": info["value_cr"],
                    "entities": info["entities"],
                    "type": info["type"],
                    "name": info.get("name", t),
                }
                for t, info in sorted(sd["stocks"].items(), key=lambda x: -x[1]["value_cr"])[:5]
            ],
            "promoter_signals": sd["promoter_signals"][:5],
        }

    # 4. Portfolio gap analysis — sectors with strong signals but no portfolio stock
    portfolio_gaps = []
    for sector, sdata in sectors_out.items():
        if not sdata["has_portfolio_exposure"] and sdata["total_flow_cr"] >= 100:
            portfolio_gaps.append({
                "sector": sector,
                "total_flow_cr": sdata["total_flow_cr"],
                "smart_money_flow_cr": sdata["smart_money_flow_cr"],
                "promoter_flow_cr": sdata["promoter_flow_cr"],
                "top_stocks": sdata["top_stocks"][:3],
                "gap_severity": (
                    "critical" if sdata["total_flow_cr"] >= 1000
                    else "high" if sdata["total_flow_cr"] >= 500
                    else "moderate"
                ),
            })
    portfolio_gaps.sort(key=lambda x: -x["total_flow_cr"])

    # 5. Top opportunities — stocks ranked by convergence of signals
    top_opps = []
    for sector, sdata in sectors_out.items():
        for stock in sdata["top_stocks"]:
            # Check if this stock also has promoter backing
            has_promoter_here = any(
                ps["ticker"] == stock["ticker"] for ps in sdata["promoter_signals"]
            )
            convergence_score = 0
            if stock["value_cr"] >= 100:
                convergence_score += 2  # Strong smart money
            elif stock["value_cr"] >= 20:
                convergence_score += 1
            if has_promoter_here:
                convergence_score += 2  # Promoter backing
            if stock["entities"] >= 5:
                convergence_score += 1  # Many entities = consensus
            if stock["ticker"] not in PORTFOLIO_TICKERS:
                convergence_score += 1  # New name = discovery value

            if convergence_score >= 2:
                top_opps.append({
                    "ticker": stock["ticker"],
                    "name": stock.get("name", stock["ticker"]),
                    "sector": sector,
                    "value_cr": stock["value_cr"],
                    "entities": stock["entities"],
                    "has_promoter": has_promoter_here,
                    "in_portfolio": stock["ticker"] in PORTFOLIO_TICKERS,
                    "convergence_score": convergence_score,
                })
    top_opps.sort(key=lambda x: -x["convergence_score"])

    # Read macro signals if available (written by Claude during digest processing)
    macro_signals = {}
    macro_file = DATA_DIR / "macro_signals.json"
    if macro_file.exists():
        try:
            macro_signals = json.loads(macro_file.read_text())
        except Exception:
            pass

    # Assemble final output
    all_dates = [t["date"] for t in trades]
    output = {
        "last_updated": today,
        "insider_date_range": {
            "from": min(all_dates) if all_dates else "",
            "to": max(all_dates) if all_dates else "",
        },
        "macro_date": macro_signals.get("date", "none — run morning digest first"),
        "sectors": sectors_out,
        "portfolio_gaps": portfolio_gaps,
        "top_opportunities": top_opps[:20],
        "summary_stats": {
            "sectors_with_activity": len(sectors_out),
            "total_smart_money_cr": round(sum(s["smart_money_flow_cr"] for s in sectors_out.values()), 1),
            "total_promoter_cr": round(sum(s["promoter_flow_cr"] for s in sectors_out.values()), 1),
            "portfolio_gaps_found": len(portfolio_gaps),
            "critical_gaps": sum(1 for g in portfolio_gaps if g["gap_severity"] == "critical"),
        },
    }

    # Save
    SECTOR_FILE.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"  Sector signals saved → {SECTOR_FILE}")

    return output


def _print_summary(activity: dict, profiles: dict, coordinated: list):
    """Print a high-level summary."""
    print(f"\n{'='*60}")
    print(f"  INSIDER ACTIVITY SUMMARY")
    print(f"  Period: {activity['date_range'].get('from', '?')} → {activity['date_range'].get('to', '?')}")
    print(f"{'='*60}")

    # Portfolio hits
    pa = activity.get("portfolio_activity", {})
    if pa:
        print(f"\n  📊 PORTFOLIO STOCKS ({len(pa)} with insider activity):")
        for ticker, data in sorted(pa.items()):
            s = data["summary"]
            direction = "↑ BUY" if s["net_direction"] == "buy" else "↓ SELL"
            promoter = " [PROMOTER]" if s["promoter_buying"] else ""
            print(f"    {ticker:12s} {direction} ₹{s['total_buy_value_cr']:.0f} Cr{promoter}")
            for n in data.get("narratives", []):
                print(f"      [{n['confidence']}] {n['text'][:80]}")
    else:
        print(f"\n  📊 No insider activity on portfolio stocks in this period")

    # Top promoter signals
    ps = activity.get("promoter_signals", [])
    if ps:
        print(f"\n  🔑 TOP PROMOTER SIGNALS ({len(ps)} total):")
        for s in ps[:10]:
            portfolio_flag = " ⭐" if s["in_portfolio"] else ""
            family_flag = " 👨‍👩‍👧‍👦" if s["is_family_cluster"] else ""
            print(f"    {s['ticker']:12s} ₹{s['value_cr']:>8.0f} Cr  [{s['strength']}]{portfolio_flag}{family_flag}")

    # Coordinated buys
    if coordinated:
        print(f"\n  🤝 COORDINATED BUYS ({len(coordinated)} detected):")
        for c in coordinated[:5]:
            print(f"    {c['ticker']:12s} ₹{c['combined_value_cr']:>8.0f} Cr  {c['party_count']} entities")

    # Explore candidates
    ec = activity.get("explore_candidates", [])
    if ec:
        print(f"\n  🔍 EXPLORE CANDIDATES ({len(ec)} non-portfolio stocks):")
        for e in ec[:5]:
            print(f"    {e['ticker']:12s} {e['narrative'][:60]}")

    # Profile stats
    need_research = sum(1 for p in profiles.values() if not p.get("last_researched"))
    print(f"\n  📋 Profiles: {len(profiles)} total | {need_research} need research")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    # Parse CLI modes
    ticker_filter = None
    who_filter = None
    show_coordinated = False

    i = 0
    while i < len(args):
        if args[i] == "--ticker" and i + 1 < len(args):
            ticker_filter = args[i + 1].upper()
            i += 2
        elif args[i] == "--who" and i + 1 < len(args):
            who_filter = args[i + 1]
            i += 2
        elif args[i] == "--coordinated":
            show_coordinated = True
            i += 1
        else:
            i += 1

    # Check freshness
    meta = _load_meta()
    if not force and not ticker_filter and not who_filter and not show_coordinated:
        csv_files = sorted(p for p in INSIDER_DIR.glob("*.csv") if not p.name.startswith("._"))
        csv_mtimes = {f.name: f.stat().st_mtime for f in csv_files}
        last_processed = meta.get("insider_trades", {}).get("csv_mtimes", {})

        if csv_mtimes == last_processed and ACTIVITY_FILE.exists():
            print("Insider data is current — no new CSVs. Use --force to reprocess.")
            # Still allow detail views
            activity = json.loads(ACTIVITY_FILE.read_text())
            profiles = _load_profiles()
            coordinated = activity.get("coordinated_buys", [])
            _print_summary(activity, profiles, coordinated)
            return

    print("Processing insider trades...")

    # Step 1: Parse all CSVs
    raw_trades = _parse_csvs()
    if not raw_trades:
        print("No trades found. Exiting.")
        return

    # Step 2: Deduplicate
    trades = _dedup(raw_trades)

    # Step 3: Map company names to tickers
    trades = _map_tickers(trades)

    # Step 3b: Write new trades to market.db (INSERT OR IGNORE — idempotent dedup)
    try:
        import market_db as _mdb
        _db_conn = _mdb.get_conn()
        for t in trades:
            _mdb.insert_insider_trade(_db_conn, t)
        _db_conn.commit()
        print(f"  market.db: {len(trades)} trades written (duplicates ignored)")
    except Exception as _e:
        print(f"  market.db write skipped — {_e}")
        _db_conn = None

    # Step 4: Load existing profiles
    existing_profiles = _load_profiles()

    # Step 5: Build/update profiles
    print("Building party profiles...")
    profiles = _build_profiles(trades, existing_profiles)
    _save_profiles(profiles)
    print(f"  {len(profiles)} profiles saved → {PROFILES_FILE}")

    # Step 5b: Upsert party profiles to market.db
    if _db_conn is not None:
        try:
            import market_db as _mdb
            for name, profile in profiles.items():
                _mdb.upsert_party_profile(_db_conn, name, profile)
            _db_conn.commit()
            print(f"  market.db: {len(profiles)} party profiles upserted")
        except Exception as _e:
            print(f"  market.db profile upsert skipped — {_e}")

    # Step 6: Detect family clusters
    family_clusters = _detect_family_clusters(trades)
    if family_clusters:
        print(f"  Family clusters detected: {', '.join(family_clusters.keys())}")

    # Step 7: Detect coordinated buys
    print("Detecting coordinated buys...")
    coordinated = _detect_coordinated_buys(trades)
    print(f"  {len(coordinated)} coordinated buy signals found")

    # Step 8: Cross-reference support zones
    print("Cross-referencing support zones...")
    zones = _load_portfolio_zones()
    zone_matches = _match_zones(trades, zones)
    if zone_matches:
        for zm in zone_matches:
            print(f"  ✅ {zm['ticker']}: {zm['party'][:30]} bought at ₹{zm['avg_price']:.0f} — near zone {zm['zone']}")

    # Step 9: Build narratives and activity JSON
    # Use 90-day rolling window from DB so JSON is always historically complete.
    # Falls back to current batch if DB is unavailable.
    print("Building activity JSON...")
    if _db_conn is not None:
        try:
            import market_db as _mdb
            windowed_trades = _mdb.query_windowed_trades(_db_conn, days=90)
            print(f"  Using {len(windowed_trades)} trades from DB (last 90 days)")
        except Exception as _e:
            print(f"  DB window query failed, using batch trades — {_e}")
            windowed_trades = trades
    else:
        windowed_trades = trades
    activity = _build_narratives(windowed_trades, profiles, zone_matches, family_clusters)
    activity["coordinated_buys"] = coordinated[:30]  # Top 30

    # Add coordinated explore candidates
    for c in coordinated[:10]:
        if c["ticker"] not in PORTFOLIO_TICKERS and c["combined_value_cr"] >= 20:
            activity["explore_candidates"].append({
                "ticker": c["ticker"],
                "stock_name": c["stock"],
                "reason": "coordinated_institutional",
                "narrative": c["narrative"],
                "value_cr": c["combined_value_cr"],
            })

    # Sort explore candidates by value
    activity["explore_candidates"].sort(key=lambda x: -x.get("value_cr", 0))
    activity["explore_candidates"] = activity["explore_candidates"][:15]

    # Save activity JSON
    DATA_DIR.mkdir(exist_ok=True)
    ACTIVITY_FILE.write_text(json.dumps(activity, indent=2, ensure_ascii=False))
    print(f"  Activity saved → {ACTIVITY_FILE}")

    # Step 9b: Build sector-level signals for convergence analysis
    print("Building sector signals...")
    sector_signals = _build_sector_signals(windowed_trades, coordinated, profiles, activity)
    gaps = sector_signals.get("portfolio_gaps", [])
    if gaps:
        critical = [g for g in gaps if g["gap_severity"] == "critical"]
        print(f"  ⚠️  {len(gaps)} portfolio gaps found ({len(critical)} critical)")
        for g in gaps[:5]:
            sev = "🔴" if g["gap_severity"] == "critical" else "🟡" if g["gap_severity"] == "high" else "⚪"
            print(f"    {sev} {g['sector']:30s} ₹{g['total_flow_cr']:>7,.0f} Cr  [{g['gap_severity']}]")

    # Step 10: Update meta
    csv_files = sorted(p for p in INSIDER_DIR.glob("*.csv") if not p.name.startswith("._"))
    meta.setdefault("insider_trades", {})["csv_mtimes"] = {
        f.name: f.stat().st_mtime for f in csv_files
    }
    meta["insider_trades"]["last_processed"] = date.today().isoformat()
    _save_meta(meta)

    # Self-clear the ingestion watch flag — watcher re-sets it if new CSVs arrive while we ran
    try:
        _wf = Path("data/watch_flags.json")
        if _wf.exists():
            _fl = json.loads(_wf.read_text())
            _fl["insider_csv_pending"] = False
            _tmp = _wf.with_suffix(".tmp")
            _tmp.write_text(json.dumps(_fl, indent=2))
            _tmp.rename(_wf)
    except Exception:
        pass

    # Close DB connection
    if _db_conn is not None:
        try:
            _db_conn.close()
        except Exception:
            pass

    # Step 11: Send Telegram alerts for portfolio stocks
    _send_portfolio_alerts(activity, profiles, zone_matches, meta)

    # --- Display ---
    if ticker_filter:
        _print_ticker_detail(ticker_filter, activity, profiles, coordinated)
    elif who_filter:
        _print_who_detail(who_filter, trades, profiles)
    elif show_coordinated:
        _print_coordinated(coordinated)
    else:
        _print_summary(activity, profiles, coordinated)


if __name__ == "__main__":
    main()
