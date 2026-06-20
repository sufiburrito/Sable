"""
Hidden Markov Model (HMM) Regime Detection for Stock Prices.

= WHAT THIS MODULE DOES =

Imagine a stock has a "mood" that changes over time — sometimes it's
optimistic (bull), sometimes pessimistic (bear), sometimes just calm
(sideways), and sometimes confused/chaotic (volatile). You can't SEE
the mood directly. But you CAN see clues: how much the price moved,
how wildly it's been swinging, and how many people are trading it.

This module uses a Hidden Markov Model to look at those clues and
figure out: "What mood is this stock MOST LIKELY in today?"

= HOW IT WORKS (THE INTUITION) =

Step 1: FEATURES — We measure 4 "clues" about the stock each day:
    - How much did the price change? (smoothed over 5 days)
    - How wildly has it been swinging? (volatility over 10 days)
    - Are more people trading than usual? (volume vs its 20-day average)
    - Are the recent moves lopsided? (skewness over 20 days)

Step 2: TRAINING — We feed these clues to the HMM algorithm. It looks
    at the entire history and discovers patterns: "Ah, when returns are
    positive AND volatility is low, that tends to cluster together for
    weeks — I'll call that State 0." It finds 4 such clusters (states)
    entirely on its own. We don't tell it what "bull" means — it figures
    out the groupings from the data.

Step 3: LABELING — After training, we look at what each state's numbers
    look like and assign human-readable names: the state with the highest
    average return is "Bull," the one with the most negative return is
    "Bear," etc.

Step 4: DECODING — For each day in history (and crucially, for TODAY),
    the model tells us: "There's an 87% chance today is Bull, 8% chance
    Sideways, 4% Bear, 1% Volatile." The highest-probability state is
    the regime call.

= THE 4 REGIMES AND WHAT TO DO IN EACH =

    Bull       → Price rising, calm conditions. Accumulate on dips.
    Bear       → Price falling, high fear. Hold core, don't add.
    Sideways   → Price flat, quiet. Trade the range (buy support, trim resistance).
    Volatile   → High uncertainty, no clear direction. DO NOTHING. Wait.

= KEY TERMS =

    "Hidden"     — The regime is hidden; we can't observe it directly.
    "Markov"     — Today's regime depends ONLY on yesterday's regime,
                   not on last week or last month. (Memoryless.)
    "Emission"   — The clues (features) that each regime "emits."
                   Bull emits positive returns + low vol. Bear emits
                   negative returns + high vol. Etc.
    "Transition" — The probability of switching from one regime to another.
                   Regimes are "sticky" — once you're in bull, you tend
                   to stay in bull for a while before switching.
"""

import warnings
import numpy as np
from hmmlearn.hmm import GaussianHMM
from scipy.stats import skew


# ─────────────────────────────────────────────────────────────────
# STEP 1: FEATURE ENGINEERING
#
# We transform raw price/volume data into 4 measurements that help
# the HMM distinguish between regimes. Each measurement captures a
# different dimension of market behavior.
# ─────────────────────────────────────────────────────────────────

