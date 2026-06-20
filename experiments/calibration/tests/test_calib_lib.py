"""TDD tests for the calibration-experiment library (experiments/calibration/calib_lib.py).

These cover the *new* pure logic the bake-off harness adds on top of the
production `alert_bot.calibrate` engine:

  - classify_regime: a point-in-time regime tag from the NIFTY benchmark's own
    trailing return (must never peek at bars after the sample date).

Run from the repo root:  python3 -m pytest experiments/calibration/tests -q
"""
import numpy as np
import pandas as pd

from experiments.calibration import calib_lib as cl


# ---------------------------------------------------------------------------
# classify_regime — point-in-time regime tag from the benchmark's trailing return
# ---------------------------------------------------------------------------

def test_classify_regime_uptrend():
    # A series rising +20% over the lookback window is unambiguously an uptrend.
    closes = np.linspace(100.0, 120.0, 200)
    assert cl.classify_regime(closes, i=199, lookback=126) == "uptrend"


def test_classify_regime_downtrend():
    # A series falling -20% over the lookback window is a downtrend.
    closes = np.linspace(120.0, 100.0, 200)
    assert cl.classify_regime(closes, i=199, lookback=126) == "downtrend"


def test_classify_regime_sideways():
    # A flat series (well inside the +/-5% band) is sideways.
    closes = np.full(200, 100.0)
    assert cl.classify_regime(closes, i=199, lookback=126) == "sideways"


def test_classify_regime_unknown_before_lookback():
    # Not enough history behind bar i to measure a trailing return.
    closes = np.linspace(100.0, 120.0, 200)
    assert cl.classify_regime(closes, i=50, lookback=126) == "unknown"


def test_classify_regime_no_lookahead():
    # The label at bar i must depend only on bars <= i. Mutating future bars
    # (everything after i) must not change the classification.
    closes = np.linspace(100.0, 110.0, 300)
    i = 150
    before = cl.classify_regime(closes, i=i, lookback=126)
    closes[i + 1:] = -999.0  # corrupt the future
    after = cl.classify_regime(closes, i=i, lookback=126)
    assert before == after


# ---------------------------------------------------------------------------
# Weighting methods — the building blocks of the bake-off
# ---------------------------------------------------------------------------

FACTORS = ["A", "B", "C"]


def _synthetic_samples(n=300, seed=0):
    """A frame where factor 'A' predicts the forward return, 'B' is a perfect
    copy of 'A' (redundant), and 'C' is independent noise."""
    rng = np.random.default_rng(seed)
    a = rng.integers(-1, 2, size=n).astype(float)        # -1/0/+1 votes
    noise = rng.normal(0, 1.0, size=n)
    fwd = a * 3.0 + noise                                 # A drives the outcome
    c = rng.integers(-1, 2, size=n).astype(float)         # orthogonal to fwd
    return pd.DataFrame({"A": a, "B": a.copy(), "C": c, "fwd_return": fwd})


def test_ic_weights_rewards_the_predictive_factor():
    df = _synthetic_samples()
    w = cl.ic_weights(df, FACTORS)
    # A (and its copy B) predict the return; C does not → A weighted above C.
    assert w["A"] > w["C"]
    # Mean-normalized: the average calibrated weight is ~1.0.
    assert abs(np.mean(list(w.values())) - 1.0) < 0.25


def test_shrink_weights_endpoints():
    w = {"A": 1.6, "B": 0.4, "C": 1.0}
    # lam=0 collapses every weight to the equal-weight 1.0…
    assert cl.shrink_weights(w, 0.0) == {"A": 1.0, "B": 1.0, "C": 1.0}
    # …lam=1 leaves them untouched.
    assert cl.shrink_weights(w, 1.0) == w
    # lam=0.5 is the midpoint.
    half = cl.shrink_weights(w, 0.5)
    assert abs(half["A"] - 1.3) < 1e-9 and abs(half["B"] - 0.7) < 1e-9


def test_redundancy_weights_downweights_the_collinear_pair():
    df = _synthetic_samples()
    w = cl.redundancy_weights(df, FACTORS)
    # A and B are perfectly correlated (one idea, two votes) → each gets less
    # weight than the orthogonal C.
    assert w["C"] > w["A"]
    assert abs(w["A"] - w["B"]) < 1e-6   # the twins are treated identically


