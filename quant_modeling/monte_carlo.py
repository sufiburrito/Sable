"""
Monte Carlo simulation engine for stock price forecasting.

This module generates thousands of possible future price paths using
Geometric Brownian Motion (GBM), then summarizes them as percentile
bands (a "fan chart"). The fan chart shows where the price is likely
to end up — not as a single prediction, but as a probability envelope.

Core concepts:
  - GBM models stock prices as a random walk with drift
  - "Drift" (mu) = the stock's average annualized return
  - "Volatility" (sigma) = how much the stock bounces around, annualized
  - Each simulation step: next_price = price * exp((mu - sigma²/2)*dt + sigma*sqrt(dt)*Z)
    where Z is a random draw from a standard normal distribution (bell curve)
  - Run thousands of simulations → get a distribution of outcomes
  - Summarize as percentile bands: P5, P25, P50 (median), P75, P95
"""

import numpy as np
from scipy.stats import skewnorm


def estimate_parameters(closes: list[float], lookback_days: int = 252) -> dict:
    """
    Estimate drift (mu) and volatility (sigma) from historical closing prices.

    We work with LOG RETURNS because stock returns are multiplicative:
      - If a stock goes from 100 → 110, that's +10%
      - If it then goes 110 → 99, that's -10%
      - But 100 * 1.10 * 0.90 = 99, not 100 — returns compound, they don't add
      - Log returns DO add: log(110/100) + log(99/110) = log(99/100)
      - This makes them much easier to work with mathematically

    Parameters:
        closes: list of closing prices, oldest first
        lookback_days: how many recent trading days to use for estimation.
                       252 ≈ 1 year of trading days on NSE.

    Returns:
        dict with:
          mu    — annualized drift (expected return per year)
          sigma — annualized volatility (standard deviation of returns per year)
          n     — number of daily returns used in estimation
    """
    prices = np.array(closes[-lookback_days:], dtype=float)

    # Log returns: ln(price_today / price_yesterday) for each consecutive pair
    # This gives us the continuously compounded daily return.
    log_returns = np.diff(np.log(prices))

    # Annualize: multiply daily mean by 252 (trading days/year)
    # and daily std dev by sqrt(252). The sqrt scaling comes from
    # the statistical property that variance of a sum of independent
    # variables equals the sum of variances. If daily variance is v,
    # then 252-day variance is 252*v, so 252-day std dev is sqrt(252)*daily_std.
    mu_daily = np.mean(log_returns)
    sigma_daily = np.std(log_returns, ddof=1)  # ddof=1 for sample std dev

    mu_annual = mu_daily * 252
    sigma_annual = sigma_daily * np.sqrt(252)

    return {
        "mu": float(mu_annual),
        "sigma": float(sigma_annual),
        "n": len(log_returns),
    }