def compute_features(
    closes: np.ndarray,
    volumes: np.ndarray,
) -> tuple[np.ndarray, int]:
    """
    Turn raw price and volume data into the 4 features the HMM needs.

    Parameters:
        closes  — array of daily closing prices (oldest first)
        volumes — array of daily trading volumes (same length as closes)

    Returns:
        features — 2D array, shape (num_days, 4). Each row is one day,
                   each column is one feature. Ready to feed to the HMM.
        offset   — how many days at the start were "used up" computing
                   rolling windows. The features array starts at
                   day `offset` of the original data.
    """

    # ── Feature 1: Smoothed log returns (5-day average) ──
    #
    # A "log return" is the natural logarithm of (today's price / yesterday's price).
    # Example: if price goes from 100 to 105, log return = ln(105/100) = 0.0488 ≈ +4.9%
    #
    # Why logarithm instead of simple percentage?
    #   - Log returns ADD up over multiple days (simple percentages don't)
    #   - A +5% move and a -5% move cancel out in log returns but NOT in simple %
    #   - This makes the math cleaner and more honest
    #
    # Why smooth over 5 days?
    #   - A single day's return is very noisy — one big trade can spike it.
    #   - Averaging 5 days gives us the "trend direction this week" instead of
    #     "what happened today." The HMM cares about the trend, not daily noise.
    log_prices = np.log(closes)
    daily_log_returns = np.diff(log_prices)  # length = len(closes) - 1

    smoothed_returns = _rolling_mean(daily_log_returns, window=5)

    # ── Feature 2: Realized volatility (10-day rolling standard deviation) ──
    #
    # "Volatility" measures how wildly the price has been swinging.
    #
    # Standard deviation is a statistical measure of spread:
    #   - If daily returns are [+1%, -0.5%, +0.8%, -0.3%, +0.6%], the std dev is small
    #     (returns are clustered near zero — calm market)
    #   - If daily returns are [+5%, -4%, +6%, -3%, +7%], the std dev is large
    #     (returns are all over the place — wild market)
    #
    # We compute this over a rolling 10-day window, so we get a volatility
    # reading for each day that reflects "how crazy were the last 10 days."
    #
    # Why 10 days? It's roughly 2 trading weeks — long enough to capture a
    # volatility regime, short enough to react when conditions change.
    realized_vol = _rolling_std(daily_log_returns, window=10)

    # ── Feature 3: Volume ratio (today's volume / 20-day average volume) ──
    #
    # Raw volume numbers are meaningless for comparison between stocks.
    # STLTECH might trade 5 million shares normally; SPARC might trade 500,000.
    # Instead, we ask: "Is today's volume HIGH or LOW for THIS stock?"
    #
    # We divide each day's volume by the average of the last 20 days.
    #   - Ratio = 1.0 → perfectly normal activity
    #   - Ratio = 2.5 → 2.5x normal → something significant is happening
    #   - Ratio = 0.4 → unusually quiet → nobody is interested right now
    #
    # Why this matters: price movement WITH high volume = conviction.
    # Price movement on LOW volume = potentially meaningless drift.
    vol_ratio = _rolling_ratio(volumes, window=20)

    # ── Feature 4: Return skewness (20-day rolling) ──
    #
    # "Skewness" measures whether recent returns are lopsided.
    #
    # Imagine plotting the last 20 days of returns on a histogram:
    #   - If the histogram is symmetric (balanced left and right), skewness ≈ 0
    #   - If there are a few big NEGATIVE days pulling the left tail,
    #     skewness is NEGATIVE → this is typical of bear regimes (sudden drops)
    #   - If there are a few big POSITIVE days pulling the right tail,
    #     skewness is POSITIVE → this can happen in strong bull rallies
    #
    # Skewness helps the model distinguish between:
    #   - Bear (negative skew: mostly okay days with occasional sharp drops)
    #   - Volatile (both positive AND negative outliers, skew near zero but vol high)
    return_skew = _rolling_skewness(daily_log_returns, window=20)

    # ── Align all features ──
    #
    # Each rolling window "uses up" some days at the start. For example,
    # a 20-day rolling window can't produce a value until day 20.
    # We need to trim all features to the same length, starting from the
    # point where ALL features have valid values.
    #
    # The longest window is 20 days (skewness and volume ratio), applied
    # AFTER the diff (which loses 1 day), so we lose 20 days total.
    # But to be safe, we compute the actual offset from the shortest array.
    min_len = min(len(smoothed_returns), len(realized_vol),
                  len(vol_ratio), len(return_skew))

    # Take the last min_len values from each (they align at the end)
    features = np.column_stack([
        smoothed_returns[-min_len:],
        realized_vol[-min_len:],
        vol_ratio[-min_len:],
        return_skew[-min_len:],
    ])

    # The offset tells the caller: "features[0] corresponds to
    # closes[offset], not closes[0]"
    offset = len(closes) - min_len

    return features, offset


# ─────────────────────────────────────────────────────────────────
# ROLLING WINDOW HELPERS
#
# These functions compute statistics over a sliding window.
# Imagine dragging a magnifying glass of fixed width across the data —
# at each position, you compute one number summarizing what's under
# the glass, then slide it one step forward.
# ─────────────────────────────────────────────────────────────────

def _rolling_mean(data: np.ndarray, window: int) -> np.ndarray:
    """
    Compute the rolling average over a sliding window.

    Example with window=3 and data=[10, 20, 30, 40, 50]:
        Position 0: can't compute (not enough data yet)
        Position 1: can't compute
        Position 2: average(10, 20, 30) = 20
        Position 3: average(20, 30, 40) = 30
        Position 4: average(30, 40, 50) = 40
        Result: [20, 30, 40]

    We use numpy's cumsum trick for speed: instead of re-adding all
    numbers in the window each time, we keep a running total and
    subtract the number that just left the window.
    """
    cumsum = np.cumsum(data)
    cumsum[window:] = cumsum[window:] - cumsum[:-window]
    return cumsum[window - 1:] / window


