"""
Gold tracker — Indian investor accumulation signals.

Architectural principle (read commodities/metals_instructions.md before editing):
  Python does ALL deterministic math. The output bundle
  (data/gold_analysis_bundle.json) leaves zero arithmetic for the LLM —
  every number, percentile, slope, label, and historical context that
  narrative reasoning might need is precomputed here.

Public surface:
  fetch_gold_snapshot(cfg)   — top-level: fetches all 7 yfinance series,
                                computes prices/scorecard/regime/zones,
                                writes both data/gold_snapshot.json (compact)
                                and data/gold_analysis_bundle.json (full).
                                Returns the full bundle dict.
  classify_regime(tailwinds_count, price_extension_for_lump_sum)
                              — pure function: tailwind count + extension
                                flag → "ACCUMULATE" | "NEUTRAL" | "WAIT"
  check_gold_zones(cfg, prev_inr_per_gram, curr_inr_per_gram, state)
                              — reuses engine._crosses() to detect zone
                                crossings on the 24K ₹/gram price. Returns
                                a list of (zone, message) tuples to send.
  format_gold_telegram(bundle, narrative_quotes, transition_kind)
                              — composes the Telegram message body for
                                regime transitions and weekly digests.

What this module does NOT do:
  - Touch GOLDLENDER alerts (decoupled — convergence layer reads our bundle)
  - Write to gold_narrative.json (autonomous loop's exclusive domain)
  - Recompute anything during narrative reasoning (Python is the only writer)
  - Fire daily Telegram noise (only on regime transitions / zone crossings)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pytz

from .config import (
    GOLD_BUNDLE_FILE, GOLD_NARRATIVE_FILE, GOLD_SNAPSHOT_FILE,
    MARKET_TIMEZONE,
)
from .ohlc_cache import load_ohlc_cached
from .parser import GoldConfig

logger = logging.getLogger(__name__)
IST = pytz.timezone(MARKET_TIMEZONE)

# ---------------------------------------------------------------------------
# yfinance symbols — the entire fetch surface lives here
# ---------------------------------------------------------------------------
# Each entry: (cache_ticker, yf_symbol, role)
GOLD_SYMBOLS: list[tuple[str, str, str]] = [
    # Primary price series
    ("GOLD",     "GC=F",         "international gold futures (USD/oz)"),
    ("GOLDBEES", "GOLDBEES.NS",  "Indian gold ETF (₹/unit)"),
    ("INR",      "INR=X",        "USDINR (₹ per USD) — context only, NOT scored"),
    # Scorecard inputs
    ("DXY",      "DX-Y.NYB",     "DXY dollar index — scored"),
    ("TIP",      "TIP",          "iShares 7-10 Year TIPS ETF (real-yield proxy) — scored"),
    ("INDIAVIX", "^INDIAVIX",    "India volatility index — scored"),
    ("VIX",      "^VIX",         "US VIX (secondary) — scored"),
]

# Scorecard regime collapse thresholds
ACCUMULATE_MIN_TAILWINDS = 4   # 4-5 tailwinds → ACCUMULATE
NEUTRAL_MIN_TAILWINDS    = 2   # 2-3 → NEUTRAL ; 0-1 → potentially WAIT
PRICE_EXTENSION_LUMP_SUM_SIGMA = 2.0   # > +2σ above 200DMA = headwind for lump sums

# Hardcoded GOLDLENDER thesis-breaker level (from stocks/GOLDLENDER.md)
GOLDLENDER_THESIS_BREAKER_USD = 4000.0

# How many bars (~1y of trading days) we need to compute the rolling premium percentile
PREMIUM_PCTILE_WINDOW = 252

# Grams per troy ounce — physical constant
GRAMS_PER_TROY_OZ = 31.1035


# ---------------------------------------------------------------------------
# Helpers: math primitives
# ---------------------------------------------------------------------------

def _last_close(df: pd.DataFrame) -> Optional[float]:
    """Return the most recent close price, or None if the frame is empty."""
    if df is None or len(df) == 0:
        return None
    return float(df["Close"].iloc[-1])


def _pct_change_n_days(df: pd.DataFrame, days: int) -> Optional[float]:
    """
    Percent change between the latest close and the close `days` calendar days
    ago. Returns None if there isn't enough history.
    """
    if df is None or len(df) < 2:
        return None
    today = df.index[-1]
    cutoff = today - pd.Timedelta(days=days)
    older = df[df.index <= cutoff]
    if len(older) == 0:
        return None
    old_price = float(older["Close"].iloc[-1])
    new_price = float(df["Close"].iloc[-1])
    if old_price == 0:
        return None
    return (new_price / old_price - 1.0) * 100.0


def _pct_change_ytd(df: pd.DataFrame) -> Optional[float]:
    """Percent change from January 1 of the current year."""
    if df is None or len(df) < 2:
        return None
    year_start = pd.Timestamp(df.index[-1].year, 1, 1)
    ytd = df[df.index >= year_start]
    if len(ytd) < 2:
        return None
    return (float(ytd["Close"].iloc[-1]) / float(ytd["Close"].iloc[0]) - 1.0) * 100.0


def _slope_30d_pct(df: pd.DataFrame) -> Optional[float]:
    """30-day percent change — used as a "slope direction" proxy for scorecard factors."""
    return _pct_change_n_days(df, 30)


def _direction_label(slope_pct: Optional[float], flat_threshold: float = 0.5) -> str:
    """Convert a slope to a human label so the LLM never has to interpret raw numbers."""
    if slope_pct is None:
        return "unknown"
    if slope_pct > flat_threshold:
        return "rising"
    if slope_pct < -flat_threshold:
        return "falling"
    return "flat"


def _zscore_vs_ma(df: pd.DataFrame, window: int) -> Optional[float]:
    """
    Z-score of the latest close vs its trailing-`window` moving average.
    Standard deviation is computed over the same window.
    """
    if df is None or len(df) < window:
        return None
    closes = df["Close"].iloc[-window:]
    ma = closes.mean()
    sd = closes.std()
    if sd == 0 or pd.isna(sd):
        return None
    return float((float(df["Close"].iloc[-1]) - ma) / sd)


def _moving_average(df: pd.DataFrame, window: int) -> Optional[float]:
    if df is None or len(df) < window:
        return None
    return float(df["Close"].iloc[-window:].mean())


def _rolling_corr(a: pd.Series, b: pd.Series, window: int) -> Optional[float]:
    """Rolling Pearson correlation of the most recent `window` overlapping bars."""
    if a is None or b is None:
        return None
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(joined) < window:
        return None
    tail = joined.iloc[-window:]
    corr = tail.iloc[:, 0].corr(tail.iloc[:, 1])
    if pd.isna(corr):
        return None
    return float(corr)


def _annualized_volatility(df: pd.DataFrame, window: int) -> Optional[float]:
    """Annualized realized volatility of daily returns over the last `window` days."""
    if df is None or len(df) < window + 1:
        return None
    returns = df["Close"].pct_change().dropna().iloc[-window:]
    if len(returns) == 0:
        return None
    return float(returns.std() * np.sqrt(252) * 100.0)


def _drawdown_from_52w_high(df: pd.DataFrame) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Returns (drawdown_pct_from_high, high_52w, low_52w)."""
    if df is None or len(df) == 0:
        return None, None, None
    one_year_ago = df.index[-1] - pd.Timedelta(days=365)
    window = df[df.index >= one_year_ago]
    if len(window) == 0:
        return None, None, None
    high = float(window["High"].max() if "High" in window.columns else window["Close"].max())
    low = float(window["Low"].min() if "Low" in window.columns else window["Close"].min())
    curr = float(df["Close"].iloc[-1])
    if high == 0:
        return None, high, low
    dd = (curr / high - 1.0) * 100.0
    return dd, high, low


