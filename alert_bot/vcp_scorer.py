"""
VCP (Volatility Contraction Pattern) composite scorer.

Wraps the 5 VCP calculators from indian-trading-skills/ into a single
compute_vcp_bundle() call and writes analysis/{TICKER}_vcp.json.

Called nightly by compute_vcp.py. Never called in the hot alert path —
confidence.py _score_vcp() reads the JSON sidecar instead.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Add the nse-vcp-screener scripts directory to sys.path so we can import
# the 5 calculators and the scorer from that skill's package.
_CALCS = (Path(__file__).parent.parent
          / "indian-trading-skills" / "skills" / "nse-vcp-screener" / "scripts")
if str(_CALCS) not in sys.path:
    sys.path.insert(0, str(_CALCS))

from calculators.trend_template_calculator import calculate_trend_template
from calculators.vcp_pattern_calculator import calculate_vcp
from calculators.volume_pattern_calculator import calculate_volume_pattern
from calculators.pivot_proximity_calculator import calculate_pivot_proximity
from calculators.relative_strength_calculator import calculate_relative_strength
from scorer import calculate_composite_score

# Written relative to cwd (the repo root when the bot is running).
ANALYSIS_DIR = Path("analysis")


@dataclass
class VcpFactorScore:
    """Return type for score_factor() — compatible with confidence.py FactorScore."""
    score: int   # -1 / 0 / +1
    label: str


def compute_vcp_bundle(df: pd.DataFrame, curr_price: float,
                       ticker: str,
                       bench_df: pd.DataFrame | None = None) -> dict:
    """
    Run all 5 VCP calculators and return the composite bundle dict.

    Parameters
    ----------
    df         : OHLCV DataFrame for the stock (sorted ascending by date).
    curr_price : Latest closing/live price for pivot proximity calculation.
    ticker     : NSE ticker string (used for logging / bundle metadata).
    bench_df   : Optional Nifty 50 OHLCV DataFrame for relative strength.
                 When None, the RS calculator uses zero-benchmark returns.

    Returns
    -------
    dict with keys: ticker, generated_at, composite_score, quality, is_vcp,
                    pivot, stage, dry_up_ratio, rs_value, components.
    """
    # 1. Minervini's 7-point trend template — is the stock in a Stage 2 uptrend?
    trend = calculate_trend_template(df)

    # 2. VCP pattern detection — are contractions tightening?
    vcp = calculate_vcp(df)

    # 3. Volume dry-up — are sellers exhausting as the pattern forms?
    volume = calculate_volume_pattern(df)

    # 4. Pivot proximity — how close is the current price to the breakout level?
    #    Falls back to curr_price as pivot when no VCP pivot was detected.
    pivot_level = vcp.get("pivot") or curr_price
    pivot = calculate_pivot_proximity(curr_price, pivot_level)

    # 5. Relative strength vs Nifty 50 — is the stock outperforming the market?
    rs = calculate_relative_strength(df, bench_df)

    # Weighted composite (weights defined in scorer.py: 25/25/20/15/15)
    composite = calculate_composite_score(
        trend_score       = trend["score"],
        contraction_score = vcp["score"],
        volume_score      = volume["score"],
        pivot_score       = pivot["score"],
        rs_score          = rs["score"],
    )

    return {
        "ticker":          ticker,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "composite_score": composite["composite_score"],
        "quality":         composite["quality"],
        "is_vcp":          vcp.get("is_vcp", False),
        "pivot":           vcp.get("pivot"),
        "stage":           trend.get("stage"),
        "dry_up_ratio":    volume.get("dry_up_ratio"),
        "rs_value":        rs.get("rs_value"),
        "components":      composite["components"],
    }


def score_factor(composite: float, is_vcp: bool, alert_type: str) -> VcpFactorScore:
    """
    Convert a VCP composite score (0-100) to a FactorScore-compatible result.

    Semantics:
      BUY:  high VCP composite = stock is coiling in a clean Stage 2 setup
            → price structure favours the entry → +1.
            Low composite = poor structure → -1.
      SELL: high VCP composite = stock is building toward an upside breakout
            → trim early means leaving money on the table → -1.
            Moderate/low = neutral on the sell.
      WATCH: always 0 — VCP is a directional signal, not watch-relevant.
    """
    label = f"VCP:{composite:.0f}"
    if alert_type == "WATCH":
        return VcpFactorScore(0, label)
    if alert_type == "BUY":
        if composite >= 80:
            return VcpFactorScore(1, label)
        elif composite >= 50:
            return VcpFactorScore(0, label)
        else:
            return VcpFactorScore(-1, label)
    else:  # SELL
        if composite >= 80:
            return VcpFactorScore(-1, label)
        return VcpFactorScore(0, label)


def run(ticker: str, df: pd.DataFrame, curr_price: float,
        bench_df: pd.DataFrame | None = None) -> dict:
    """
    Compute the VCP bundle and write analysis/{TICKER}_vcp.json.

    This is the nightly entry point. The hot-alert path only reads the JSON.
    """
    bundle = compute_vcp_bundle(df, curr_price, ticker, bench_df)
    ANALYSIS_DIR.mkdir(exist_ok=True)
    (ANALYSIS_DIR / f"{ticker}_vcp.json").write_text(json.dumps(bundle, indent=2))
    return bundle