def _rolling_std(data: np.ndarray, window: int) -> np.ndarray:
    """
    Compute the rolling standard deviation over a sliding window.

    Standard deviation = "how spread out are the numbers?"
      - Small std dev → numbers are clustered together (calm)
      - Large std dev → numbers are all over the place (wild)

    We compute this by sliding a window across the data and calculating
    the standard deviation of the numbers inside the window at each step.
    """
    result = np.empty(len(data) - window + 1)
    for i in range(len(result)):
        result[i] = np.std(data[i:i + window], ddof=1)
    return result


def _rolling_ratio(volumes: np.ndarray, window: int) -> np.ndarray:
    """
    Compute each day's volume divided by its trailing average.

    Result > 1.0 means "more active than usual."
    Result < 1.0 means "quieter than usual."
    """
    avg = _rolling_mean(volumes.astype(float), window)
    # The rolling mean starts at index (window-1) of the original data.
    # We divide each day's volume by its corresponding average.
    # The ratio array aligns with the END of the rolling mean.
    current = volumes[window - 1:]
    # Avoid division by zero for stocks with zero-volume days
    safe_avg = np.where(avg > 0, avg, 1.0)
    return current / safe_avg


def _rolling_skewness(data: np.ndarray, window: int) -> np.ndarray:
    """
    Compute the rolling skewness over a sliding window.

    Skewness measures asymmetry:
      - Negative skew → left tail is longer (occasional big drops)
      - Positive skew → right tail is longer (occasional big jumps)
      - Zero skew → symmetric (equally likely to go up or down by the same amount)

    We use scipy's skew function which handles the math.
    """
    result = np.empty(len(data) - window + 1)
    for i in range(len(result)):
        result[i] = skew(data[i:i + window])
    return result


# ─────────────────────────────────────────────────────────────────
# STEP 2: TRAINING THE HMM
#
# We feed the features to the HMM and let it discover 4 clusters
# (regimes) in the data. The algorithm it uses internally is called
# "Baum-Welch" (a variant of Expectation-Maximization):
#
#   1. Start with a random guess of what each regime looks like.
#   2. Assign each day to the regime it most likely belongs to.
#   3. Re-estimate what each regime looks like based on assignments.
#   4. Repeat steps 2-3 until the assignments stop changing.
#
# Because the starting guess is random, different runs can converge
# to different solutions. So we run the algorithm multiple times and
# keep the best result (highest "score" = the model that explains
# the data best).
# ─────────────────────────────────────────────────────────────────

def train_hmm(
    features: np.ndarray,
    n_states: int = 4,
    n_runs: int = 8,
    random_seed: int = 42,
) -> GaussianHMM:
    """
    Train a Gaussian Hidden Markov Model on the feature matrix.

    "Gaussian" means the model assumes each regime's features follow a
    bell-curve (normal) distribution. This is a reasonable assumption
    for financial returns over short windows.

    Parameters:
        features    — 2D array from compute_features(), shape (days, 4)
        n_states    — number of regimes to discover (default: 4)
        n_runs      — how many times to re-train with different starting
                      guesses and keep the best (default: 8)
        random_seed — for reproducibility

    Returns:
        The best-fitting GaussianHMM model object, ready for decoding.
    """
    best_model = None
    best_score = float('-inf')

    for run in range(n_runs):
        model = GaussianHMM(
            n_components=n_states,

            # "full" covariance means the model can learn that features
            # are correlated within a regime. For example, in a bear
            # regime, high volatility and negative returns tend to
            # occur TOGETHER — "full" lets the model capture this
            # relationship. Simpler options ("diag", "spherical") would
            # assume features are independent, which they aren't.
            covariance_type="full",

            # Maximum iterations for the Baum-Welch algorithm.
            # 200 is generous — it usually converges in 50-100.
            n_iter=200,

            # Each run uses a different random starting point.
            random_state=random_seed + run,

            # Stop early if the improvement between iterations is tiny.
            # 1e-4 means "stop if the score improved by less than 0.01%."
            tol=1e-4,
        )

        # Suppress convergence warnings — some runs won't converge fully
        # from their random starting point, and that's fine. We'll pick
        # the best one anyway.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model.fit(features)

        # Score = log-likelihood = "how well does this model explain the data?"
        # Higher is better. Think of it as a grade: the model that gets the
        # highest grade is the one that best captures the patterns in the data.
        score = model.score(features)

        if score > best_score:
            best_score = score
            best_model = model

    return best_model