# ---------------------------------------------------------------------------
# Helpers: price-bundle builder
# ---------------------------------------------------------------------------

def _price_block(df: pd.DataFrame) -> dict:
    """Build the per-price block: value + multi-window % changes + 52w distance."""
    value = _last_close(df)
    dd_pct, high_52w, _low_52w = _drawdown_from_52w_high(df)
    return {
        "value": round(value, 4) if value is not None else None,
        "today_pct": _round_pct(_pct_change_n_days(df, 1)),
        "week_pct": _round_pct(_pct_change_n_days(df, 7)),
        "month_pct": _round_pct(_pct_change_n_days(df, 30)),
        "ytd_pct": _round_pct(_pct_change_ytd(df)),
        "from_52w_high_pct": _round_pct(dd_pct),
        "high_52w": round(high_52w, 4) if high_52w is not None else None,
    }


def _round_pct(v: Optional[float]) -> Optional[float]:
    """Round a percent value to 2 decimal places, preserving None."""
    if v is None:
        return None
    return round(v, 2)


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------

def compute_scorecard(
    series: dict[str, pd.DataFrame],
    cfg: GoldConfig,
) -> dict:
    """
    Compute all 5 scorecard factors. Returns the full scorecard sub-bundle:
      {
        "tailwinds_count": int,
        "max_count": 5,
        "factors": { ... per-factor details ... }
      }
    """
    factors: dict[str, dict] = {}
    tailwinds = 0

    # ---- Factor 1: TIP ETF direction (real-yield proxy) ----
    tip_df = series.get("TIP")
    tip_slope = _slope_30d_pct(tip_df)
    tip_dir = _direction_label(tip_slope, flat_threshold=0.3)
    tip_tailwind = tip_dir == "rising"   # rising TIP = falling real yields = gold tailwind
    if tip_tailwind:
        tailwinds += 1
    factors["tip_etf"] = {
        "value": round(_last_close(tip_df), 2) if _last_close(tip_df) is not None else None,
        "slope_30d_pct": _round_pct(tip_slope),
        "label": tip_dir,
        "interpretation": (
            "real yields falling" if tip_dir == "rising"
            else "real yields rising" if tip_dir == "falling"
            else "real yields flat"
        ),
        "tailwind": tip_tailwind,
    }

    # ---- Factor 2: DXY direction ----
    dxy_df = series.get("DXY")
    dxy_slope = _slope_30d_pct(dxy_df)
    dxy_dir = _direction_label(dxy_slope, flat_threshold=0.3)
    dxy_200dma = _moving_average(dxy_df, 200)
    dxy_value = _last_close(dxy_df)
    above_200dma = (
        bool(dxy_value > dxy_200dma) if (dxy_value is not None and dxy_200dma is not None) else None
    )
    dxy_tailwind = dxy_dir == "falling"   # falling DXY = gold tailwind
    if dxy_tailwind:
        tailwinds += 1
    factors["dxy"] = {
        "value": round(dxy_value, 2) if dxy_value is not None else None,
        "slope_30d_pct": _round_pct(dxy_slope),
        "label": dxy_dir,
        "above_200dma": above_200dma,
        "tailwind": dxy_tailwind,
    }

    # ---- Factor 3: Equity volatility regime (INDIAVIX primary, VIX secondary) ----
    indiavix_df = series.get("INDIAVIX")
    vix_df = series.get("VIX")
    indiavix_value = _last_close(indiavix_df)
    indiavix_30d_avg = _moving_average(indiavix_df, 30)
    vix_value = _last_close(vix_df)
    elevated = (
        bool(indiavix_value is not None and indiavix_value > 20)
    )
    rising = (
        bool(indiavix_value is not None and indiavix_30d_avg is not None
             and indiavix_value > indiavix_30d_avg)
    )
    vix_tailwind = elevated and rising
    if vix_tailwind:
        tailwinds += 1
    factors["vix_regime"] = {
        "indiavix": round(indiavix_value, 2) if indiavix_value is not None else None,
        "indiavix_30d_avg": round(indiavix_30d_avg, 2) if indiavix_30d_avg is not None else None,
        "vix_us": round(vix_value, 2) if vix_value is not None else None,
        "elevated": elevated,
        "rising": rising,
        "tailwind": vix_tailwind,
    }

    # ---- Factor 4: Price extension (lump-sum gate, NOT a SIP gate) ----
    goldbees_df = series.get("GOLDBEES")
    z200 = _zscore_vs_ma(goldbees_df, 200)
    z50 = _zscore_vs_ma(goldbees_df, 50)
    extended_for_lump_sum = (
        bool(z200 is not None and z200 > PRICE_EXTENSION_LUMP_SUM_SIGMA)
    )
    # Tailwind: NOT extended for lump sums (within ±2σ of 200DMA OR within ±1σ of 50DMA)
    not_extended = (
        (z200 is not None and abs(z200) <= PRICE_EXTENSION_LUMP_SUM_SIGMA)
        or (z50 is not None and abs(z50) <= 1.0)
    )
    pe_tailwind = not_extended and not extended_for_lump_sum
    if pe_tailwind:
        tailwinds += 1
    factors["price_extension"] = {
        "goldbees_zscore_vs_200dma": round(z200, 2) if z200 is not None else None,
        "goldbees_zscore_vs_50dma": round(z50, 2) if z50 is not None else None,
        "extended_for_lump_sum": extended_for_lump_sum,
        "extended_for_sip": False,   # SIP is never gated by extension — by design
        "tailwind": pe_tailwind,
        "note": _price_extension_note(z200, extended_for_lump_sum),
    }

    # ---- Factor 5: India premium percentile (rolling 1Y percentile of self) ----
    premium_block = _india_premium_factor(
        goldbees_df=goldbees_df,
        gold_df=series.get("GOLD"),
        inr_df=series.get("INR"),
        cfg=cfg,
    )
    if premium_block.get("tailwind"):
        tailwinds += 1
    factors["india_premium"] = premium_block

    return {
        "tailwinds_count": tailwinds,
        "max_count": 5,
        "factors": factors,
    }