def test_composite_scores_equal_weights_is_the_plain_sum():
    df = _synthetic_samples()
    eq = {f: 1.0 for f in FACTORS}
    s = cl.composite_scores(df, eq, FACTORS)
    expected = df[FACTORS].sum(axis=1)
    assert np.allclose(s.to_numpy(), expected.to_numpy())


# ---------------------------------------------------------------------------
# Profit metrics — the leaderboard columns
# ---------------------------------------------------------------------------

def test_bucket_metrics_perfect_ranking():
    composites = [1, 2, 3, 4, 5, 6]
    returns = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]   # composite perfectly orders returns
    m = cl.bucket_metrics(composites, returns, frac=1 / 3, cost=0.0)
    # top tercile = {5,6} mean 5.5 ; bottom = {1,2} mean 1.5
    assert abs(m["top_return"] - 5.5) < 1e-9
    assert abs(m["spread"] - 4.0) < 1e-9
    assert abs(m["hit_rate"] - 100.0) < 1e-9


def test_bucket_metrics_cost_drags_returns_down():
    composites = [1, 2, 3, 4, 5, 6]
    returns = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    free = cl.bucket_metrics(composites, returns, frac=1 / 3, cost=0.0)
    costed = cl.bucket_metrics(composites, returns, frac=1 / 3, cost=0.1)
    assert costed["top_return"] < free["top_return"]
    assert costed["spread"] < free["spread"]


# ---------------------------------------------------------------------------
# Regime detection — volatility-state classification (the GARCH output stage)
# ---------------------------------------------------------------------------

def _vol_series_calm_then_burst(n_low=30, n_high=10):
    """Low, constant vol followed by a high-vol burst — a clean regime change."""
    idx = pd.date_range("2024-01-01", periods=n_low + n_high, freq="D")
    vals = [1.0] * n_low + [10.0] * n_high
    return pd.Series(vals, index=idx)


def test_vol_states_flags_the_burst_as_stressed():
    vol = _vol_series_calm_then_burst()
    states = cl.vol_states_from_series(vol, hi=0.67, lo=0.33, min_history=10)
    # A bar deep in the calm run is 'calm'; a bar in the burst is 'stressed'.
    assert states.iloc[25] == "calm"
    assert states.iloc[37] == "stressed"


def test_vol_states_no_lookahead():
    # The state at bar i uses only the vol history up to i — mutating the future
    # must not change it.
    vol = _vol_series_calm_then_burst()
    i = 25
    before = cl.vol_states_from_series(vol, min_history=10).iloc[i]
    vol.iloc[i + 1:] = 999.0
    after = cl.vol_states_from_series(vol, min_history=10).iloc[i]
    assert before == after


def test_vol_states_insufficient_history_is_neutral():
    vol = _vol_series_calm_then_burst()
    states = cl.vol_states_from_series(vol, min_history=10)
    # Before min_history bars accumulate, we can't judge stress → neutral.
    assert (states.iloc[:9] == "normal").all()


# ---------------------------------------------------------------------------
# Regime detection — 2D combo + gate + detector metrics
# ---------------------------------------------------------------------------

def test_combined_regime_pairs_direction_and_turbulence():
    idx = pd.date_range("2024-01-01", periods=2, freq="D")
    direction = pd.Series(["bull", "bear"], index=idx)
    turbulence = pd.Series(["calm", "stressed"], index=idx)
    cells = cl.combined_regime(direction, turbulence)
    assert list(cells) == ["bull|calm", "bear|stressed"]


def test_gate_composite_damps_only_danger_cells():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    comp = pd.Series([10.0, 10.0, 10.0], index=idx)
    cells = pd.Series(["bull|calm", "bear|stressed", "sideways|normal"], index=idx)
    gated = cl.gate_composite(comp, cells, danger_cells={"bear|stressed"}, damp=0.0)
    assert list(gated) == [10.0, 0.0, 10.0]


def test_gate_composite_damp_one_is_noop():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    comp = pd.Series([10.0, -4.0, 7.0], index=idx)
    cells = pd.Series(["bear|stressed"] * 3, index=idx)
    gated = cl.gate_composite(comp, cells, danger_cells={"bear|stressed"}, damp=1.0)
    assert list(gated) == [10.0, -4.0, 7.0]