# ─────────────────────────────────────────────────────────────────
# STEP 3: LABELING THE REGIMES
#
# The HMM discovers 4 states, but it calls them State 0, State 1,
# State 2, State 3. It has no idea what "bull" or "bear" means.
#
# We look at what each state's average features look like and assign
# human-readable labels. The labeling logic:
#
#   1. Sort states by their mean return (feature 0).
#   2. The state with the HIGHEST mean return → "bull"
#   3. The state with the LOWEST mean return → "bear"
#   4. Of the remaining two, the one with HIGHER volatility → "volatile"
#   5. The last one → "sideways"
#
# This is a heuristic, not a guarantee. But it works well in practice
# because the return and volatility dimensions are the strongest
# separators between regime types.
# ─────────────────────────────────────────────────────────────────

# Human-readable regime names and their associated colors/actions
REGIME_INFO = {
    "bull":     {"color": "#2dd4a0", "action": "Accumulate on dips",     "emoji": "🟢"},
    "bear":     {"color": "#f06060", "action": "Hold core, don't add",   "emoji": "🔴"},
    "sideways": {"color": "#8c8a80", "action": "Range-trade swing layer", "emoji": "⚪"},
    "volatile": {"color": "#e0c040", "action": "Wait for clarity",       "emoji": "🟡"},
}


def label_regimes(model: GaussianHMM) -> dict[int, str]:
    """
    Assign human-readable names to the HMM's numbered states.

    Parameters:
        model — a trained GaussianHMM

    Returns:
        dict mapping state number → regime name
        Example: {0: "bear", 1: "sideways", 2: "bull", 3: "volatile"}
    """
    # model.means_ is a 2D array, shape (n_states, n_features).
    # Row i = the average feature values for state i.
    # Column 0 = mean smoothed return for that state.
    # Column 1 = mean realized volatility for that state.
    means = model.means_

    # Sort state indices by their mean return (column 0), lowest to highest
    sorted_by_return = np.argsort(means[:, 0])

    # Lowest return → bear, highest return → bull
    bear_state = sorted_by_return[0]
    bull_state = sorted_by_return[-1]

    # The middle two states: the one with higher volatility is "volatile",
    # the other is "sideways"
    middle_states = sorted_by_return[1:-1]

    # Compare their mean volatility (column 1)
    if means[middle_states[0], 1] > means[middle_states[1], 1]:
        volatile_state = middle_states[0]
        sideways_state = middle_states[1]
    else:
        volatile_state = middle_states[1]
        sideways_state = middle_states[0]

    return {
        bull_state: "bull",
        bear_state: "bear",
        sideways_state: "sideways",
        volatile_state: "volatile",
    }


# ─────────────────────────────────────────────────────────────────
# STEP 4: DECODING — WHAT REGIME IS EACH DAY IN?
#
# Now that the model is trained and labeled, we ask it two questions:
#
#   A) For each day in history, what regime was it most likely in?
#      → This gives us the colored background for the chart.
#
#   B) For TODAY specifically, what are the probabilities?
#      → This gives us the confidence of the current regime call.
#      → Example: {bull: 0.87, sideways: 0.08, bear: 0.04, volatile: 0.01}
#
# The algorithm used for (A) is called "Viterbi" — it finds the single
# most likely SEQUENCE of regimes across all days. This is subtly
# different from just picking the highest-probability regime for each
# day independently, because it respects the transition probabilities
# (the model knows regimes are sticky and penalizes rapid switching).
# ─────────────────────────────────────────────────────────────────

