"""
Tests for sr_levels.compute_sr — historical support/resistance from OHLC.

Covers: swing-zone detection + touch counting, support/resistance split by
current price, Fibonacci retracement maths + classification, confluence
flagging (≥2-touch only), and graceful empty returns.
"""
import csv

from sr_levels import compute_sr, _detect_pivots, _cluster, _fib_levels


def _write_ohlc(path, bars):
    """bars: list of (date, o, h, l, c, v) → a cache-format CSV."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Open", "High", "Low", "Close", "Volume",
                    "Dividends", "Stock Splits"])
        for (date, o, h, l, c, v) in bars:
            w.writerow([date, o, h, l, c, v, 0.0, 0.0])


def _series(values):
    """Turn a list of closes into OHLC bars (flat O, H=L=C=value) with dates."""
    bars = []
    for i, v in enumerate(values):
        d = f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}"
        bars.append((d, v, v, v, v, 1000))
    return bars


# ── Fibonacci maths (pure) ──────────────────────────────────────────────────

def test_fib_levels_prices_and_split():
    # Range 100→200; current price 180 → most fibs below = support.
    fib = _fib_levels(100.0, 200.0, 180.0, swing_zones=[])
    by_ratio = {f["ratio"]: f for f in fib}
    assert by_ratio[0.236]["price"] == 176   # 200 - 100*0.236
    assert by_ratio[0.5]["price"] == 150
    assert by_ratio[0.618]["price"] == 138
    # 23.6% (176) is below 180 → support; all are below 180 here.
    assert by_ratio[0.236]["type"] == "support"
    # With current price low (130), the shallow retracements sit above → resistance.
    fib2 = {f["ratio"]: f for f in _fib_levels(100.0, 200.0, 130.0, [])}
    assert fib2[0.236]["type"] == "resistance"   # 176 > 130
    assert fib2[0.786]["type"] == "support"      # 121 < 130


def test_fib_confluence_requires_two_touches():
    zones_weak = [{"price": 150, "touches": 1}]
    zones_strong = [{"price": 150, "touches": 4}]
    weak = {f["ratio"]: f for f in _fib_levels(100.0, 200.0, 180.0, zones_weak)}
    strong = {f["ratio"]: f for f in _fib_levels(100.0, 200.0, 180.0, zones_strong)}
    # 50% fib = 150, coincides with the zone at 150.
    assert weak[0.5]["confluence"] is None        # 1-touch ⇒ not real confluence
    assert strong[0.5]["confluence"] == 4         # ≥2-touch ⇒ flagged with strength


def test_fib_zero_range_returns_empty():
    assert _fib_levels(100.0, 100.0, 100.0, []) == []


# ── Swing detection + clustering ────────────────────────────────────────────

def test_cluster_counts_touches_and_merges_within_pct():
    # Three pivots at ~100 (within 2.5%) → one zone, 3 touches; one at 200 alone.
    pts = [("2025-01-01", 100.0), ("2025-02-01", 101.0),
           ("2025-03-01", 99.0), ("2025-04-01", 200.0)]
    zones = {z["price"]: z for z in _cluster(pts, 0.025)}
    assert zones[100]["touches"] == 3
    assert zones[200]["touches"] == 1


def test_detect_pivots_finds_local_extremes():
    # A clear V: the trough is a swing low; the flanks are swing highs.
    import pandas as pd
    bars = _series([110, 108, 105, 100, 105, 108, 110])
    frame = pd.DataFrame(
        {"High": [b[2] for b in bars], "Low": [b[3] for b in bars]},
        index=pd.to_datetime([b[0] for b in bars]),
    )
    pivots = _detect_pivots(frame, window=2)
    prices = [round(p[1]) for p in pivots]
    assert 100 in prices   # the trough is detected


# ── End-to-end compute_sr ───────────────────────────────────────────────────

def test_compute_sr_splits_support_and_resistance(tmp_path):
    # Price oscillates between a ~100 floor and a ~150 ceiling, many times.
    cycle = [150, 130, 110, 100, 110, 130, 150, 130, 110, 100, 110, 130]
    values = cycle * 6                      # plenty of swing pivots
    path = tmp_path / "X_ohlc_cache.csv"
    _write_ohlc(path, _series(values))
    sr = compute_sr(path, current_price=125, window=2)
    # Zones below 125 are support, above are resistance.
    assert all(z["price"] < 125 for z in sr["support"])
    assert all(z["price"] > 125 for z in sr["resistance"])
    assert sr["support"] and sr["resistance"]
    assert len(sr["fib"]) == 5


def test_compute_sr_falls_back_to_last_close_when_no_price(tmp_path):
    path = tmp_path / "Y_ohlc_cache.csv"
    _write_ohlc(path, _series([150, 130, 110, 100, 110, 130, 150] * 4))
    sr = compute_sr(path, current_price=None, window=2)   # no price → last close
    assert sr["support"] or sr["resistance"]              # still computes a split


def test_compute_sr_empty_on_missing_or_short(tmp_path):
    assert compute_sr(tmp_path / "nope.csv", 100) == {"support": [], "resistance": [], "fib": []}
    short = tmp_path / "Z_ohlc_cache.csv"
    _write_ohlc(short, _series([100, 101, 102]))          # < 2*window+1 rows
    assert compute_sr(short, 100, window=5) == {"support": [], "resistance": [], "fib": []}