def _price_extension_note(z200: Optional[float], extended: bool) -> str:
    """Human-readable note for the price extension factor."""
    if z200 is None:
        return "insufficient history"
    if extended:
        return f"{z200:.1f}σ above 200DMA — extended; hold off lump sums; SIP unaffected"
    if z200 > 1.0:
        return f"{z200:.1f}σ above 200DMA — slightly elevated for lump sums; SIP unaffected"
    if z200 < -1.0:
        return f"{z200:.1f}σ below 200DMA — pullback; lump sums favourable"
    return f"{z200:.1f}σ from 200DMA — neutral range"


def _india_premium_factor(
    goldbees_df: Optional[pd.DataFrame],
    gold_df: Optional[pd.DataFrame],
    inr_df: Optional[pd.DataFrame],
    cfg: GoldConfig,
) -> dict:
    """
    India premium = (GOLDBEES NAV per gram) / (international gold per gram in ₹) - 1.

    We compute this as a daily series for the last ~1y, then take the percentile of
    today's value within that series. This is **percentile-of-self**, not absolute
    threshold — immune to customs duty changes.

    Tailwind: today's percentile <= 50 (premium below the trailing-1Y median).
    Headwind: today's percentile >= 75 (premium festival-spiked).
    """
    out: dict = {
        "premium_pct": None,
        "premium_1y_percentile": None,
        "label": "unknown",
        "tailwind": False,
    }
    if goldbees_df is None or gold_df is None or inr_df is None:
        return out

    # Build a per-day ratio series: GOLDBEES NAV / international ₹/gram.
    # The absolute ratio is unitless (not retail "premium %") because the units
    # don't align — but the *percentile-of-self* over a rolling window IS valid:
    # it captures whether GOLDBEES is rich or cheap relative to its own history
    # against international gold. Rank is invariant under any positive scaling,
    # so we don't need a multiplier or duty assumption baked in.
    gold_per_gram_inr = (gold_df["Close"] / GRAMS_PER_TROY_OZ) * inr_df["Close"]

    aligned = pd.concat(
        [goldbees_df["Close"].rename("nav"), gold_per_gram_inr.rename("intl")],
        axis=1, join="inner",
    ).dropna()
    if len(aligned) < 30:
        return out

    ratio_series = aligned["nav"] / aligned["intl"]
    today_ratio = float(ratio_series.iloc[-1])

    # Use the last `PREMIUM_PCTILE_WINDOW` bars (≈1y of trading days) for the percentile
    # AND for the human-readable display (deviation from 1Y median, in %).
    window = ratio_series.iloc[-PREMIUM_PCTILE_WINDOW:]
    rank = (window <= today_ratio).sum()
    pctile = int(round(100.0 * rank / len(window)))
    median_1y = float(window.median())
    today_premium = (today_ratio / median_1y - 1.0) * 100.0 if median_1y else 0.0

    if pctile <= 50:
        label = "low — favourable"
        tailwind = True
    elif pctile >= 75:
        label = "elevated — festival-spiked"
        tailwind = False
    else:
        label = "normal"
        tailwind = True

    out.update({
        "premium_pct": round(today_premium, 2),
        "premium_1y_percentile": pctile,
        "label": label,
        "tailwind": tailwind,
    })
    return out