def decode_regimes(
    model: GaussianHMM,
    features: np.ndarray,
    labels: dict[int, str],
) -> dict:
    """
    Decode the regime for every day, and compute today's probabilities.

    Parameters:
        model    — trained GaussianHMM
        features — the same feature matrix used for training (or new data)
        labels   — state-number-to-name mapping from label_regimes()

    Returns:
        dict with:
          regimes     — list of regime names, one per day ("bull", "bear", ...)
          probs       — today's probability for each regime name
          current     — the most likely regime name for today
          transitions — the model's transition matrix, labeled with names
    """
    # Viterbi decoding: find the most likely sequence of hidden states
    # that explains the observed features.
    state_sequence = model.predict(features)

    # Convert state numbers to human-readable names
    regimes = [labels[s] for s in state_sequence]

    # Get the probability of each regime for every day.
    # predict_proba returns shape (n_days, n_states), where each row
    # sums to 1.0. We care most about the LAST row (today).
    all_probs = model.predict_proba(features)
    today_probs_raw = all_probs[-1]

    # Map to named probabilities
    today_probs = {}
    for state_num, name in labels.items():
        today_probs[name] = round(float(today_probs_raw[state_num]), 4)

    # Current regime = the one with highest probability today
    current = max(today_probs, key=today_probs.get)

    # Transition matrix: probability of moving from regime A to regime B.
    # model.transmat_[i][j] = P(tomorrow = state j | today = state i)
    # We label the rows and columns with regime names.
    trans = {}
    for i, name_i in labels.items():
        trans[name_i] = {}
        for j, name_j in labels.items():
            trans[name_i][name_j] = round(float(model.transmat_[i][j]), 4)

    return {
        "regimes": regimes,
        "probs": today_probs,
        "current": current,
        "transitions": trans,
    }


# ─────────────────────────────────────────────────────────────────
# STEP 5: PER-REGIME GBM PARAMETER ESTIMATION
#
# This is the bridge between the HMM and Monte Carlo simulation.
# The HMM tells us "which regime was each day in." Now we go back
# to the RAW daily log returns and compute: "What were the return
# statistics (mu, sigma, skewness) specifically during bull days?
# During bear days?" etc.
#
# These per-regime parameters are what the regime-switching Monte
# Carlo engine needs to generate realistic forward simulations.
#
# IMPORTANT: We DON'T use the HMM's model.means_ for this.
# Those are means of the engineered features (5-day smoothed
# returns, 10-day volatility, etc.) — NOT the raw daily log
# returns that GBM consumes. We need to go back to the raw data.
#
# SHRINKAGE (Credibility Weighting):
# Some regimes are rare. In 2 years of data, "volatile" might
# only cover 25 days. Estimating mu and sigma from 25 data points
# is noisy — you might get a wildly high or low number just by
# chance. Shrinkage blends the regime-specific estimate toward
# the global average:
#
#   weight = n_observations / (n_observations + 60)
#   mu_final = weight * mu_regime + (1 - weight) * mu_global
#
# If you have 300 bull days: weight = 300/360 = 0.83 → trust
#   the regime-specific estimate (barely any shrinkage).
# If you have 25 volatile days: weight = 25/85 = 0.29 → pull
#   heavily toward the global average (don't trust sparse data).
#
# The constant 60 is a "prior sample size" — it says "I need at
# least 60 observations before I fully trust a regime's numbers."
# This is a standard technique in Bayesian statistics.
# ─────────────────────────────────────────────────────────────────

