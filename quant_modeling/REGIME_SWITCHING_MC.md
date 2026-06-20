# How HMM Regime Detection Feeds Into Monte Carlo Simulation

## The Problem With Single-Regime Monte Carlo

Our current `monte_carlo.py` estimates a **single mu (drift)** and a **single sigma (volatility)** from the last year of data, then runs 10,000 price paths. Every path uses the same drift and volatility.

This is like predicting tomorrow's weather by averaging the temperature over the whole year. If it's January, you get a forecast that's way too warm. If it's July, way too cold. The average hides the reality that **the climate is in different modes at different times**.

The HMM already knows this! It has identified that the stock cycles between regimes with **different return characteristics**. A bull regime might have mu = +25% annualized with sigma = 20%, while a bear regime might have mu = -15% with sigma = 35%. Averaging these into a single "mu = +8%, sigma = 28%" loses the critical information.

## The Hamilton (1989) Framework

James Hamilton's seminal 1989 paper, *"A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle,"* formalized exactly this idea. His regime-switching model says:

> The data-generating process for returns **switches between multiple states**, where each state has its own distribution parameters. The switching is governed by a Markov chain — meaning the probability of being in each state tomorrow depends on which state you're in today.

This is exactly our HMM. Hamilton's insight was that you shouldn't model financial returns as one distribution — you should model them as a **mixture of distributions**, where the mixing is controlled by a hidden state that evolves over time.

## The Three Realities a Regime-Switching Monte Carlo Respects

### 1. Different regimes have different return distributions