def classify_regime(
    tailwinds_count: int,
    extended_for_lump_sum: bool,
) -> str:
    """
    Collapse the 5-factor scorecard to one of three states.

    Pure function — no I/O. Used by both fetch_gold_snapshot and the
    end-to-end test that overrides the scorecard.

    Rules (deliberately coarse — see metals_instructions.md §3):
      - 4-5 tailwinds          → ACCUMULATE
      - 2-3 tailwinds          → NEUTRAL
      - 0-1 tailwinds AND price extended for lump sums → WAIT
      - 0-1 tailwinds otherwise → NEUTRAL (don't punish low-conviction
        backdrop unless price is also extended)
    """
    if tailwinds_count >= ACCUMULATE_MIN_TAILWINDS:
        return "ACCUMULATE"
    if tailwinds_count >= NEUTRAL_MIN_TAILWINDS:
        return "NEUTRAL"
    if extended_for_lump_sum:
        return "WAIT"
    return "NEUTRAL"


# ---------------------------------------------------------------------------
# Zone evaluation (₹/gram of 24K, derived from GOLDBEES NAV)
# ---------------------------------------------------------------------------

def _zone_block(cfg: GoldConfig, current_inr_per_gram: float) -> list[dict]:
    """
    Distance from current ₹/gram price to every user-defined zone.
    Sorted by absolute distance; the closest gets is_nearest=True.
    """
    out: list[dict] = []
    if current_inr_per_gram <= 0 or not cfg.zones:
        return out

    for z in cfg.zones:
        # Use the zone midpoint for the distance calculation
        mid = (z.lower + z.upper) / 2.0
        dist_inr = mid - current_inr_per_gram
        dist_pct = (dist_inr / current_inr_per_gram) * 100.0

        if z.lower <= current_inr_per_gram <= z.upper:
            status = "inside_zone"
        elif current_inr_per_gram < z.lower:
            status = "above_zone"   # current price is BELOW the zone (we're below floor)
        else:
            status = "below_zone"   # current price is ABOVE the zone (zone is below us)

        out.append({
            "type": z.alert_type,
            "signal": z.signal,
            "lower_inr_per_gram": z.lower,
            "upper_inr_per_gram": z.upper,
            "price_str": z.price_str,
            "current_distance_pct": round(dist_pct, 2),
            "current_distance_inr": round(dist_inr, 0),
            "status": status,
            "is_nearest": False,
        })

    # Mark the closest zone (smallest absolute distance)
    if out:
        nearest_idx = min(range(len(out)), key=lambda i: abs(out[i]["current_distance_inr"]))
        out[nearest_idx]["is_nearest"] = True

    return out


def check_gold_zones(
    cfg: GoldConfig,
    prev_inr_per_gram: Optional[float],
    curr_inr_per_gram: float,
) -> list[tuple]:
    """
    Reuse the same crossing semantics as stock alerts (engine._crosses)
    but operate on the 24K ₹/gram price.

    Returns a list of (AlertLevel, message) tuples for zones that just crossed.
    The caller is responsible for cooldown checks and Telegram delivery.
    """
    from .engine import AlertEngine   # local import to avoid circular dep

    crossings = []
    if prev_inr_per_gram is None or prev_inr_per_gram <= 0 or curr_inr_per_gram <= 0:
        return crossings

    for zone in cfg.zones:
        if AlertEngine._crosses(zone, prev_inr_per_gram, curr_inr_per_gram):
            crossings.append((zone, zone.message))
    return crossings