def estimate_regime_gbm_params(
    closes: np.ndarray,
    regime_sequence: list[str],
    offset: int,
    shrinkage_prior: int = 60,
) -> dict:
    """
    Estimate annualized GBM parameters (mu, sigma, skewness) for each
    regime, using only the daily returns that fell within that regime.

    This is the critical function that connects HMM output to Monte
    Carlo input. The HMM tells us "day 47 was Bull." We look at the
    actual daily log return on day 47 and group it with all other Bull
    returns. Then we compute statistics on each group.

    Parameters:
        closes          — daily closing prices used for the HMM
                          (the same array passed to run_regime_detection)
        regime_sequence — list of regime names from decode_regimes(),
                          one per day AFTER the feature offset
        offset          — how many days at the start were "used up" by
                          feature computation (from compute_features)
        shrinkage_prior — the "prior sample size" for shrinkage.
                          Higher = more shrinkage toward global mean.
                          Default 60 = "trust regime-specific stats
                          only after ~60 observations."

    Returns:
        dict mapping regime name → {mu, sigma, skewness, n_obs, shrinkage_w}
        where:
          mu         — annualized drift (e.g., +0.25 means +25% per year)
          sigma      — annualized volatility (e.g., 0.30 means 30% per year)
          skewness   — asymmetry of daily returns (negative = left tail,
                       i.e., occasional sharp drops typical of bear regimes)
          n_obs      — how many daily returns fell in this regime
          shrinkage_w — the weight applied (0 to 1). Close to 1 = trusted.
    """
    # Step 1: Compute daily log returns from the FULL closing price array.
    #
    # log_returns[i] = ln(closes[i+1] / closes[i])
    # This gives us one return per day (length = len(closes) - 1).
    log_returns = np.diff(np.log(closes.astype(float)))

    # Step 2: Align returns with regime labels.
    #
    # The regime_sequence starts at position `offset` of the closes array.
    # But log_returns starts at position 1 of closes (because it's a diff).
    # So regime_sequence[i] corresponds to log_returns[offset + i - 1]
    # (the return from day offset+i-1 to day offset+i).
    #
    # Wait — let's think carefully:
    #   - closes has N prices: closes[0], closes[1], ..., closes[N-1]
    #   - log_returns has N-1 values: log_returns[i] = ln(closes[i+1]/closes[i])
    #     so log_returns[i] is the return on day i+1
    #   - regime_sequence has len = N - offset values
    #     regime_sequence[j] is the regime for closes[offset + j]
    #
    # We want: for each regime day j, what's the return that PRODUCED
    # that day's closing price? That's the return from the previous day
    # to this day = log_returns[offset + j - 1].
    #
    # But we need offset + j - 1 >= 0, so j >= 1. We skip the first
    # regime label (j=0) since its "producing return" might be before
    # our log_returns array starts.

    # Compute global statistics first (for shrinkage targets)
    # We use ALL returns in the lookback window, not just regime-aligned ones.
    mu_global = float(np.mean(log_returns) * 252)     # annualized
    sigma_global = float(np.std(log_returns, ddof=1) * np.sqrt(252))

    # Step 3: Group returns by regime and compute per-regime stats.
    regime_params = {}

    # Get unique regime names that actually appear in the sequence
    unique_regimes = set(regime_sequence)

    for regime_name in unique_regimes:
        # Collect all daily log returns that fall in this regime.
        # regime_sequence[j] is the regime for closes[offset + j].
        # The return for that day is log_returns[offset + j - 1].
        regime_returns = []
        for j in range(1, len(regime_sequence)):
            if regime_sequence[j] == regime_name:
                ret_idx = offset + j - 1
                if 0 <= ret_idx < len(log_returns):
                    regime_returns.append(log_returns[ret_idx])

        regime_returns = np.array(regime_returns)
        n_obs = len(regime_returns)

        if n_obs < 3:
            # Too few observations — fall back entirely to global stats.
            # We need at least 3 points for meaningful std and skewness.
            regime_params[regime_name] = {
                "mu": mu_global,
                "sigma": sigma_global,
                "skewness": 0.0,
                "n_obs": n_obs,
                "shrinkage_w": 0.0,  # 0 = fully shrunk to global
            }
            continue

        # Raw regime-specific statistics (annualized)
        mu_regime = float(np.mean(regime_returns) * 252)
        sigma_regime = float(np.std(regime_returns, ddof=1) * np.sqrt(252))
        skewness_regime = float(skew(regime_returns))

        # Apply shrinkage: blend toward global mean based on sample size.
        #
        # Think of it like a weighted average between two opinions:
        #   - "The regime-specific data says mu is X" (weight = w)
        #   - "The overall data says mu is Y" (weight = 1 - w)
        #
        # When we have lots of data for a regime (n_obs >> 60), w ≈ 1
        # and we trust the regime-specific number.
        # When data is sparse (n_obs << 60), w ≈ 0 and we fall back
        # to the global average as a safety net.
        w = n_obs / (n_obs + shrinkage_prior)

        mu_final = w * mu_regime + (1 - w) * mu_global
        sigma_final = w * sigma_regime + (1 - w) * sigma_global
        # Skewness shrinks toward 0 (symmetric), not toward global skewness,
        # because skewness is a regime-defining characteristic — bear regimes
        # SHOULD have negative skew, and we don't want to dilute that signal
        # unless we have very few observations.
        skewness_final = w * skewness_regime

        regime_params[regime_name] = {
            "mu": round(mu_final, 6),
            "sigma": round(sigma_final, 6),
            "skewness": round(skewness_final, 4),
            "n_obs": n_obs,
            "shrinkage_w": round(w, 4),
        }

    # Ensure all 4 standard regime names exist in the output, even if
    # they didn't appear in the regime sequence (rare but possible).
    for name in ["bull", "bear", "sideways", "volatile"]:
        if name not in regime_params:
            regime_params[name] = {
                "mu": mu_global,
                "sigma": sigma_global,
                "skewness": 0.0,
                "n_obs": 0,
                "shrinkage_w": 0.0,
            }

    return regime_params