In a bull market, daily returns tend to be slightly positive with moderate variance. In a bear market, returns are negative on average with **much higher** variance and **negative skew** (meaning: most bear days are small moves, but every so often there's a sharp crash). This asymmetry is critical — standard GBM assumes returns are symmetric (bell curve), which underestimates crash risk.

We estimate (mu, sigma, skewness) for each regime separately by filtering historical returns through the Viterbi sequence: "on days the HMM says were Bull, what were the actual returns like?"

### 2. Regime persistence (transition matrix)

The HMM's transition matrix tells us regimes are **sticky**. If the model says Bull->Bull probability is 96%, that means a bull market tends to last ~25 days on average before transitioning. Bear->Bear might be 92% (bear markets last ~12 days on average).

In our simulation, each path respects this stickiness. A path that starts in Bull doesn't randomly jump to Bear the next day — there's only a 4% chance of that. This is **far more realistic** than either (a) assuming the regime never changes (pure single-regime GBM) or (b) assuming the regime is random each day.

**Expected regime duration formula:**

If the self-transition probability is `p` (e.g., 0.96 for Bull->Bull), then the expected duration in that regime is `1 / (1 - p)` days. So:
- Bull->Bull = 0.96 -> expected duration = 25 trading days (~5 weeks)
- Bear->Bear = 0.92 -> expected duration = 12.5 trading days (~2.5 weeks)

### 3. Regime uncertainty at initialization

Here's a subtle but important point. The Viterbi decoder gives us the single most-likely regime sequence, but the forward-backward algorithm gives us *probabilities*: "87% Bull, 8% Sideways, 4% Bear, 1% Volatile."

When we initialize 10,000 Monte Carlo paths, we don't put all of them in Bull. We distribute them according to today's probability:
- 8,700 paths start in Bull
- 800 paths start in Sideways
- 400 paths start in Bear
- 100 paths start in Volatile

This preserves the **uncertainty about the current regime**. Those 400 paths starting in Bear will produce dramatically different forecasts, and they *should* be part of the fan chart — because there's a 4% chance we're actually in a bear market right now and the HMM just can't tell for certain.

## Per-Regime Parameter Estimation (With Shrinkage)

A practical challenge: some regimes are rare. In 2 years of data, a stock might spend 300 days in Bull, 80 in Sideways, 60 in Bear, and only 25 in Volatile. Estimating mu and sigma from 25 data points is statistically noisy — you might get wild values.

The solution is **shrinkage** (also called credibility weighting, common in actuarial science and Bayesian statistics). For sparse regimes, we blend the regime-specific estimate toward the global average:

```
weight = n_observations / (n_observations + 60)
mu_final = weight * mu_regime + (1 - weight) * mu_global
sigma_final = weight * sigma_regime + (1 - weight) * sigma_global
```

**Examples:**
- Volatile regime has 25 observations: weight = 25/85 = 0.29, so 71% of the estimate comes from the global average. The sparse data is "pulled toward safety."
- Bull regime has 300 observations: weight = 300/360 = 0.83, so it barely shrinks at all. Plenty of data — trust the regime-specific estimate.

The constant 60 in the denominator acts as a "prior sample size" — it says "I need at least 60 observations before I fully trust the regime-specific estimate." This is a common technique in Bayesian statistics and insurance pricing (credibility theory).

## Why We Can't Use the HMM's Built-in Means Directly

You might wonder: "The HMM's `model.means_` already has the mean feature vector per state — why not use those directly?"

Because those are means of *normalized, engineered features* (5-day smoothed returns, 10-day realized volatility, volume ratio, skewness) — not raw annualized GBM parameters. The HMM features are designed to be good at *detecting* regimes, but the Monte Carlo *simulation* needs raw return statistics:

| What HMM has | What Monte Carlo needs |
|---|---|
| 5-day smoothed mean return (normalized) | Annualized mu from daily log returns |
| 10-day realized volatility (normalized) | Annualized sigma from daily log returns |
| Volume ratio (not useful for simulation) | Skewness of daily log returns |

We get what Monte Carlo needs by filtering the raw daily log returns through the Viterbi regime labels and computing statistics on each group separately.

## Skew-Normal Distributions for Bear Regimes

Standard GBM uses a normal (bell-curve) distribution for daily returns — symmetric, with equal probability of going up or down by the same amount. But real bear markets have **negative skew**: most days are small moves (even slightly positive), punctuated by occasional sharp drops (the "fat left tail").

If we measure skewness in a regime and find it's significantly negative (< -0.1), we use scipy's **skew-normal distribution** instead of the standard normal. The skew-normal distribution has three parameters:
- **Location** (like the mean of a normal distribution)
- **Scale** (like the standard deviation)
- **Shape** (controls the asymmetry — negative shape = longer left tail)

This gives us a distribution that's still bell-shaped but has a longer left tail — capturing the crash risk that standard GBM misses.

**Why this matters for you as an investor:** If the model says "this stock is in a bear regime," the fan chart's downside should extend further than the upside. A symmetric fan chart in a bear regime would be lying to you about the risk.

## What This Looks Like Visually

### Bull regime with high confidence (e.g., 87% Bull)
- Fan chart is relatively **narrow** (low uncertainty)
- Center of the fan **drifts upward** (positive mu)
- Fan is roughly **symmetric** around the median
- The message: "Things are calm. The model expects gradual growth. Accumulate on dips."

### Bear regime (e.g., 70% Bear)
- Fan chart is much **wider** (bear = high volatility)
- Center **drifts downward** (negative mu)
- Fan is **asymmetric** — the downside tail extends further than the upside (negative skew)
- The message: "Risk is elevated. The model sees more downside than upside. Hold core, don't add."

### Uncertain regime (e.g., 50% Bull, 30% Sideways, 20% Bear)
- Fan chart is wider than pure Bull but less directional
- Reflects **genuine uncertainty** about which regime we're in
- Some paths evolve through Bull dynamics, others through Bear dynamics
- The message: "The model isn't sure what's happening. Wait for clarity."

## Simulation Algorithm (Step by Step)

```
For each of the 10,000 paths:
    1. Sample starting regime from today's probability distribution
       (e.g., draw from {Bull: 0.87, Sideways: 0.08, Bear: 0.04, Volatile: 0.01})

    For each day t = 1 to 60:
        2. Sample next regime from the Markov transition matrix
           (e.g., if currently Bull, 96% stay Bull, 2% -> Sideways, 1.5% -> Bear, 0.5% -> Volatile)

        3. Look up that regime's parameters (mu, sigma, skewness)

        4. Generate today's return:
           - If |skewness| > 0.1: draw from skew-normal distribution
           - Otherwise: draw from standard normal (regular GBM step)

        5. Update price: price[t] = price[t-1] * exp(daily_return)

After all paths are complete:
    6. Compute percentiles (P5, P25, P50, P75, P95) at each day
    7. Return as fan chart data
```

## Vectorization Strategy

The inner loop (10,000 paths x 60 days = 600,000 iterations) would be very slow in pure Python. The vectorization approach:

At each timestep, group all paths by their current regime, then generate returns for all paths in the same regime with a single numpy call. This reduces the inner loop from 10,000 iterations to ~4 numpy calls per timestep (one per regime). Total: 4 x 60 = 240 vectorized operations instead of 600,000 scalar operations.

## References

- Hamilton, J.D. (1989). "A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle." *Econometrica*, 57(2), 357-384.
- Guidolin, M. & Timmermann, A. (2007). "Asset Allocation Under Multivariate Regime Switching." *Journal of Economic Dynamics and Control*, 31(11), 3503-3544.
- Ang, A. & Bekaert, G. (2002). "Regime Switches in Interest Rates." *Journal of Business and Economic Statistics*, 20(2), 163-182.