# ---------------------------------------------------------------------------
# Calendar (festival days-until)
# ---------------------------------------------------------------------------

def _calendar_block(cfg: GoldConfig, today: date) -> dict:
    """Compute days-until for the next festival + the remaining list."""
    upcoming = [f for f in cfg.festivals if f.event_date >= today]
    upcoming.sort(key=lambda f: f.event_date)

    if not upcoming:
        return {"next_event": None, "all_events_remaining": []}

    nxt = upcoming[0]
    days = (nxt.event_date - today).days
    premium_risk = (
        "elevated" if days <= 30 and "wedding" in nxt.demand_implication.lower()
        else "elevated" if days <= 14
        else "normal"
    )

    return {
        "next_event": {
            "name": nxt.name,
            "date": nxt.event_date.isoformat(),
            "days_until": days,
            "demand_implication": nxt.demand_implication,
            "premium_risk": premium_risk,
        },
        "all_events_remaining": [
            {"name": f.name, "date": f.event_date.isoformat(),
             "demand_implication": f.demand_implication}
            for f in upcoming
        ],
    }


# ---------------------------------------------------------------------------
# Correlations + volatility
# ---------------------------------------------------------------------------

def _correlations_block(series: dict[str, pd.DataFrame]) -> dict:
    """30-day rolling correlations between GOLDBEES and each scorecard factor."""
    goldbees = series.get("GOLDBEES")
    if goldbees is None or len(goldbees) < 30:
        return {
            "goldbees_vs_tip": None, "goldbees_vs_dxy": None,
            "goldbees_vs_vix": None, "goldbees_vs_usdinr": None,
            "unusual_decoupling_flag": False, "decoupling_note": None,
        }
    g_returns = goldbees["Close"].pct_change().dropna()

    def corr_to(other_key: str) -> Optional[float]:
        other = series.get(other_key)
        if other is None or len(other) < 30:
            return None
        return _rolling_corr(g_returns, other["Close"].pct_change().dropna(), window=30)

    corrs = {
        "goldbees_vs_tip":    corr_to("TIP"),
        "goldbees_vs_dxy":    corr_to("DXY"),
        "goldbees_vs_vix":    corr_to("INDIAVIX"),
        "goldbees_vs_usdinr": corr_to("INR"),
    }
    # Flag unusual decoupling: gold vs DXY should typically be NEGATIVE
    # (-0.3 to -0.5 over long windows). Positive correlation = unusual.
    decoupling = False
    note = None
    if corrs["goldbees_vs_dxy"] is not None and corrs["goldbees_vs_dxy"] > 0.2:
        decoupling = True
        note = (
            f"Gold and DXY both rising together (30D corr {corrs['goldbees_vs_dxy']:.2f}) "
            f"— historically inverse. Likely flight-to-safety regime; both are catching bid."
        )

    return {
        **{k: round(v, 2) if v is not None else None for k, v in corrs.items()},
        "unusual_decoupling_flag": decoupling,
        "decoupling_note": note,
    }


def _volatility_block(series: dict[str, pd.DataFrame]) -> dict:
    """Realized vol + 52w extremes for GOLDBEES."""
    goldbees = series.get("GOLDBEES")
    if goldbees is None or len(goldbees) == 0:
        return {}
    dd, high, low = _drawdown_from_52w_high(goldbees)
    return {
        "goldbees_realized_30d_annualized_pct": _round_pct(_annualized_volatility(goldbees, 30)),
        "goldbees_realized_90d_annualized_pct": _round_pct(_annualized_volatility(goldbees, 90)),
        "goldbees_drawdown_from_52w_high_pct": _round_pct(dd),
        "goldbees_52w_high": round(high, 2) if high is not None else None,
        "goldbees_52w_low": round(low, 2) if low is not None else None,
    }


# ---------------------------------------------------------------------------
# GOLDLENDER linkage (decoupled, single-direction — no cross-writes)
# ---------------------------------------------------------------------------

def _goldlender_linkage(international_usd_per_oz: Optional[float]) -> dict:
    """
    Mechanical thesis-status field for GOLDLENDER. Convergence layer reads this
    and decides how to surface it. Gold tracker never reaches into GOLDLENDER
    alerts.
    """
    if international_usd_per_oz is None:
        return {"thesis_status": "unknown"}

    above = international_usd_per_oz >= GOLDLENDER_THESIS_BREAKER_USD
    distance_pct = (international_usd_per_oz / GOLDLENDER_THESIS_BREAKER_USD - 1.0) * 100.0

    if above:
        interpretation = (
            f"Gold {distance_pct:.1f}% above ${GOLDLENDER_THESIS_BREAKER_USD:.0f}/oz "
            f"thesis-breaker — GOLDLENDER collateral base inflated, AUM tailwind active"
        )
        status = "tailwind_active"
    else:
        interpretation = (
            f"Gold {abs(distance_pct):.1f}% BELOW ${GOLDLENDER_THESIS_BREAKER_USD:.0f}/oz "
            f"thesis-breaker — GOLDLENDER collateral base impaired, thesis under pressure"
        )
        status = "thesis_breached"

    return {
        "gold_at_or_above_thesis_breaker": above,
        "thesis_breaker_level_usd": GOLDLENDER_THESIS_BREAKER_USD,
        "current_international_usd": round(international_usd_per_oz, 2),
        "distance_to_breaker_pct": round(distance_pct, 2),
        "thesis_status": status,
        "mechanical_interpretation": interpretation,
    }