def simulate(
    s0: float,
    mu: float,
    sigma: float,
    days: int = 60,
    n_sims: int = 10_000,
    seed: int | None = 42,
) -> np.ndarray:
    """
    Run Monte Carlo simulation using Geometric Brownian Motion (GBM).

    For each simulation, we walk forward `days` steps from starting price s0.
    At each step, we draw a random number Z from a standard normal distribution
    and compute:

        price[t+1] = price[t] * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)

    Why "mu - sigma²/2"?  This is the Itô correction. In continuous-time finance,
    the EXPECTED value of a lognormal process exp(X) is exp(E[X] + Var[X]/2).
    If we naively used mu as the drift in log-space, the expected price would
    actually grow faster than mu. Subtracting sigma²/2 corrects for this, so
    that the AVERAGE of all simulated paths grows at rate mu, as intended.

    We simulate ALL paths at once using numpy vectorization — no Python loops
    over the 10,000 simulations. This makes it fast (~50ms for 10K × 60 days).

    Parameters:
        s0     — starting price (today's closing price)
        mu     — annualized drift (from estimate_parameters)
        sigma  — annualized volatility (from estimate_parameters)
        days   — number of trading days to simulate forward
        n_sims — number of independent price paths to generate
        seed   — random seed for reproducibility (None for true random)

    Returns:
        np.ndarray of shape (n_sims, days + 1)
        Column 0 is s0 for all rows. Column `days` is the final simulated price.
    """
    if seed is not None:
        rng = np.random.default_rng(seed)
    else:
        rng = np.random.default_rng()

    dt = 1 / 252  # one trading day as a fraction of a year

    # Draw ALL random numbers at once: a matrix of (n_sims × days) standard normals.
    # Each row is one simulation's sequence of daily random shocks.
    Z = rng.standard_normal((n_sims, days))

    # Compute the daily log-return for every simulation and every day.
    # This is the exponent in our GBM formula, applied element-wise.
    #   drift part:   (mu - sigma²/2) * dt       — same for every simulation
    #   random part:  sigma * sqrt(dt) * Z[i,t]   — different for each sim & day
    daily_log_returns = (mu - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * Z

    # Convert log returns to cumulative log prices by summing along the time axis.
    # np.cumsum along axis=1 gives us: [r1, r1+r2, r1+r2+r3, ...]
    # Then exp() converts from log-space back to price-space.
    # Prepend a column of zeros (day 0 = no change yet) so we get days+1 columns.
    cumulative_log = np.cumsum(daily_log_returns, axis=1)
    cumulative_log = np.hstack([np.zeros((n_sims, 1)), cumulative_log])

    # Multiply by starting price to get actual price paths
    paths = s0 * np.exp(cumulative_log)

    return paths


def fan_chart_percentiles(
    paths: np.ndarray,
    percentiles: list[int] = [5, 25, 50, 75, 95],
) -> dict[int, list[float]]:
    """
    Summarize simulation paths into percentile bands for visualization.

    Given 10,000 simulated paths, at each future day we compute:
      P5  — only 5% of simulations went below this (optimistic lower bound)
      P25 — lower quartile
      P50 — median outcome (the "most typical" path)
      P75 — upper quartile
      P95 — only 5% of simulations went above this (optimistic upper bound)

    The P5-to-P95 band contains 90% of all simulated outcomes.
    The P25-to-P75 band contains 50% of outcomes (the "likely" zone).

    Parameters:
        paths       — (n_sims, days+1) array from simulate()
        percentiles — which percentiles to compute

    Returns:
        dict mapping percentile → list of prices (one per day, including day 0)
        Example: {5: [100, 98.2, 96.5, ...], 50: [100, 100.8, 101.2, ...], ...}
    """
    result = {}
    for p in percentiles:
        # np.percentile along axis=0 computes the percentile across all
        # simulations for each day independently.
        values = np.percentile(paths, p, axis=0)
        result[p] = [round(float(v), 2) for v in values]
    return result


# ─────────────────────────────────────────────────────────────────
# REGIME-SWITCHING MONTE CARLO SIMULATION
#
# This is the upgrade from single-regime GBM. Instead of using one
# (mu, sigma) for all 10,000 paths, each path carries its own
# "current regime" that evolves over time according to the HMM's
# Markov transition matrix.
#
# Hamilton (1989) showed that financial returns are better modeled
# as switching between regimes with different distributions. This
# function implements that insight as a Monte Carlo simulation.
#
# THE ALGORITHM:
#
# 1. INITIALIZE: Each path samples its starting regime from today's
#    probability distribution (e.g., 87% bull, 8% sideways, 4% bear).
#    This preserves UNCERTAINTY about the current state.
#
# 2. EACH DAY, FOR EACH PATH:
#    a. Sample next regime from the Markov transition matrix
#       (e.g., if currently bull, 96% chance of staying bull)
#    b. Look up that regime's (mu, sigma, skewness)
#    c. Generate a daily return using either:
#       - Standard normal (if skewness is near zero)
#       - Skew-normal distribution (if the regime has asymmetric returns)
#    d. Update price: price *= exp(daily_return)
#
# VECTORIZATION:
# Running 10,000 paths × 60 days = 600,000 iterations in Python loops
# would be painfully slow. Instead, we vectorize BY REGIME: at each
# timestep, we group all paths by their current regime and generate
# all their returns in one numpy call. Since there are only 4 regimes,
# this means ~4 numpy calls per timestep instead of 10,000.
# ─────────────────────────────────────────────────────────────────

def _skewnorm_shape_from_skewness(target_skew: float) -> float:
    """
    Approximate the skew-normal 'a' (shape) parameter that produces
    a given skewness.

    The skew-normal distribution has 3 parameters: location, scale, and
    shape (a). When a=0, it's a standard normal. When a>0, the
    distribution has a longer right tail. When a<0, longer left tail.

    The exact relationship between 'a' and skewness is complex:
        skewness = ((4 - pi)/2) * (delta^3) / (1 - 2*delta^2/pi)^(3/2)
        where delta = a / sqrt(1 + a^2)

    We use a simple approximation that works well for |skewness| < 1.5:
    the shape parameter 'a' is roughly proportional to the target skewness,
    scaled by a factor derived from the formula above.

    For bear regimes (skewness ≈ -0.5), this gives a ≈ -1.0, which
    produces a distribution with a noticeably longer left tail —
    capturing the "occasional sharp drop" pattern of real bear markets.
    """
    # Clamp to avoid extreme values
    target_skew = max(-1.5, min(1.5, target_skew))

    # Approximation: a ≈ skewness * 2.0 works reasonably well
    # For small skewness, this is very close to the true inverse.
    # For |skew| > 1, it slightly underestimates, which is conservative.
    return target_skew * 2.0


def simulate_regime_switching(
    s0: float,
    regime_probs: dict[str, float],
    regime_params: dict[str, dict],
    transmat: dict[str, dict[str, float]],
    days: int = 60,
    n_sims: int = 10_000,
    seed: int | None = 42,
) -> np.ndarray:
    """
    Run Monte Carlo simulation with regime-switching dynamics.

    Unlike the single-regime simulate(), this function:
      - Starts each path in a regime sampled from today's probabilities
      - Transitions between regimes using the HMM's Markov matrix
      - Uses different (mu, sigma, skewness) for each regime
      - Applies skew-normal returns for regimes with asymmetric tails

    Parameters:
        s0           — starting price (today's close)
        regime_probs — today's regime probability distribution
                       e.g., {"bull": 0.87, "sideways": 0.08, "bear": 0.04, "volatile": 0.01}
        regime_params — per-regime GBM parameters from estimate_regime_gbm_params()
                       e.g., {"bull": {"mu": 0.25, "sigma": 0.20, "skewness": 0.05}, ...}
        transmat     — Markov transition matrix from HMM
                       e.g., {"bull": {"bull": 0.96, "bear": 0.01, ...}, ...}
        days         — number of trading days to simulate forward
        n_sims       — number of independent price paths to generate
        seed         — random seed for reproducibility

    Returns:
        np.ndarray of shape (n_sims, days + 1)
        Column 0 is s0 for all rows. Same format as simulate().
    """
    rng = np.random.default_rng(seed)
    dt = 1 / 252  # one trading day as fraction of a year

    # ── Convert regime names to integer indices for fast array operations ──
    # We need to work with integer arrays, not string arrays, for speed.
    regime_names = sorted(regime_probs.keys())
    name_to_idx = {name: i for i, name in enumerate(regime_names)}
    n_regimes = len(regime_names)

    # Build probability arrays from the string-keyed dicts
    # init_probs[i] = probability of starting in regime i
    init_probs = np.array([regime_probs.get(name, 0.0) for name in regime_names])
    # Normalize (in case probabilities don't sum to exactly 1.0 due to rounding)
    init_probs = init_probs / init_probs.sum()

    # trans_matrix[i, j] = probability of transitioning from regime i to regime j
    trans_matrix = np.zeros((n_regimes, n_regimes))
    for i, name_i in enumerate(regime_names):
        row = transmat.get(name_i, {})
        for j, name_j in enumerate(regime_names):
            trans_matrix[i, j] = row.get(name_j, 0.0)
        # Normalize each row to sum to 1
        row_sum = trans_matrix[i].sum()
        if row_sum > 0:
            trans_matrix[i] /= row_sum

    # Per-regime simulation parameters (annualized)
    mus = np.array([regime_params[name]["mu"] for name in regime_names])
    sigmas = np.array([regime_params[name]["sigma"] for name in regime_names])
    skews = np.array([regime_params[name]["skewness"] for name in regime_names])

    # Pre-compute daily drift for each regime (Itô-corrected)
    # daily_drift[i] = (mu_i - sigma_i^2 / 2) * dt
    daily_drifts = (mus - 0.5 * sigmas**2) * dt
    # daily_vol[i] = sigma_i * sqrt(dt)
    daily_vols = sigmas * np.sqrt(dt)

    # Pre-compute skew-normal shape parameters for regimes with significant skew
    skew_shapes = np.array([
        _skewnorm_shape_from_skewness(s) if abs(s) > 0.1 else 0.0
        for s in skews
    ])

    # ── Initialize paths ──
    # paths[sim, day] = price at that simulation and day
    paths = np.empty((n_sims, days + 1))
    paths[:, 0] = s0  # day 0 = today's price for all paths

    # Sample starting regime for each path from today's probability distribution.
    # This is key: we DON'T put all paths in the Viterbi regime.
    # If today is "87% bull, 8% sideways, 4% bear, 1% volatile",
    # then ~8,700 paths start in bull, ~800 in sideways, etc.
    current_regimes = rng.choice(n_regimes, size=n_sims, p=init_probs)

    # ── Simulate forward day by day ──
    for t in range(days):
        # Step A: Transition all paths to their next regime.
        # For each path, sample the next regime from the transition matrix
        # row corresponding to its current regime.
        #
        # Vectorized approach: group paths by current regime, then sample
        # next regimes for each group in one call.
        next_regimes = np.empty(n_sims, dtype=int)
        daily_returns = np.empty(n_sims)

        for r in range(n_regimes):
            # Find all paths currently in regime r
            mask = current_regimes == r
            count = mask.sum()
            if count == 0:
                continue

            # Sample next regime for all paths in this group
            # trans_matrix[r] is the probability vector for transitions FROM regime r
            next_regimes[mask] = rng.choice(n_regimes, size=count, p=trans_matrix[r])

            # Step B: Generate daily returns for paths NOW in regime r.
            # (We use the CURRENT regime's parameters for today's return,
            # then update the regime for tomorrow.)
            drift = daily_drifts[r]
            vol = daily_vols[r]

            if abs(skew_shapes[r]) > 0.01:
                # Skew-normal return: for regimes with asymmetric tails.
                # The skewnorm.rvs function generates random numbers from
                # a skew-normal distribution. We then rescale to have the
                # desired drift and volatility.
                #
                # skewnorm(a) has mean ≈ a*sqrt(2/pi) / sqrt(1+a^2) and
                # variance ≈ 1 - mean^2. We need to adjust for this to
                # get the right final drift and vol.
                a = skew_shapes[r]
                Z = skewnorm.rvs(a, size=count, random_state=rng)
                # Standardize: subtract the skewnorm's theoretical mean and
                # divide by its theoretical std so that Z has mean≈0, std≈1.
                # Then our drift and vol scaling work correctly.
                delta = a / np.sqrt(1 + a**2)
                sn_mean = delta * np.sqrt(2 / np.pi)
                sn_var = 1 - 2 * delta**2 / np.pi
                sn_std = np.sqrt(sn_var)
                Z = (Z - sn_mean) / sn_std
            else:
                # Standard normal: symmetric returns (bull, sideways).
                Z = rng.standard_normal(count)

            # Apply the GBM formula: log_return = drift + vol * Z
            daily_returns[mask] = drift + vol * Z

        # Step C: Update regimes for the next timestep
        current_regimes = next_regimes

        # Step D: Update prices using the GBM formula
        # price[t+1] = price[t] * exp(daily_return)
        paths[:, t + 1] = paths[:, t] * np.exp(daily_returns)

    return paths


def run_simulation(
    closes: list[float],
    days_forward: int = 60,
    n_sims: int = 10_000,
    lookback_days: int = 252,
    regime_data: dict | None = None,
) -> dict:
    """
    End-to-end: estimate parameters from history, simulate, return fan chart data.

    This is the main entry point — call this from the API endpoint.

    If regime_data is provided (from run_regime_detection()), the simulation
    uses regime-switching dynamics: each path evolves through different regimes
    with different (mu, sigma, skewness), governed by the HMM's transition
    matrix. This produces a fan chart that reflects the current market regime
    — wider in volatile/bear periods, narrower in bull periods, and
    asymmetric (skewed downward) during bear regimes.

    If regime_data is None, falls back to single-regime GBM (the original
    behavior): one global (mu, sigma) estimated from all historical data.

    Parameters:
        closes        — historical closing prices, oldest first
        days_forward  — how many trading days to project forward
        n_sims        — number of simulation paths
        lookback_days — how many historical days to use for parameter estimation
        regime_data   — output from run_regime_detection() (optional)

    Returns:
        dict with:
          params     — {mu, sigma, n} estimated global parameters
          fan        — {5: [...], 25: [...], 50: [...], 75: [...], 95: [...]}
          days       — number of forward days (for x-axis generation)
          start_price — the price simulations start from (last close)
          regime_conditional — True if regime-switching was used
          regime_params — per-regime GBM parameters (if regime-switching)
    """
    if len(closes) < 30:
        raise ValueError("Need at least 30 closing prices for meaningful estimation")

    # Always compute global parameters (shown in the header stats)
    params = estimate_parameters(closes, lookback_days=lookback_days)
    start_price = closes[-1]

    # If we have regime data, use regime-switching simulation
    if regime_data and "gbm_params" in regime_data and "transitions" in regime_data:
        paths = simulate_regime_switching(
            s0=start_price,
            regime_probs=regime_data["probs"],
            regime_params=regime_data["gbm_params"],
            transmat=regime_data["transitions"],
            days=days_forward,
            n_sims=n_sims,
        )
        fan = fan_chart_percentiles(paths)
        return {
            "params": params,
            "fan": fan,
            "days": days_forward,
            "start_price": start_price,
            "regime_conditional": True,
            "regime_params": regime_data["gbm_params"],
        }

    # Fallback: single-regime GBM (original behavior)
    paths = simulate(
        s0=start_price,
        mu=params["mu"],
        sigma=params["sigma"],
        days=days_forward,
        n_sims=n_sims,
    )
    fan = fan_chart_percentiles(paths)

    return {
        "params": params,
        "fan": fan,
        "days": days_forward,
        "start_price": start_price,
        "regime_conditional": False,
    }
