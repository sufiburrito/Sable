# `quant_modeling/` — market models behind the alert verdicts

The statistical models the alert engine reasons with. `alert_bot/regime_context.py` bridges these into
live verdicts ("HIGH CONVICTION", "FALLING KNIFE"); the web UI reads them for its regime badge and
Monte Carlo fan chart.

## Code

```
quant_modeling/
  hmm_regime.py   — Hidden Markov Model regime detection (Bull / Bear / Sideways / Volatile) via the Viterbi path
  monte_carlo.py  — regime-switching Monte Carlo price simulation → P5/P25/P50/P75/P95 forward cones
```

## Design docs

| File | Purpose |
|------|---------|
| `MARKET_MODELING.md` | The modeling reference — HMM, Monte Carlo, mean reversion, momentum factors, volatility models, risk parity, and backtesting design |
| `REGIME_SWITCHING_MC.md` | Design of the regime-switching Monte Carlo (per-regime return/vol, transition matrix, cone construction) |
| `GROWW_INTEGRATION.md` | Groww brokerage SDK integration notes |