# ─────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
#
# This ties everything together: takes raw price/volume data, builds
# features, trains the model, labels the regimes, and decodes.
# Call this from the API endpoint.
# ─────────────────────────────────────────────────────────────────

def run_regime_detection(
    closes: list[float],
    volumes: list[float],
    lookback_days: int = 504,
    n_states: int = 4,
) -> dict:
    """
    End-to-end regime detection for a stock.

    Parameters:
        closes        — daily closing prices, oldest first
        volumes       — daily trading volumes, same length as closes
        lookback_days — rolling window size for training (default: 504 = 2 years)
        n_states      — number of regimes to detect (default: 4)

    Returns:
        dict with:
          regimes      — list of regime names, one per day (aligned to the
                         end of the input data, with `offset` days trimmed
                         from the start for feature computation)
          probs        — today's probability for each regime
          current      — today's most likely regime name
          transitions  — labeled transition matrix
          regime_info  — metadata (colors, actions) for each regime
          offset       — how many days were trimmed from the start
          params       — model parameters for transparency
    """
    # Use only the most recent `lookback_days` for training.
    # This is the "rolling window" — the model only learns from recent
    # history, so it adapts as the stock's behavior evolves over time.
    closes_arr = np.array(closes[-lookback_days:], dtype=float)
    volumes_arr = np.array(volumes[-lookback_days:], dtype=float)

    if len(closes_arr) < 60:
        raise ValueError("Need at least 60 days of price data for regime detection")

    # Step 1: Build features
    features, offset = compute_features(closes_arr, volumes_arr)

    # Step 2: Normalize features before training.
    #
    # WHY NORMALIZE?
    # Our 4 features have very different scales:
    #   - Smoothed returns might be in the range [-0.05, +0.05]
    #   - Realized volatility might be [0.01, 0.10]
    #   - Volume ratio might be [0.3, 5.0]
    #   - Skewness might be [-2.0, +2.0]
    #
    # If we feed these raw, the model would be dominated by volume ratio
    # and skewness (because their numbers are bigger) and would barely
    # notice returns and volatility (because their numbers are tiny).
    #
    # Normalization rescales each feature so they all have mean=0 and
    # standard deviation=1. Now a "1 standard deviation move" in returns
    # is treated as equally important as a "1 standard deviation move"
    # in volume ratio. This is fair.
    feature_means = features.mean(axis=0)
    feature_stds = features.std(axis=0)
    # Avoid division by zero if a feature is constant (shouldn't happen, but safety first)
    feature_stds = np.where(feature_stds > 0, feature_stds, 1.0)
    features_normalized = (features - feature_means) / feature_stds

    # Step 3: Train the HMM
    model = train_hmm(features_normalized, n_states=n_states)

    # Step 4: Label the regimes
    labels = label_regimes(model)

    # Step 5: Decode the regime for each day
    result = decode_regimes(model, features_normalized, labels)

    # Add metadata for the API/UI
    result["regime_info"] = REGIME_INFO
    result["offset"] = offset

    # Include model parameters for transparency — so you can see what
    # the model learned about each regime's "personality"
    params = {}
    for state_num, name in labels.items():
        # Un-normalize the means so they're in the original feature units
        raw_means = model.means_[state_num] * feature_stds + feature_means
        params[name] = {
            "mean_return_5d": round(float(raw_means[0]) * 100, 3),     # as percentage
            "mean_volatility": round(float(raw_means[1]) * 100, 3),    # as percentage
            "mean_volume_ratio": round(float(raw_means[2]), 2),
            "mean_skewness": round(float(raw_means[3]), 3),
        }
    result["params"] = params

    # Step 6: Estimate per-regime GBM parameters for Monte Carlo.
    #
    # This is the bridge to the simulation engine. We compute the
    # actual annualized drift/volatility/skewness of daily returns
    # within each regime, with shrinkage for sparse regimes.
    gbm_params = estimate_regime_gbm_params(
        closes_arr,
        result["regimes"],
        offset,
    )
    result["gbm_params"] = gbm_params

    return result
