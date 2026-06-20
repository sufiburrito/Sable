"""Phase 2 — the calibration spine.

Measures how predictive each confidence factor actually is, and emits
`data/factor_weights.json` so `confidence.py` can weight factors by their
realized signal instead of summing them equally.

Two label sources, merged:

  1. Reconstructed seed (price-derivable factors). For each watchlist ticker
     we step through its cached OHLC history; at each past date we re-score the
     OHLC-only factors *as of that date* (truncating the frame — the scorers
     read only the tail, so there is no look-ahead) and pair the score with the
     realized **blended forward return** (mean of the 63/126/252-day returns).

  2. Live logged history (all factors). Once the fire-time logging hook
     accumulates factor vectors in `data/sent_alerts.json`, every logged
     factor is calibrated the same way. Empty until that history matures.

Per factor we compute the Spearman rank **information coefficient** (IC) —
the rank-correlation between the factor's vote and the forward return — then
convert IC → weight, mean-normalized to 1.0 so a weight-1.0 factor contributes
exactly as the old equal-weight sum did (verdict thresholds need no change).

Pure offline: reads only cached CSVs + JSON. No network calls.

Run:  python3 -m alert_bot.calibrate
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from .ohlc_cache import read_ohlc_cache
from .confidence import (
    FactorScore,
    _score_trend,
    _score_momentum,
    _score_volume,
    _score_relative_strength,
    _score_dma_support,
    _score_dma_extension,
    _score_dma_cross,
)

_ROOT = Path(__file__).resolve().parent.parent
_ANALYSIS_DIR = _ROOT / "analysis"
_STOCKS_DIR = _ROOT / "stocks"
_DATA_DIR = _ROOT / "data"

# Factors reconstructable from OHLC alone (calibrated now). name → scorer(df, type).
# RS additionally needs a date-aligned Nifty slice, handled separately in the loop.
PRICE_FACTORS: dict[str, Callable[[pd.DataFrame, str], FactorScore]] = {
    "Trend": _score_trend,
    "Momentum": _score_momentum,
    "Volume": _score_volume,
    "DMA-S": _score_dma_support,
    "DMA-X": _score_dma_extension,
    "DMA-C": _score_dma_cross,
}

# Point-in-time factors — their historical state is gone, so they can only be
# calibrated from live logged history. Until then they stay at weight 1.0.
LOGGED_ONLY_FACTORS = ["Regime", "Level", "MMI", "Insider", "vcp", "vix", "flow", "breadth", "fund"]

FORWARD_WINDOWS = (63, 126, 252)   # trading days — the blended outcome horizon
STRIDE = 5                          # sample every 5 trading days (overlap is fine)
WARMUP = 220                        # need 200-DMA + 20-day slope before scoring
MIN_SAMPLES = 30                    # below this a factor's IC is too noisy to trust
IC_FLOOR = 0.02                     # |IC| below this is indistinguishable from noise


# ---------------------------------------------------------------------------
# Pure math (unit-tested in tests/phase2/test_calibrate.py)
# ---------------------------------------------------------------------------

def spearman_ic(scores, returns, min_samples: int = MIN_SAMPLES) -> Optional[float]:
    """Spearman rank correlation of factor scores vs forward returns.

    Uses pandas (average-rank ties — factor scores are -1/0/+1, heavily tied).
    Returns None when there are too few samples or either side has zero variance.
    """
    if len(scores) != len(returns) or len(scores) < min_samples:
        return None
    ic = pd.Series(scores, dtype="float64").corr(
        pd.Series(returns, dtype="float64"), method="spearman"
    )
    if ic is None or pd.isna(ic):
        return None
    return float(ic)


def blended_forward_return(closes, i: int, windows=FORWARD_WINDOWS) -> Optional[float]:
    """Mean of the percent returns from bar `i` to bars i+63 / i+126 / i+252.

    None if the full horizon doesn't fit (the last ~year of every series), so a
    factor must show signal across all three timeframes to earn weight.
    """
    n = len(closes)
    if i + max(windows) >= n:
        return None
    base = float(closes[i])
    if base <= 0:
        return None
    rets = [(float(closes[i + w]) - base) / base * 100.0 for w in windows]
    return float(np.mean(rets))


def ic_to_weights(
    ic: dict[str, Optional[float]],
    n_samples: dict[str, int],
    min_samples: int = MIN_SAMPLES,
    ic_floor: float = IC_FLOOR,
) -> dict[str, float]:
    """Convert per-factor IC into per-factor weights, mean-normalized to 1.0.

    A factor earns a calibrated weight only with enough samples AND |IC| above
    the noise floor; otherwise it stays at a neutral 1.0. Among the calibrated
    factors, weight = (1 + IC) rescaled so their mean is 1.0 — i.e. each vote's
    loudness is set *relative* to its peers, leaving the composite on the same
    scale as the old equal-weight sum.
    """
    calibrated = [
        name for name, v in ic.items()
        if v is not None and n_samples.get(name, 0) >= min_samples and abs(v) >= ic_floor
    ]
    weights: dict[str, float] = {name: 1.0 for name in ic}
    raw = {name: max(0.0, 1.0 + float(ic[name])) for name in calibrated}  # type: ignore[arg-type]
    if raw:
        mean_raw = float(np.mean(list(raw.values())))
        if mean_raw > 0:
            for name, r in raw.items():
                weights[name] = r / mean_raw
    return weights


# ---------------------------------------------------------------------------
# Sample collection
# ---------------------------------------------------------------------------

def _watchlist_tickers() -> list[str]:
    """The equity universe to reconstruct — one .md per tracked ticker."""
    if not _STOCKS_DIR.exists():
        return []
    return sorted(p.stem for p in _STOCKS_DIR.glob("*.md"))


def reconstruct_samples(
    tickers: list[str], nifty_idx: Optional[pd.DataFrame]
) -> tuple[dict[str, tuple[list, list]], Optional[tuple]]:
    """Walk each ticker's history, re-scoring OHLC factors as-of each past date.

    Every factor is scored on the *same* sample set so their ICs are directly
    comparable (a prerequisite for mean-normalizing the weights). Relative
    Strength needs the Nifty benchmark, whose cache is shorter than the single-
    stock caches — so a sample is taken only at dates where the benchmark has
    >=63 bars of history, and that same date set is used for all factors. This
    pins the seed to the common window (recorded in the output for transparency).

    Returns ({factor_name: ([scores], [returns])}, (start_date, end_date)).
    """
    names = list(PRICE_FACTORS) + ["RS"]
    samples: dict[str, tuple[list, list]] = {name: ([], []) for name in names}
    start_date = end_date = None

    if nifty_idx is None:
        return samples, None

    for ticker in tickers:
        df = read_ohlc_cache(ticker, analysis_dir=_ANALYSIS_DIR)
        if df is None or len(df) < WARMUP + max(FORWARD_WINDOWS) + 1:
            continue
        closes = df["Close"].to_numpy(dtype="float64")
        n = len(df)
        for t in range(WARMUP, n - max(FORWARD_WINDOWS), STRIDE):
            fr = blended_forward_return(closes, t)
            if fr is None:
                continue
            date_t = df.index[t]
            try:
                nslice = nifty_idx.loc[:date_t]
            except Exception:
                continue
            if len(nslice) < 63:
                continue   # benchmark not yet available — skip the whole sample
            past = df.iloc[: t + 1]
            for name, fn in PRICE_FACTORS.items():
                f = fn(past, "BUY")
                samples[name][0].append(f.score)
                samples[name][1].append(fr)
            rs = _score_relative_strength(past, "BUY", nifty=nslice)
            samples["RS"][0].append(rs.score)
            samples["RS"][1].append(fr)
            start_date = date_t if start_date is None else min(start_date, date_t)
            end_date = date_t if end_date is None else max(end_date, date_t)

    window = (str(start_date.date()), str(end_date.date())) if start_date is not None else None
    return samples, window


def logged_samples() -> dict[str, tuple[list, list]]:
    """Calibration pairs drawn from matured logged factor vectors in sent_alerts.json.

    Empty until the fire-time logging hook (Deliverable 1) accumulates history.
    """
    samples: dict[str, tuple[list, list]] = {}
    path = _DATA_DIR / "sent_alerts.json"
    if not path.exists():
        return samples
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return samples

    for rec in data.values():
        factors = rec.get("factors")
        ticker = rec.get("ticker")
        fired = rec.get("fired_at")
        if not factors or not ticker or not fired:
            continue
        df = read_ohlc_cache(ticker, analysis_dir=_ANALYSIS_DIR)
        if df is None:
            continue
        try:
            fired_date = pd.Timestamp(fired).tz_localize(None)
            idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
            pos = int(idx.searchsorted(fired_date))
        except Exception:
            continue
        if pos >= len(df):
            continue
        closes = df["Close"].to_numpy(dtype="float64")
        fr = blended_forward_return(closes, pos)
        if fr is None:
            continue
        for name, score in factors.items():
            samples.setdefault(name, ([], []))
            samples[name][0].append(int(score))
            samples[name][1].append(fr)
    return samples


def _merge(a: dict[str, tuple[list, list]], b: dict[str, tuple[list, list]]):
    out: dict[str, tuple[list, list]] = {name: ([], []) for name in set(a) | set(b)}
    for src in (a, b):
        for name, (s, r) in src.items():
            out[name][0].extend(s)
            out[name][1].extend(r)
    return out


# ---------------------------------------------------------------------------
# Optional validation overlay (console only — never written to the weights file)
# ---------------------------------------------------------------------------

def validation_overlay(weights: dict[str, float]) -> None:
    """Sanity check: do the user's *real* broker buys score above a random baseline
    on the calibrated price-factor composite? Prints one line; never raises."""
    db = _DATA_DIR / "portfolio.db"
    if not db.exists():
        return
    try:
        con = sqlite3.connect(str(db))
        rows = con.execute(
            "SELECT symbol, executed_at FROM transactions "
            "WHERE trade_type='BUY' AND COALESCE(is_corporate_action,0)=0"
        ).fetchall()
        con.close()
    except Exception:
        return

    def composite_at(df: pd.DataFrame, pos: int) -> Optional[float]:
        past = df.iloc[: pos + 1]
        if len(past) < WARMUP:
            return None
        total = 0.0
        for name, fn in PRICE_FACTORS.items():
            total += weights.get(name, 1.0) * fn(past, "BUY").score
        return total

    buy_scores: list[float] = []
    for symbol, executed_at in rows:
        df = read_ohlc_cache(symbol, analysis_dir=_ANALYSIS_DIR)
        if df is None:
            continue
        try:
            d = pd.Timestamp(executed_at).tz_localize(None)
            idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
            pos = int(idx.searchsorted(d))
        except Exception:
            continue
        if pos >= len(df):
            continue
        c = composite_at(df, pos)
        if c is not None:
            buy_scores.append(c)

    if not buy_scores:
        print("[overlay] no reconstructable real buys to validate against")
        return
    print(
        f"[overlay] real BUYs: n={len(buy_scores)} "
        f"median calibrated composite={np.median(buy_scores):+.2f} "
        f"(>0 ⇒ the price factors leaned bullish at your real entries)"
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(write: bool = True, overlay: bool = True) -> dict:
    """Compute factor weights from reconstruction + logged history; emit the file."""
    tickers = _watchlist_tickers()
    nifty_idx = read_ohlc_cache("NIFTY50", analysis_dir=_ANALYSIS_DIR)

    recon, window = reconstruct_samples(tickers, nifty_idx)
    merged = _merge(recon, logged_samples())

    ic: dict[str, Optional[float]] = {}
    n_samples: dict[str, int] = {}
    for name, (s, r) in merged.items():
        n_samples[name] = len(s)
        ic[name] = spearman_ic(s, r)

    weights = ic_to_weights(ic, n_samples)

    calibrated = sorted(
        name for name in merged
        if ic.get(name) is not None
        and n_samples[name] >= MIN_SAMPLES
        and abs(ic[name]) >= IC_FLOOR     # type: ignore[arg-type]
    )
    default_equal = sorted(set(LOGGED_ONLY_FACTORS) - set(calibrated))

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "horizon": "blend(63,126,252)",
        "method": "spearman_ic",
        "reconstruction_window": window,   # common window pinned by the Nifty benchmark cache
        "weights": {k: round(v, 4) for k, v in weights.items()},
        "ic": {k: (round(v, 4) if v is not None else None) for k, v in ic.items()},
        "n_samples": n_samples,
        "calibrated": calibrated,
        "default_equal": default_equal,
    }

    if write:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        (_DATA_DIR / "factor_weights.json").write_text(
            json.dumps(out, indent=2), encoding="utf-8"
        )

    if overlay:
        try:
            validation_overlay(weights)
        except Exception:
            pass

    return out


def main() -> None:
    out = run()
    print(f"Wrote data/factor_weights.json — method={out['method']}, horizon={out['horizon']}")
    print(f"Calibrated factors: {out['calibrated']}")
    print(f"Default-equal (await live history): {out['default_equal']}")
    for name in out["calibrated"]:
        print(f"  {name:10s} IC={out['ic'][name]:+.4f}  weight={out['weights'][name]:.3f}  n={out['n_samples'][name]}")


if __name__ == "__main__":
    main()