# ---------------------------------------------------------------------------
# Regime history (rolling 30-day classifier)
# ---------------------------------------------------------------------------

def _regime_history_30d(
    series: dict[str, pd.DataFrame],
    cfg: GoldConfig,
) -> list[dict]:
    """
    Recompute the regime classifier for each of the last 30 trading days
    using each day as a "synthetic today". This gives the LLM a regime
    timeline without it having to reason about transitions itself.

    For efficiency we only run the lightweight scorecard subset on each day —
    the heavier rolling-correlations and bundle blocks are NOT recomputed.
    """
    goldbees = series.get("GOLDBEES")
    if goldbees is None or len(goldbees) < 200:
        return []

    history: list[dict] = []
    for offset in range(min(30, len(goldbees) - 200)):
        # Slice each series to "as of N days ago"
        slice_idx = -offset - 1 if offset > 0 else None
        sliced = {k: (v.iloc[:slice_idx] if v is not None and len(v) > 0 else v)
                  for k, v in series.items()}
        sc = compute_scorecard(sliced, cfg)
        ext = sc["factors"]["price_extension"].get("extended_for_lump_sum", False)
        regime = classify_regime(sc["tailwinds_count"], ext)
        bar_date = goldbees.index[slice_idx if slice_idx is not None else -1].date()
        history.append({
            "date": bar_date.isoformat(),
            "regime": regime,
            "tailwinds": sc["tailwinds_count"],
        })

    history.reverse()  # oldest first
    return history


# ---------------------------------------------------------------------------
# Top-level fetch + bundle assembly
# ---------------------------------------------------------------------------

def fetch_gold_snapshot(cfg: GoldConfig) -> dict:
    """
    Fetch all 7 yfinance series, compute the full bundle, write both
    `data/gold_snapshot.json` (compact, for the polling bot) and
    `data/gold_analysis_bundle.json` (full, for the LLM/convergence layer).

    Returns the full bundle dict so callers can use it without re-reading disk.
    """
    series: dict[str, pd.DataFrame] = {}
    for cache_ticker, yf_symbol, _role in GOLD_SYMBOLS:
        try:
            df = load_ohlc_cached(cache_ticker, yf_symbol, period="2y")
            if df is None or len(df) == 0:
                logger.warning(f"Gold: empty fetch for {yf_symbol}")
                series[cache_ticker] = pd.DataFrame()
            else:
                series[cache_ticker] = df
        except Exception as e:
            logger.error(f"Gold: failed to fetch {yf_symbol}: {e}")
            series[cache_ticker] = pd.DataFrame()

    # ---- Prices block (5 derived views) ----
    gold_df = series.get("GOLD")
    goldbees_df = series.get("GOLDBEES")
    inr_df = series.get("INR")

    intl_usd_per_oz = _last_close(gold_df)
    goldbees_nav = _last_close(goldbees_df)
    usdinr = _last_close(inr_df)

    # Physical 24K ₹/gram (Indian retail wholesale, duty-paid) — computed
    # directly from international + FX + customs duty as a fresh time series.
    # This is independent of GOLDBEES NAV, NOT derived from it. See gold.md
    # "Physical 24K ₹/gram — How It's Computed" for the why.
    physical_df = _build_physical_24k_series(gold_df, inr_df, cfg.customs_duty_pct)
    physical_inr_per_gram = _last_close(physical_df)
    if physical_inr_per_gram is not None:
        physical_inr_per_gram = round(physical_inr_per_gram, 2)

    physical_block = _price_block(physical_df)
    physical_block["derivation"] = (
        f"(international_USD_per_oz × USDINR / {GRAMS_PER_TROY_OZ}) × "
        f"(1 + {cfg.customs_duty_pct}%) — duty-paid Indian retail spot"
    )

    prices = {
        "international_per_oz_usd": _price_block(gold_df),
        "goldbees_nav_inr": _price_block(goldbees_df),
        "physical_24k_inr_per_gram": physical_block,
        "usdinr": _price_block(inr_df),
    }

    # ---- Scorecard ----
    scorecard = compute_scorecard(series, cfg)

    # ---- Regime classification + 30D history ----
    extended = scorecard["factors"]["price_extension"].get("extended_for_lump_sum", False)
    current_regime = classify_regime(scorecard["tailwinds_count"], extended)

    history = _regime_history_30d(series, cfg)
    previous_regime = history[-2]["regime"] if len(history) >= 2 else None
    transition_today = previous_regime is not None and previous_regime != current_regime
    transition_direction = _transition_direction(previous_regime, current_regime)

    regime_block = {
        "current": current_regime,
        "previous": previous_regime,
        "transition_today": transition_today,
        "transition_direction": transition_direction,
        "history_30d": history,
    }

    # ---- Zones ----
    zones = _zone_block(cfg, physical_inr_per_gram or 0.0)

    # ---- Calendar ----
    calendar = _calendar_block(cfg, today=datetime.now(IST).date())

    # ---- Correlations + volatility ----
    correlations = _correlations_block(series)
    vol_block = _volatility_block(series)

    # ---- GOLDLENDER linkage ----
    goldlender = _goldlender_linkage(intl_usd_per_oz)

    # ---- Assemble the full bundle ----
    bundle = {
        "schema_version": "1.0",
        "as_of": datetime.now(IST).isoformat(),
        "config": {
            "target_allocation_pct": cfg.target_allocation_pct,
            "customs_duty_pct": cfg.customs_duty_pct,
        },
        "prices": prices,
        "scorecard": scorecard,
        "regime": regime_block,
        "zones": zones,
        "calendar": calendar,
        "correlations_30d": correlations,
        "volatility_and_drawdown": vol_block,
        "goldlender_linkage": goldlender,
        "narrative_context_pointer": str(GOLD_NARRATIVE_FILE),
    }

    # ---- Persist both files ----
    GOLD_BUNDLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOLD_BUNDLE_FILE.write_text(json.dumps(bundle, indent=2, default=str), encoding="utf-8")

    # The compact snapshot is a thin slice — the polling bot reads this for
    # transition detection and Telegram message composition. The full bundle
    # is for the LLM and convergence layer.
    snapshot = {
        "schema_version": "1.0",
        "as_of": bundle["as_of"],
        "physical_24k_inr_per_gram": physical_inr_per_gram,
        "goldbees_nav_inr": goldbees_nav,
        "international_per_oz_usd": intl_usd_per_oz,
        "usdinr": usdinr,
        "tailwinds_count": scorecard["tailwinds_count"],
        "regime": regime_block,
        "nearest_zone": next((z for z in zones if z.get("is_nearest")), None),
    }
    GOLD_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOLD_SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")

    logger.info(
        f"Gold: {scorecard['tailwinds_count']}/5 tailwinds → {current_regime} "
        f"(physical ₹{physical_inr_per_gram}/g, intl ${intl_usd_per_oz}/oz)"
    )
    return bundle


