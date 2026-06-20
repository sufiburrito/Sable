# Forward-test rig — does the TRADE call actually pay?

Out-of-sample track record for our swing calls, with the backtest and forward test informing each
other via Bayesian shrinkage + a learned backtest-optimism discount. Built because the Prophet study
(`quant_modeling/PROPHET_RESEARCH.md`) showed price prediction is futile — the open question is
whether the *calls* have an edge. Research read, **not** an alert gate.

## Pipeline (all idempotent)
```
backfill_ledger.py  → data/forward_ledger.jsonl   (reconstruct fired BUY/SELL calls as entry/target/stop)
forward_resolve.py  → updates the ledger          (walk each open call forward vs OHLC → realized R)
forward_edge.py     → results/forward_edge/edge.json + table   (Bayesian posteriors + δ)
```
`forward_test.py` runs all three in sequence (one command). It runs **nightly via a host cron**
(`install_forward_cron.sh` installs `run_forward_test.sh` at 11:30 PM Mon–Fri) — pure Python, no
LLM, so it deliberately does NOT live in the autonomous `/loop`. Shared helpers live in
`forward_lib.py`. `backfill` is the catch-up step too — it reads
`data/sent_alerts.json` (the bot updates it live), so re-running picks up newly-fired alerts; **no
hot-path change needed**. A future enhancement could capture the exact `TradeLevel` at fire-time
(gold standard) vs the current reconstruction (labelled `source:"reconstructed"`).

## What counts (the win definition)
- **Unit = swing call with levels.** BUY = `entry/target/stop`; SELL/trim = mirror
  (`target` = reload below, `stop` = resistance above). Entry is treated as triggered at fire-time
  (the alert only fires when price reaches the level).
- **Outcome = realized R.** target → `+R:R`, stop → `−1`, same-bar both → pessimistic stop, 63-day
  cap → fractional `(close−entry)/(entry−stop)`. **Win = R>0** (derived).
- **Excluded:** the never-sell core (that's buy-and-hold, the benchmark), binary/scenario alerts,
  and calls with no defensible levels.
- Levels reuse production math (`trade_levels.buy_stop`/`buy_target`) with a vol **p75 cone cap** so
  a giant backtest MFE can't project an absurd target; support/resistance from `sr_levels`.

## The math (forward_edge.py)
Per **class = `alert_type × conviction × regime`** (`liq_tier` is a stratifier, not a class):
- **Win-rate** — `Beta(κ·p' + wins, κ·(1−p') + losses)`, `κ=8`. `p'` = backtest win-rate deflated by
  the regime δ (skeptical `0.5` when no backtest anchor). Reports mean, 90% CI, `P(win>50%)`.
- **Expectancy R** — Normal with a **skeptical zero-edge prior** (`κ_e=5` pseudo-obs at R=0). Reports
  mean, 90% CI, `P(edge>0)`.
- **Discount δ_regime** = forward / backtest win-rate, shrunk toward 1 (`κ_δ=10`). `δ<1` ⇒ backtests
  optimistic in that regime; it deflates the priors → the loop closes.
A class only "has an edge" when `P(edge>0)` is high **and** its 90% interval excludes zero.

## Caveats (honest)
- **Slow:** per-class significance is 6–18 months; pooled δ firms up in ~a quarter. Warm-start (the
  530 historical alerts) jump-starts it.
- **Warm-start anachronism:** reconstructed levels use today's `trade_levels`/backtest sidecar, not
  the as-of-date ones; regime is a cheap 30-week-MA **proxy**, not the HMM. Live `captured` rows are
  the gold standard. Labelled in the ledger.
- **Metric mismatch in the prior:** backtest `win_rate_6m` ("% positive at 6M") ≠ our forward
  "target before stop in 63d" — so the prior anchors loosely (small κ); forward data + δ carry it.
- **Multiple comparisons:** classes are pre-registered (type×tier×regime), not mined; the CI gate +
  hierarchical δ shrinkage guard against cherry-picking.
