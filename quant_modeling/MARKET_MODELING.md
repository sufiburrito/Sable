# Market Modeling for Algorithmic Investment

A comprehensive reference for building quantitative models into TradeCentral. Written for a long-term investor working with Indian (NSE) equities, Python tooling, and Plotly visualization.

---

## Table of Contents

1. [The Modeling Landscape](#1-the-modeling-landscape)
2. [Regime Detection with Hidden Markov Models](#2-regime-detection-with-hidden-markov-models)
3. [Monte Carlo Simulation](#3-monte-carlo-simulation)
4. [Time Series Forecasting](#4-time-series-forecasting)
5. [Mean Reversion Models](#5-mean-reversion-models)
6. [Momentum & Factor Models](#6-momentum--factor-models)
7. [Volatility Modeling](#7-volatility-modeling)
8. [Support/Resistance Detection (Algorithmic)](#8-supportresistance-detection-algorithmic)
9. [Backtesting Framework](#9-backtesting-framework)
10. [Risk Modeling](#10-risk-modeling)
11. [Portfolio Optimization](#11-portfolio-optimization)
12. [Sentiment & Alternative Data](#12-sentiment--alternative-data)
13. [Machine Learning for Signal Generation](#13-machine-learning-for-signal-generation)
14. [Implementation Roadmap for TradeCentral](#14-implementation-roadmap-for-tradecentral)

---

## 1. The Modeling Landscape

Market models fall into a few broad families, each answering a different question:

| Family | Core Question | Example |
|--------|--------------|---------|
| **Regime detection** | What state is the market in right now? | HMM, Markov switching |
| **Forecasting** | Where is price likely to go? | ARIMA, Prophet, LSTM |
| **Simulation** | What are the possible futures? | Monte Carlo, bootstrap |
| **Mean reversion** | Has price deviated too far from fair value? | Bollinger, Ornstein-Uhlenbeck |
| **Momentum** | Is the current trend likely to continue? | Time-series momentum, cross-sectional |
| **Volatility** | How uncertain is the near future? | GARCH, realized vol |
| **Risk** | How much can I lose? | VaR, CVaR, max drawdown |
| **Optimization** | How should I allocate capital? | Markowitz, Kelly criterion |

No single model is "correct." The power comes from combining them — use regime detection to know *when* a model applies, forecasting to generate signals, simulation to stress-test, and risk models to size positions.

### How They Fit Into TradeCentral

Your system already has:
- **Alert levels** (human + Claude analysis) — discretionary support/resistance
- **Floor context** (ATR + backtest median drawdown) — statistical floor estimates
- **OHLCV history** via yfinance — the raw fuel for all models
- **Plotly web UI** — the visualization layer

The models below add a *quantitative layer* that complements the existing discretionary analysis. They don't replace Claude's judgment — they give it better inputs.

---

## 2. Regime Detection with Hidden Markov Models

### What It Is

A Hidden Markov Model (HMM) assumes the market is always in one of several hidden "states" (regimes) — say, **bull**, **bear**, and **sideways**. You can't directly observe the state, but you can observe symptoms (returns, volatility, volume). The HMM infers which state is most likely given the observations.

### Why It Matters for You

As a long-term investor, you don't need to predict exact prices. You need to know: *Is this a good time to accumulate, or should I wait?* Regime detection answers this directly. If the model says "we just transitioned from bear to bull," that's accumulation territory. If it says "we're in a high-volatility bear regime," that's when you hold core and wait.

### The Math (Simplified)

An HMM has three components:

1. **States** (S): e.g., {Bull, Bear, Sideways} — the hidden variable
2. **Transition matrix** (A): probability of moving from one state to another
   ```
   A = [[0.95, 0.03, 0.02],   # Bull → Bull/Bear/Sideways
        [0.04, 0.93, 0.03],   # Bear → Bull/Bear/Sideways
        [0.05, 0.05, 0.90]]   # Sideways → Bull/Bear/Sideways
   ```
   High diagonal = regimes are "sticky" (they persist for a while)

3. **Emission distributions** (B): what returns/volatility look like in each state
   - Bull: mean return +0.08%/day, low vol
   - Bear: mean return -0.12%/day, high vol
   - Sideways: mean return ~0%, moderate vol

The model is trained on historical data using the **Baum-Welch algorithm** (a special case of Expectation-Maximization). Once trained, the **Viterbi algorithm** gives you the most likely sequence of regimes, and **forward-backward** gives you the probability of being in each regime at each point.

### Python Implementation

```python
from hmmlearn.hmm import GaussianHMM
import numpy as np

# Features: [daily_return, realized_vol_5d, volume_ratio]
X = np.column_stack([returns, rolling_vol, volume_ratio])

model = GaussianHMM(
    n_components=3,       # 3 regimes
    covariance_type="full",
    n_iter=200,
    random_state=42,
)
model.fit(X)

# Predict most likely regime for each day
regimes = model.predict(X)

# Probability of being in each regime right now
probs = model.predict_proba(X)
current_regime_probs = probs[-1]  # latest day
```

**Library:** `hmmlearn` (pip install hmmlearn)

### Visualization in TradeCentral

- **Chart overlay**: Color-code the candlestick background by regime (green tint = bull, red tint = bear, neutral = sideways)
- **Regime probability bar**: Small stacked bar below the chart showing P(bull), P(bear), P(sideways) for the current day
- **Transition alerts**: Notify when regime switches (e.g., "HMM: Bear → Bull transition detected")

### Gotchas

- **Number of states**: 2-4 states work well. More than 5 overfits. Use BIC/AIC to choose.
- **Feature selection**: Returns alone are noisy. Adding volatility and volume helps dramatically.
- **Look-ahead bias**: Train on past data only. Re-train periodically (e.g., quarterly).
- **Non-stationarity**: Markets change. A model trained on 2010-2015 may not describe 2024-2026. Use a rolling training window.
- **Indian market specifics**: NSE has different volatility characteristics than US markets. Trained separately per stock or per sector, not globally.

---

## 3. Monte Carlo Simulation

### What It Is

Monte Carlo simulation generates thousands of possible future price paths by sampling from a statistical model of returns. Instead of one forecast ("price will be ₹500"), you get a *distribution* of outcomes ("there's a 70% chance price stays between ₹450 and ₹550 over the next 30 days").

### Why It Matters for You

When you're deciding whether to accumulate at a support level, Monte Carlo tells you: "If you buy here, what's the distribution of outcomes in 30/60/90 days?" It quantifies the risk of your entry, and gives you probabilistic bands for setting alert levels.

### The Math

**Geometric Brownian Motion (GBM)** — the simplest model:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `S(t)` = price at time t
- `mu` = drift (expected annual return, estimated from history)
- `sigma` = volatility (annualized standard deviation of log returns)
- `Z` = random draw from standard normal distribution
- `dt` = time step (1/252 for daily)

GBM assumes log-normal returns, which is a reasonable starting point but misses:
- **Fat tails**: Real markets have more extreme moves than normal distribution predicts
- **Volatility clustering**: High-vol days cluster together
- **Regime changes**: Different market conditions produce different parameters

**Improvements over basic GBM:**
1. **Student-t distribution** instead of normal — captures fat tails
2. **GARCH volatility** — time-varying sigma that clusters
3. **Regime-switching GBM** — different mu/sigma per HMM regime
4. **Bootstrap simulation** — resample actual historical returns instead of assuming a distribution (non-parametric, preserves whatever weirdness the real data has)

### Python Implementation

```python
import numpy as np

def monte_carlo_gbm(S0, mu, sigma, days, n_sims=10000):
    """Simulate n_sims price paths using Geometric Brownian Motion."""
    dt = 1 / 252
    paths = np.zeros((n_sims, days + 1))
    paths[:, 0] = S0

    for t in range(1, days + 1):
        Z = np.random.standard_normal(n_sims)
        paths[:, t] = paths[:, t-1] * np.exp(
            (mu - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * Z
        )
    return paths

# Example: simulate STLTECH 90 days forward
paths = monte_carlo_gbm(S0=320, mu=0.15, sigma=0.45, days=90, n_sims=10000)

# Fan chart percentiles
p5  = np.percentile(paths, 5, axis=0)
p25 = np.percentile(paths, 25, axis=0)
p50 = np.percentile(paths, 50, axis=0)
p75 = np.percentile(paths, 75, axis=0)
p95 = np.percentile(paths, 95, axis=0)
```

### Visualization in TradeCentral

**Fan chart** — the signature Monte Carlo visual:
- Filled bands showing P5-P95, P25-P75, and the median (P50) line
- Extends forward from the last candle as a "forecast cone"
- Color: graduated opacity (wider bands more transparent)

This is a natural Plotly trace: filled scatter area between percentile curves.

**Value-at-Risk overlay**: A single line showing "5th percentile outcome" — the worst case in 95% of simulations.

### Gotchas

- **Garbage in, garbage out**: mu and sigma estimated from history may not represent the future. Use rolling estimates, or better, regime-conditional estimates.
- **10,000 simulations** is a good default. More is diminishing returns. Fewer gives noisy percentiles.
- **Don't simulate too far forward**: Beyond 90 days, the fan chart becomes so wide it's useless. Short-to-medium term is where Monte Carlo is actionable.
- **Bootstrap > GBM for Indian mid-caps**: Mid/small cap NSE stocks often have non-normal return distributions. Bootstrapping actual returns preserves the idiosyncratic behavior.

---

## 4. Time Series Forecasting

### ARIMA / SARIMAX

**What**: Auto-Regressive Integrated Moving Average. Models price/return as a function of its own lagged values and past forecast errors.

- AR(p): price depends on its own past p values
- I(d): differencing to make series stationary (usually d=1 for prices)
- MA(q): depends on past q forecast errors

```python
from statsmodels.tsa.arima.model import ARIMA

model = ARIMA(prices, order=(5, 1, 2))
fit = model.fit()
forecast = fit.forecast(steps=30)  # 30 days ahead
conf_int = fit.get_forecast(30).conf_int()  # confidence intervals
```

**When to use**: Short-term mean reversion signals. If ARIMA predicts price will revert upward, it's a quantitative buy signal that complements your alert levels.

**Library**: `statsmodels`

### Facebook Prophet

**What**: Additive decomposition model — trend + seasonality + holidays. Designed for business time series but works for stock analysis as a baseline.

```python
from prophet import Prophet
import pandas as pd

df = pd.DataFrame({'ds': dates, 'y': prices})
m = Prophet(daily_seasonality=False, weekly_seasonality=True)
m.fit(df)
future = m.make_future_dataframe(periods=60)
forecast = m.predict(future)
```

**When to use**: Identifying structural trends and seasonal patterns. Indian markets have calendar effects (budget season, earnings seasons, Diwali rally) that Prophet can capture.

**Library**: `prophet`

### LSTM / Neural Forecasting

**What**: Recurrent neural networks that learn temporal patterns from sequences of features (price, volume, technical indicators).

**When to use**: When you have enough data (5+ years of daily data minimum) and want to capture complex nonlinear patterns. Requires more engineering effort.

**Library**: `pytorch` or `tensorflow`, or higher-level `darts` (unified forecasting library that includes ARIMA, Prophet, and neural models)

### Honest Assessment

Forecasting stock prices is *hard*. Academic evidence shows:
- Short-term (1-5 day) forecasts are barely better than random for liquid large-caps
- Mean reversion signals work better for less liquid stocks (many NSE mid-caps qualify)
- Ensemble forecasts (combining multiple models) outperform any single model
- **Forecast intervals matter more than point forecasts** — the width of the confidence band tells you how uncertain the model is

For your use case as a long-term investor, forecasting is most useful not for "price will be ₹X" predictions, but for:
1. Identifying when a stock is statistically oversold (confidence band floor)
2. Setting data-driven alert levels (place BUY alerts near forecast support bands)
3. Comparing current price to the "fair value" envelope

---

## 5. Mean Reversion Models

### The Thesis

Some stocks (especially mature, dividend-paying, range-bound ones) tend to revert to a mean. When price deviates significantly, there's a statistical pull back toward the average.

### Ornstein-Uhlenbeck Process

The mathematical formalization of mean reversion:

```
dS = theta * (mu - S) * dt + sigma * dW
```

- `theta` = speed of mean reversion (higher = snaps back faster)
- `mu` = long-term mean price
- `sigma` = volatility of deviations
- `dW` = random noise

**Estimation**: Fit via linear regression of `dS` on `(mu - S)`:

```python
import numpy as np
from scipy.optimize import minimize

def fit_ou(prices, dt=1/252):
    """Estimate Ornstein-Uhlenbeck parameters from price series."""
    log_prices = np.log(prices)
    dX = np.diff(log_prices)
    X = log_prices[:-1]

    # OLS: dX = a + b*X + error
    b, a = np.polyfit(X, dX, 1)

    theta = -b / dt           # mean reversion speed
    mu = -a / b               # long-term mean (in log space)
    residuals = dX - (a + b * X)
    sigma = np.std(residuals) / np.sqrt(dt)

    half_life = np.log(2) / theta  # days to revert halfway

    return {
        'theta': theta,
        'mu': np.exp(mu),     # convert back to price space
        'sigma': sigma,
        'half_life_days': half_life,
    }
```

### Half-Life

The **half-life** is the key output: how many days it takes for a deviation to revert halfway. If a stock has a 15-day half-life and is currently 10% below its mean, you'd expect it to be ~5% below in 15 days.

- **Short half-life (< 20 days)**: Strong mean reversion — good candidates for buy-the-dip strategies
- **Long half-life (> 60 days)**: Weak mean reversion — trend-following works better here

### Visualization

- **Mean reversion band**: Plot the OU mean ± 1/2 sigma as a channel. Price outside the band = stretched, likely to revert.
- **Z-score indicator**: `(price - mu) / sigma` — below -2 is deeply oversold, above +2 is overbought.

### Relevance to Your Stocks

Your existing floor context (ATR + median drawdown) is already a form of mean reversion analysis. The OU model makes it more rigorous by estimating the actual *speed* of reversion and the equilibrium price.

---

## 6. Momentum & Factor Models

### Time-Series Momentum

The simplest: if a stock has been going up over the past N months, it's likely to continue going up. Academic evidence (Moskowitz, Ooi, Pedersen 2012) shows this works across asset classes.

```python
def momentum_signal(prices, lookback=60):
    """Return >0 for positive momentum, <0 for negative."""
    return (prices[-1] / prices[-lookback]) - 1

# A momentum score of +0.15 means +15% over the lookback period
```

**For your system**: Momentum scores could influence alert confidence levels. A BUY alert at a support level is more compelling if momentum is turning positive (transition from negative to positive momentum).

### Cross-Sectional Momentum (Relative Strength)

Rank all your stocks by recent performance. Overweight the top performers, underweight the bottom. This is the basis of most systematic equity strategies.

```python
def rank_stocks(tickers_prices, lookback=60):
    """Rank stocks by momentum, return sorted list."""
    scores = {}
    for ticker, prices in tickers_prices.items():
        scores[ticker] = (prices[-1] / prices[-lookback]) - 1
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

### Factor Models

Fama-French style: decompose stock returns into factors:
- **Market** (beta): how much the stock moves with NIFTY
- **Size**: small-caps tend to outperform (over long periods)
- **Value**: low P/B stocks tend to outperform
- **Momentum**: recent winners continue winning
- **Quality**: high ROE, low debt stocks outperform

Each factor has an expected premium. A stock's exposure to these factors explains most of its return behavior and risk.

**For your system**: Factor exposure analysis for each stock in your watchlist — shown in the stock detail view. "CGPOWER: high momentum, high beta, medium quality."

---

## 7. Volatility Modeling

### Why Volatility Matters

Volatility is the key input for:
- Sizing positions (higher vol = smaller position)
- Setting alert band widths (your ATR zones already do this)
- Options pricing (if you ever trade options)
- Monte Carlo simulation (time-varying sigma)

### GARCH (Generalized Autoregressive Conditional Heteroskedasticity)

Models the fact that volatility clusters — big moves follow big moves.

```python
from arch import arch_model

# Fit GARCH(1,1) to daily returns
am = arch_model(returns * 100, vol='Garch', p=1, q=1)
res = am.fit(disp='off')

# Forecast volatility for next 10 days
forecast = res.forecast(horizon=10)
vol_forecast = forecast.variance.iloc[-1] ** 0.5  # std dev per day
```

**Library**: `arch` (pip install arch)

### GARCH Variants

- **EGARCH**: Captures the asymmetry where bad news increases vol more than good news
- **GJR-GARCH**: Similar asymmetric effect, different formulation
- **FIGARCH**: Long-memory volatility — for stocks where vol regimes persist for months

### Visualization

- **Volatility cone**: Forward-looking vol at different horizons (1-week, 1-month, 3-month), compared to current implied vol (if available) or realized vol
- **Vol surface**: If you ever model options, the implied vol surface across strikes and expirations

### For Your System

Replace the static ATR calculation in floor context with GARCH-forecasted volatility. This gives *forward-looking* floor estimates instead of backward-looking ones.

---

## 8. Support/Resistance Detection (Algorithmic)

### Beyond Discretionary S/R

Your current alert levels are set by Claude analyzing chart patterns. Algorithmic S/R detection can validate or complement these with pure data.

### Methods

**1. Kernel Density Estimation (KDE) on Price**

Find where price has spent the most time — these are natural support/resistance zones.

```python
from scipy.stats import gaussian_kde
import numpy as np

# Estimate density of closing prices over past year
kde = gaussian_kde(closes, bw_method=0.05)
price_grid = np.linspace(min(closes), max(closes), 500)
density = kde(price_grid)

# Peaks in density = support/resistance zones
from scipy.signal import find_peaks
peaks, _ = find_peaks(density, distance=20, prominence=0.001)
sr_levels = price_grid[peaks]
```

**2. Volume Profile (Volume at Price)**

The volume-weighted version of KDE. Prices where the most volume traded are the strongest S/R levels.

```python
# Histogram of volume at each price level
bins = np.linspace(min(lows), max(highs), 100)
vol_at_price = np.zeros(len(bins) - 1)
for i in range(len(closes)):
    idx = np.digitize(closes[i], bins) - 1
    idx = min(idx, len(vol_at_price) - 1)
    vol_at_price[idx] += volumes[i]

# High-volume nodes = strong S/R
peaks, _ = find_peaks(vol_at_price, distance=5, prominence=np.percentile(vol_at_price, 75))
sr_levels = (bins[peaks] + bins[peaks + 1]) / 2
```

**3. Fractal Pivots**

A point is a fractal high if it's higher than the N bars on either side. Similarly for fractal lows. Classic Williams fractals with N=2 or N=3.

```python
def fractal_pivots(highs, lows, n=3):
    """Find fractal pivot highs and lows."""
    pivot_highs, pivot_lows = [], []
    for i in range(n, len(highs) - n):
        if all(highs[i] > highs[i-j] for j in range(1, n+1)) and \
           all(highs[i] > highs[i+j] for j in range(1, n+1)):
            pivot_highs.append((i, highs[i]))
        if all(lows[i] < lows[i-j] for j in range(1, n+1)) and \
           all(lows[i] < lows[i+j] for j in range(1, n+1)):
            pivot_lows.append((i, lows[i]))
    return pivot_highs, pivot_lows
```

**4. Clustering**

Group historical pivot points using DBSCAN or agglomerative clustering. Clusters of pivots at similar prices = strong S/R zones.

### Visualization

- **Volume profile sidebar**: Horizontal histogram on the y-axis showing volume at each price level (like TradingView's Volume Profile)
- **Algorithmic S/R lines**: Drawn alongside Claude's alert levels, but styled differently (e.g., dotted lighter lines) so you can see where human and algorithmic analysis agree

---

## 9. Backtesting Framework

### Why

Before trusting any model, you need to test it against history. "If I had followed this strategy for the past 5 years, what would have happened?"

### Architecture for TradeCentral

```
backtester/
  engine.py       — Core event loop: iterate over bars, apply strategy
  strategy.py     — Strategy interface: receives bar, returns signal
  portfolio.py    — Track positions, cash, P&L
  metrics.py      — Compute returns, Sharpe, drawdown, etc.
  report.py       — Generate backtest report (HTML or Plotly)
```

### Key Metrics

| Metric | What It Tells You |
|--------|-------------------|
| **Total return** | Did the strategy make money? |
| **CAGR** | Annualized return |
| **Sharpe ratio** | Return per unit of risk (>1 is good, >2 is excellent) |
| **Max drawdown** | Worst peak-to-trough loss (your pain threshold) |
| **Calmar ratio** | CAGR / Max drawdown (risk-adjusted return) |
| **Win rate** | % of trades that were profitable |
| **Profit factor** | Gross profit / Gross loss (>1.5 is solid) |
| **Recovery time** | Days to recover from max drawdown |

### Backtesting Sins to Avoid

1. **Look-ahead bias**: Using future data in past decisions. Always use `data[:current_index]`.
2. **Survivorship bias**: Only backtesting on stocks that still exist today. Dead stocks were tradeable in the past.
3. **Overfitting**: Tuning 20 parameters to fit history perfectly = useless on new data. Use out-of-sample testing (train on 2015-2022, test on 2023-2026).
4. **Transaction costs**: NSE brokerage + STT + stamp duty + slippage adds up. Include realistic costs.
5. **Execution gap**: yfinance gives close prices. You can't actually execute at the close. Use next-open or add slippage.

### For Your System

The backtest data you already generate (`analysis/TICKER_backtest.json`) is a good start. A full backtesting framework would let you test strategies like:
- "Buy when HMM transitions to bull regime + price is within 3% of alert level"
- "Trim when Monte Carlo P75 shows +15% in 30 days"
- "Accumulate when OU z-score < -2 and momentum is turning positive"

---

## 10. Risk Modeling

### Value at Risk (VaR)

"What's the worst loss I should expect in a day/week, 95% of the time?"

```python
def var_historical(returns, confidence=0.95, horizon_days=1):
    """Historical VaR — simple percentile of past returns."""
    scaled = returns * np.sqrt(horizon_days)  # scale to horizon
    return np.percentile(scaled, (1 - confidence) * 100)

# Example: 95% daily VaR
daily_var = var_historical(returns, 0.95, 1)
# Result: -0.032 means "95% of the time, daily loss won't exceed 3.2%"
```

### Conditional VaR (CVaR / Expected Shortfall)

"When I DO lose more than VaR, how bad is it on average?"

```python
def cvar(returns, confidence=0.95):
    """Average loss in the worst (1-confidence)% of days."""
    var = np.percentile(returns, (1 - confidence) * 100)
    return returns[returns <= var].mean()
```

CVaR is better than VaR because it tells you about the tail, not just the threshold.

### Maximum Drawdown Analysis

```python
def max_drawdown(prices):
    """Max peak-to-trough decline and its duration."""
    peak = prices[0]
    max_dd = 0
    dd_start = 0
    for i, p in enumerate(prices):
        if p > peak:
            peak = p
            dd_start = i
        dd = (peak - p) / peak
        if dd > max_dd:
            max_dd = dd
            dd_end = i
    return max_dd, dd_start, dd_end
```

### For Your System

- **Portfolio-level VaR**: Aggregate risk across all your positions, accounting for correlations
- **Drawdown monitor**: Real-time chart overlay showing current drawdown from recent peak
- **Risk-adjusted alert sizing**: Scale conviction levels by portfolio risk — higher total portfolio risk = more conservative alerts

---

## 11. Portfolio Optimization

### Markowitz Mean-Variance

Find the portfolio weights that maximize return for a given risk level (or minimize risk for a given return).

```python
from scipy.optimize import minimize
import numpy as np

def optimize_portfolio(expected_returns, cov_matrix, target_return=None):
    """Find minimum-variance portfolio, optionally with target return."""
    n = len(expected_returns)

    def portfolio_vol(weights):
        return np.sqrt(weights @ cov_matrix @ weights)

    constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]
    if target_return:
        constraints.append(
            {'type': 'eq', 'fun': lambda w: w @ expected_returns - target_return}
        )

    bounds = [(0, 0.3) for _ in range(n)]  # max 30% per stock
    result = minimize(portfolio_vol, np.ones(n)/n, bounds=bounds, constraints=constraints)
    return result.x
```

### Risk Parity

Instead of optimizing returns, equalize the *risk contribution* of each position. If CGPOWER is 3x more volatile than BBOX, hold 3x less CGPOWER. Simple and robust.

```python
def risk_parity_weights(volatilities):
    """Inverse-volatility weighting — simple risk parity."""
    inv_vol = 1 / np.array(volatilities)
    return inv_vol / inv_vol.sum()
```

### Kelly Criterion

Optimal bet sizing based on your edge and the odds:

```
f* = (p * b - q) / b
```

Where `p` = win probability, `q` = loss probability (1-p), `b` = win/loss ratio.

In practice, use **half-Kelly** (f*/2) because the real world has more uncertainty than the model assumes.

### For Your System

The **core % allocation** you already define for each stock is a form of strategic allocation. Portfolio optimization can refine the swing layer sizing: "Given correlations and current volatility, how much of the swing layer should be deployed right now?"

Visualization: **Efficient frontier chart** showing risk-return tradeoff with your current allocation marked.

---

## 12. Sentiment & Alternative Data

### News Sentiment

Score news headlines for bullish/bearish sentiment using NLP.

```python
from transformers import pipeline

sentiment = pipeline("sentiment-analysis", model="ProsusAI/finbert")

result = sentiment("CGPOWER wins ₹500cr transformer order from Power Grid")
# [{'label': 'positive', 'score': 0.95}]
```

**Library**: `transformers` with FinBERT model (specifically trained on financial text)

### Sources for Indian Markets

- **BSE/NSE filings**: Corporate announcements, board meeting outcomes
- **MoneyControl/Economic Times**: News headlines (scrape or API)
- **Twitter/X**: Real-time sentiment on specific stocks (noisy but fast)
- **Promoter holding changes**: SEBI filings — insider buying/selling is one of the strongest signals

### Promoter Holding Analysis

Unique to Indian markets: promoter holding changes are publicly filed and highly predictive.

- **Promoter buying**: Very bullish signal (they know more than anyone)
- **Promoter pledging increasing**: Bearish — promoter is leveraged
- **FII/DII flow**: Institutional money flow direction

Your system already tracks some of this in the stock analysis. Automating the scraping and scoring would make it real-time.

### For Your System

- **Sentiment badge** on each stock in the sidebar (positive/negative/neutral based on recent news)
- **Promoter holding trend** indicator — arrow up/down based on latest quarterly change
- **News feed** in the alert panel — filtered to the selected stock

---

## 13. Machine Learning for Signal Generation

### The Approach

Use ML not to predict exact prices, but to classify: **"Given current features, will the stock be higher or lower in N days?"**

### Feature Engineering (Most Important Step)

Good features for NSE stocks:

| Feature | Category | Description |
|---------|----------|-------------|
| Return_5d, Return_20d, Return_60d | Momentum | Recent returns at multiple horizons |
| Vol_5d, Vol_20d | Volatility | Realized volatility |
| RSI_14 | Technical | Relative Strength Index |
| MACD_signal | Technical | MACD histogram sign |
| Volume_ratio_20d | Volume | Current volume vs 20-day average |
| Distance_to_MA20 | Mean reversion | (Price - MA20) / MA20 |
| Distance_to_MA200 | Trend | (Price - MA200) / MA200 |
| ATR_pct | Volatility | ATR as percentage of price |
| Day_of_week | Calendar | Mon-Fri encoding |
| MMI_value | Sentiment | Market Mood Index |
| Sector_return_20d | Cross-sectional | Sector performance |

### Models

**Gradient Boosted Trees (XGBoost/LightGBM)** — the workhorse of tabular ML. Fast, handles missing data, built-in feature importance.

```python
import lightgbm as lgb
from sklearn.model_selection import TimeSeriesSplit

# Target: 1 if price is higher in 20 days, 0 otherwise
y = (prices.shift(-20) > prices).astype(int)

# Time-series cross-validation (never leak future data)
tscv = TimeSeriesSplit(n_splits=5)
for train_idx, test_idx in tscv.split(X):
    model = lgb.LGBMClassifier(n_estimators=200, max_depth=5)
    model.fit(X.iloc[train_idx], y.iloc[train_idx])
    score = model.score(X.iloc[test_idx], y.iloc[test_idx])
```

**Library**: `lightgbm` or `xgboost`

### Ensemble Approach

Combine multiple models for robustness:
1. HMM → current regime
2. Mean reversion (OU) → deviation from fair value
3. Momentum → trend direction
4. ML classifier → aggregate signal

Weight them and compute a composite score. This is more robust than any single model.

### For Your System

A **composite signal score** for each stock: -1 (strong sell) to +1 (strong buy), computed daily. Display as a gradient-colored bar in the stock sidebar. Integrates naturally with your existing alert levels — Claude sets the price levels, the model scores the timing.

---

## 14. Implementation Roadmap for TradeCentral

### Phase 1: Foundation (Immediate Value)

These produce visible results quickly and plug into the existing UI:

1. **Algorithmic S/R detection** (KDE + volume profile)
   - Validates Claude's alert levels with data
   - Adds volume profile sidebar to the chart
   - Difficulty: Low. Pure numpy/scipy. 1-2 days.

2. **Monte Carlo fan chart**
   - Forward-looking cone extending from last candle
   - Plotly filled scatter traces
   - Difficulty: Low. The math is simple. 1 day.

3. **Enhanced backtesting metrics**
   - Extend existing backtest with Sharpe, Calmar, recovery time
   - Display in a backtest summary panel
   - Difficulty: Low. Metrics are formulas. 1 day.

### Phase 2: Regime Intelligence

4. **HMM regime detection**
   - Train per-stock 3-state model
   - Overlay regime coloring on candlestick chart
   - Add regime transition alerts to Telegram
   - Difficulty: Medium. Needs `hmmlearn`, careful feature selection. 2-3 days.

5. **GARCH volatility forecasting**
   - Replace static ATR in floor context with GARCH forecast
   - Forward-looking volatility cone
   - Difficulty: Medium. `arch` library. 1-2 days.

### Phase 3: Smart Signals

6. **Mean reversion scoring (OU process)**
   - Per-stock half-life estimation
   - Z-score indicator on chart
   - Difficulty: Medium. Fitting is straightforward. 1 day.

7. **Composite signal model**
   - Combine regime, momentum, mean reversion, vol
   - Signal score bar in sidebar
   - Difficulty: Medium-High. Needs careful calibration. 3-4 days.

### Phase 4: Portfolio Level

8. **Portfolio risk dashboard**
   - Correlation matrix heatmap
   - Portfolio VaR and drawdown
   - Risk parity weight suggestions
   - Difficulty: Medium. Multiple visualizations. 2-3 days.

9. **Efficient frontier**
   - Plot current allocation vs optimal frontier
   - Interactive: drag weights, see risk/return change
   - Difficulty: Medium-High. Optimization + interactive chart. 2-3 days.

### Phase 5: Advanced

10. **ML signal classifier**
    - LightGBM trained on engineered features
    - Requires feature pipeline and time-series CV
    - Difficulty: High. Most engineering effort. 4-5 days.

11. **Sentiment integration**
    - FinBERT on news headlines
    - Promoter holding tracking
    - Difficulty: High. Needs data sources and NLP pipeline. 3-4 days.

---

## Python Library Reference

| Library | Purpose | Install |
|---------|---------|---------|
| `numpy`, `scipy` | Core math, statistics, optimization | `pip install numpy scipy` |
| `statsmodels` | ARIMA, statistical tests, regression | `pip install statsmodels` |
| `hmmlearn` | Hidden Markov Models | `pip install hmmlearn` |
| `arch` | GARCH and volatility models | `pip install arch` |
| `lightgbm` | Gradient boosted ML | `pip install lightgbm` |
| `scikit-learn` | ML utilities, cross-validation, clustering | `pip install scikit-learn` |
| `prophet` | Time series decomposition | `pip install prophet` |
| `transformers` | FinBERT sentiment analysis | `pip install transformers` |
| `plotly` | Already in stack — all visualization | Already installed |
| `darts` | Unified forecasting (wraps many models) | `pip install darts` |

---

## Key Principles

1. **Models are tools, not oracles.** They quantify uncertainty — they don't eliminate it.
2. **Simpler models with good inputs beat complex models with bad inputs.** Feature engineering > architecture.
3. **Every model has a regime where it fails.** Mean reversion fails in trends. Momentum fails in reversals. That's why regime detection matters — it tells you *which* model to trust right now.
4. **Out-of-sample testing is non-negotiable.** If you didn't test it on data the model hasn't seen, you don't know if it works.
5. **Position sizing matters more than entry timing.** A mediocre entry with proper sizing beats a perfect entry with reckless sizing.
6. **Indian market specifics**: Lower liquidity in mid-caps means larger bid-ask spreads, more mean reversion, and more impact from institutional flows (FII/DII). Models should be calibrated per-stock, not globally.