def _build_physical_24k_series(
    gold_df: Optional[pd.DataFrame],
    inr_df: Optional[pd.DataFrame],
    customs_duty_pct: float,
) -> Optional[pd.DataFrame]:
    """
    Synthesize a daily Indian retail 24K ₹/gram series from international gold
    (USD/oz) and USDINR. Aligns the two series on overlapping dates and applies
    the customs duty multiplier. Returns a DataFrame with a "Close" column so
    it can be passed straight to _price_block().
    """
    if gold_df is None or inr_df is None or gold_df.empty or inr_df.empty:
        return None

    aligned = pd.concat(
        [gold_df["Close"].rename("usd"), inr_df["Close"].rename("inr")],
        axis=1, join="inner",
    ).dropna()
    if aligned.empty:
        return None

    duty_mult = 1.0 + (customs_duty_pct / 100.0)
    physical_close = (aligned["usd"] / GRAMS_PER_TROY_OZ) * aligned["inr"] * duty_mult
    return pd.DataFrame({"Close": physical_close})


def _transition_direction(prev: Optional[str], curr: str) -> str:
    """Label a regime transition as 'upgrade', 'downgrade', or 'none'."""
    rank = {"WAIT": 0, "NEUTRAL": 1, "ACCUMULATE": 2}
    if prev is None or prev == curr:
        return "none"
    if rank.get(curr, 1) > rank.get(prev, 1):
        return "upgrade"
    return "downgrade"


# ---------------------------------------------------------------------------
# Telegram message formatting
# ---------------------------------------------------------------------------