def test_detection_lag_counts_days_after_peak():
    idx = pd.date_range("2024-01-01", periods=15, freq="D")
    danger = pd.Series([False] * 15, index=idx)
    danger.iloc[9:] = True                       # first danger flag at index 9
    lag = cl.detection_lag(danger, peak_date="2024-01-05")   # peak at index 4
    assert lag == 5                              # 9 - 4


def test_detection_lag_none_when_never_flagged():
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    danger = pd.Series([False] * 10, index=idx)
    assert cl.detection_lag(danger, peak_date="2024-01-03") is None


def test_whipsaw_rate_counts_false_to_true_rises():
    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    danger = pd.Series([False, True, False, True, False, True], index=idx)
    n = cl.whipsaw_rate(danger, start="2024-01-01", end="2024-01-06")
    assert n == 3


# ---------------------------------------------------------------------------
# SELL-side (swing-trim) calibration primitives (Round 5)
# ---------------------------------------------------------------------------

def test_extension_above_ma_known_value():
    # 9 bars flat at 100 then one bar at 120. With window=10 the SMA over the
    # last 10 bars = (9*100 + 120)/10 = 102, so extension = (120-102)/102.
    close = pd.Series([100.0] * 9 + [120.0])
    ext = cl.extension_above_ma(close, window=10)
    assert ext.iloc[-1] == (120.0 - 102.0) / 102.0


def test_extension_above_ma_no_lookahead():
    # Extension at bar t must use only bars <= t. Mutating a LATER bar cannot
    # change an earlier extension value (rolling is causal by construction).
    close = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    base = cl.extension_above_ma(close, window=3)
    bumped = close.copy()
    bumped.iloc[-1] = 999.0
    after = cl.extension_above_ma(bumped, window=3)
    assert base.iloc[3] == after.iloc[3]        # earlier value unchanged


def test_forward_max_dip_captures_deepest_drop():
    # From bar i=0 (price 100) the path dips to 90 within the horizon -> the
    # deepest forward dip is (90-100)/100 = -10%, returned as magnitude 10.0.
    close = np.array([100.0, 105.0, 90.0, 110.0])
    assert cl.forward_max_dip(close, i=0, horizon=3) == 10.0


def test_forward_max_dip_zero_when_only_rises():
    # A path that only rises has no dip below entry -> magnitude 0.0.
    close = np.array([100.0, 110.0, 120.0, 130.0])
    assert cl.forward_max_dip(close, i=0, horizon=3) == 0.0


def test_trim_reload_roundtrip_wins_when_reverts_cheaper():
    # Trim at 100, price reverts (ext drops to reload level) at a price of 95,
    # then ends at 110. Reloading cheaper than the trim price beats holding.
    fwd_close = np.array([100.0, 120.0, 95.0, 110.0])
    fwd_ext = np.array([0.20, 0.30, -0.01, 0.05])    # reverts to <=0 at index 2
    roundtrip, hold = cl.trim_reload_roundtrip(fwd_close, fwd_ext, reload_ext=0.0, cost_pct=0.0)
    assert hold == (110.0 / 100.0 - 1.0) * 100.0      # +10%
    assert roundtrip == (110.0 / 95.0 - 1.0) * 100.0  # ~+15.8%
    assert roundtrip > hold


def test_trim_reload_roundtrip_loses_when_never_reverts():
    # Trim at 100, price never reverts to the reload level -> swing sits in cash
    # (0% return) while holding would have ridden the trend up +40%.
    fwd_close = np.array([100.0, 120.0, 130.0, 140.0])
    fwd_ext = np.array([0.20, 0.30, 0.40, 0.50])      # never <= 0
    roundtrip, hold = cl.trim_reload_roundtrip(fwd_close, fwd_ext, reload_ext=0.0, cost_pct=0.0)
    assert hold == (140.0 / 100.0 - 1.0) * 100.0      # +40%
    assert roundtrip == 0.0                           # stayed in cash
    assert roundtrip < hold


def test_trailing_top_pctile_flag_fires_in_top_band():
    # Value sits above `hi` share of its own trailing history -> flag True.
    # Rising series: the last bar is the highest it has ever been (100% of prior
    # bars below it) so it flags; an early bar with too little history does not.
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    flag = cl.trailing_top_pctile_flag(s, hi=0.80, min_history=3)
    assert bool(flag.iloc[-1]) is True
    assert bool(flag.iloc[0]) is False                # no history yet