def format_gold_telegram(
    bundle: dict,
    narrative_quotes: Optional[list[dict]] = None,
    transition_kind: str = "regime",   # "regime" | "weekly" | "zone"
) -> str:
    """
    Compose a human-readable Telegram message body from the bundle.

    transition_kind:
      - "regime"   → header is the regime change ("NEUTRAL → ACCUMULATE")
      - "weekly"   → Sunday digest header (current state recap)
      - "zone"     → zone-crossing header (the zone message is appended outside)

    narrative_quotes: optional list of dicts read from gold_narrative.json,
      each with keys {date, quote, source}. May be None if narrative file
      doesn't exist or is empty.
    """
    regime = bundle.get("regime", {})
    prices = bundle.get("prices", {})
    scorecard = bundle.get("scorecard", {})
    zones = bundle.get("zones", [])
    calendar = bundle.get("calendar", {})

    # Header
    if transition_kind == "regime":
        prev = regime.get("previous") or "?"
        curr = regime.get("current") or "?"
        header = f"🪙 GOLD — REGIME CHANGE: {prev} → {curr}"
    elif transition_kind == "weekly":
        header = f"🪙 GOLD — WEEKLY DIGEST ({regime.get('current', 'UNKNOWN')})"
    else:
        header = f"🪙 GOLD — ZONE CROSSED ({regime.get('current', 'UNKNOWN')})"

    today_str = datetime.now(IST).strftime("%Y-%m-%d")

    lines = [header, today_str, ""]

    # Prices block
    physical = prices.get("physical_24k_inr_per_gram", {})
    goldbees = prices.get("goldbees_nav_inr", {})
    intl = prices.get("international_per_oz_usd", {})
    inr_block = prices.get("usdinr", {})

    def _fmt_pct(p):
        if p is None:
            return "—"
        return f"{p:+.1f}%"

    lines.append(
        f"PHYSICAL (24K):  ₹{physical.get('value', '—')}/g  "
        f"({_fmt_pct(physical.get('today_pct'))} today, {_fmt_pct(physical.get('ytd_pct'))} YTD)"
    )
    lines.append(
        f"TRADABLE (ETF):  GOLDBEES ₹{goldbees.get('value', '—')}  "
        f"({_fmt_pct(goldbees.get('today_pct'))} today)"
    )
    lines.append(
        f"INTERNATIONAL:   ${intl.get('value', '—')}/oz  "
        f"({_fmt_pct(intl.get('today_pct'))} today, {_fmt_pct(intl.get('ytd_pct'))} YTD)"
    )
    lines.append(
        f"USDINR:          {inr_block.get('value', '—')}  "
        f"({_fmt_pct(inr_block.get('today_pct'))} today)"
    )
    lines.append("")

    # Scorecard
    lines.append(f"Scorecard ({scorecard.get('tailwinds_count', 0)}/{scorecard.get('max_count', 5)} tailwinds):")
    factors = scorecard.get("factors", {})
    lines.append(_factor_line("TIP ETF", factors.get("tip_etf"), "tip"))
    lines.append(_factor_line("DXY", factors.get("dxy"), "dxy"))
    lines.append(_factor_line("INDIAVIX", factors.get("vix_regime"), "vix"))
    lines.append(_factor_line("Price extension", factors.get("price_extension"), "extension"))
    lines.append(_factor_line("India premium", factors.get("india_premium"), "premium"))
    lines.append("")

    # Recent narrative (LLM-extracted from digests)
    if narrative_quotes:
        lines.append("Recent narrative (from Dalal Street digests):")
        for q in narrative_quotes[-3:]:   # last 3 quotes
            lines.append(f"  • {q.get('date', '')}: \"{q.get('quote', '')}\"")
        lines.append("")

    # Nearest zone
    nearest = next((z for z in zones if z.get("is_nearest")), None)
    if nearest:
        lines.append(
            f"Nearest zone: {nearest['type']} {nearest['price_str']}/g "
            f"({nearest['current_distance_pct']:+.1f}% from current)"
        )

    # Next festival
    next_event = (calendar or {}).get("next_event")
    if next_event:
        lines.append(
            f"Next event:   {next_event['name']} {next_event['date']} "
            f"({next_event['days_until']} days) — {next_event['demand_implication']}"
        )

    # Action footer
    if regime.get("current") == "ACCUMULATE":
        lines.append("")
        lines.append("→ Add to gold position this week.")
    elif regime.get("current") == "WAIT":
        lines.append("")
        lines.append("→ Continue SIP. Hold off on additional lump sums.")

    return "\n".join(lines)


def _factor_line(label: str, factor: Optional[dict], kind: str) -> str:
    """One line of the scorecard block, with checkmark/circle and details."""
    if factor is None:
        return f"  ? {label}: unknown"
    mark = "✓" if factor.get("tailwind") else "○"

    if kind == "tip":
        slope = factor.get("slope_30d_pct")
        slope_str = f"{slope:+.1f}%" if slope is not None else "—"
        return f"  {mark} TIP {factor.get('label', 'unknown')} ({slope_str} 30D) → {factor.get('interpretation', '')}"
    if kind == "dxy":
        slope = factor.get("slope_30d_pct")
        slope_str = f"{slope:+.1f}%" if slope is not None else "—"
        return f"  {mark} DXY {factor.get('value', '—')}, {factor.get('label', 'unknown')} ({slope_str} 30D)"
    if kind == "vix":
        elev = "elevated" if factor.get("elevated") else "calm"
        return (
            f"  {mark} INDIAVIX {factor.get('indiavix', '—')}, {elev} → "
            f"{'risk-off backdrop' if factor.get('tailwind') else 'risk-on backdrop'}"
        )
    if kind == "extension":
        return f"  {mark} GOLDBEES {factor.get('note', '')}"
    if kind == "premium":
        pct = factor.get("premium_1y_percentile")
        return (
            f"  {mark} India premium {pct}th percentile (1Y) → {factor.get('label', 'unknown')}"
        )
    return f"  {mark} {label}"


# ---------------------------------------------------------------------------
# Narrative file reader (read-only — Python never writes this)
# ---------------------------------------------------------------------------

def load_gold_narrative() -> list[dict]:
    """
    Read data/gold_narrative.json (written by the autonomous loop).

    Returns the rolling list of {date, quote, source} entries, or empty
    list if the file doesn't exist or is malformed. NEVER writes to this file.
    """
    if not GOLD_NARRATIVE_FILE.exists():
        return []
    try:
        data = json.loads(GOLD_NARRATIVE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data.get("quotes", [])
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read gold narrative: {e}")
    return []
