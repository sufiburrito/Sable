> **Calibration note:** These failure patterns are structural and relatively timeless — they describe how backtests lie regardless of market era. The specific examples (Satyam 2009, DHFL 2019, Vodafone Idea F&O ban) are Indian market anchors that make the abstract concrete. Review patterns before trusting any `analysis/{TICKER}_backtest.json` result.

---

# Backtest Failure Modes

Companion to `docs/backtest_guide.md` — that file covers the **system**; this file covers **when not to trust it**.

When evaluating whether a backtest can be trusted — open this file first. Applies to every `analysis/{TICKER}_backtest.json` output and every strategy result before it informs an alert level or position size.

**Minimum edge thresholds for a backtest to be actionable:**
- Delivery (CNC): >0.5% per round-trip after costs
- Intraday (MIS): >0.2% per round-trip after costs
- F&O options: >1-2% per round-trip after costs

**Required regime survival rule:** Every backtest must survive **≥3 distinct market regimes** (e.g., 2019 bull, 2020 COVID crash, 2021 recovery, 2022 Fed hike bear) to be trusted. A backtest trained on a single bull run is a survivor story, not a strategy.

---

## The 8 Failure Patterns

### 1. The Cost Killer

**What it is:** A strategy with a 0.3% gross edge that looks profitable in a frictionless backtest but produces losses after India's actual transaction cost stack.

**Indian market example:** A mean-reversion strategy on Nifty Midcap 150 stocks, entering and exiting within 2-3 days. Gross win rate: 58%. After STT (0.1% delivery), brokerage, exchange, stamp duty — round-trip costs ~0.5-0.8% on small-caps. The entire edge disappears.

**Mechanism:** Small gross edges are completely consumed by costs. The backtest looks profitable because it was run without realistic cost modeling or with costs underestimated by 50-70%.

**Fix:** Model every rupee of cost. For delivery: ~0.3-0.5% round-trip large-cap, ~0.5-1.0% small-cap. Use the full cost table from `docs/fii_dii_methodology.md` as reference. A backtest that doesn't show the P&L net of costs is not a backtest — it's a wish.

**Minimum edge to survive costs:** >0.5% net for delivery strategies.

---

### 2. The Parameter Peak

**What it is:** A strategy that only works at a very specific parameter setting — RSI(42), EMA(17), lookback(23) — but fails at RSI(40) or RSI(44). The backtest shows a sharp peak in parameter space, not a plateau.

**Indian market example:** A momentum strategy using a 23-day momentum lookback that shows 24% CAGR on backtest. Setting it to 20 or 25 days drops CAGR to 8%. The 23-day peak is a statistical artifact — it fit the 2019-2023 data, not a real phenomenon.

**Mechanism:** Testing 100 parameter combinations means ~5 will appear significant at the 95% confidence level purely by chance (multiple testing problem). The "winner" is often data-mined noise.

**Fix:** 
1. **Heat map test:** Plot performance across a grid of parameter values (e.g., RSI 30-60 in steps of 5, lookback 10-40 in steps of 5). A genuine edge shows a **plateau** of positive performance, not a sharp spike.
2. **Rule of thumb:** Need 10-20 trades per parameter for statistical validity. A 5-parameter strategy needs 50-100 trades minimum to be meaningful.
3. **Out-of-sample holdout:** Reserve the last 2 years as an untouched test set. Optimize on the first 8 years, validate on the last 2.

---

### 3. The Regime Specialist

**What it is:** A strategy trained during a bull market that fails completely in a sideways or bear market. The backtest covers only one regime.

**Indian market example:** A breakout strategy backtested from April 2020 to November 2021 (strong bull run post-COVID recovery) shows 45% CAGR. Applied from January 2022 onwards (FII selling + sideways), it produces -15% in 12 months. The strategy worked in a trending bull, not in a mean-reverting range.

**Mechanism:** The 2020-2021 period was anomalous — cheap liquidity, retail rush, everything broke out. A breakout strategy will always shine in such an environment. It's not alpha; it's beta to the bull.

**Fix:** Require the strategy to be tested across ≥3 distinct regimes:
- 2018-2019: moderate bull → IL&FS liquidity crisis (range → crash)
- 2020: COVID crash → recovery
- 2021: strong bull
- 2022: FII selling bear
- 2023-2024: recovery + China rotation disruption

A strategy that survives all of these with positive net returns has regime-survival credibility.

---

### 4. The Survivorship Illusion

**What it is:** The backtest universe includes only stocks currently listed. Stocks that went bankrupt, were delisted, or suffered fraud are absent — systematically biasing returns upward.

**Indian market example:** Backtest the Nifty 200 from 2009. If the current Nifty 200 is your universe, you've excluded Satyam (fraud 2009), Unitech (bankruptcy 2010s), Kingfisher (defaulted 2013), DHFL (fraud 2019), Jet Airways (liquidated 2019). These were Nifty 200 members — any long strategy would have held them on the way down.

**Mechanism:** Survivorship bias systematically overstates win rates and understates maximum drawdown. The "worst stocks" are removed from your universe retroactively.

**Fix:** Use point-in-time universe construction — the universe at time T should include only stocks that were listed and liquid at time T, including subsequently delisted names. For most retail backtests this is impractical, so at minimum: explicitly acknowledge the bias and apply a 2-3% annual survivorship premium discount to stated CAGR.

---

### 5. The Circuit Limit Trap

**What it is:** The backtest assumes instant execution at stop-loss levels, but in reality NSE applies circuit limits (5%, 10%, 15%, 20%) that can lock a stock for multiple sessions.

**Indian market example:** A strategy exits a stock at -5% stop. The stock triggers a bad earnings announcement after close; it opens at -15% the next day. The circuit limit kicks in at -10%. The exit is filled at -10%, not -5%. Worse: the stock is locked at the lower circuit for 3 consecutive days — no exit possible.

**Mechanism:** Circuit limits are non-negotiable. A stock hitting a 10% lower circuit cannot be sold until buyers emerge. For small-cap stocks (circuit limits as low as 5%), this is a regular occurrence.

**Fix:** In any strategy involving stocks below Nifty 100 size, model circuit-limit gaps explicitly: assume stop-loss execution at 1.5-2× the stated stop on small-caps. Alternatively, never hold more than 5% of a single small-cap position overnight — gap risk is structural.

---

### 6. The Gap Risk Destroyer

**What it is:** A strategy that holds positions overnight assumes next-day open is close to prior close. Earnings surprises, corporate fraud, and regulatory actions cause overnight gaps that exceed stop levels.

**Indian market example:**
- Earnings miss + guidance cut: -3% to -8% gap down at open
- Corporate fraud revelation (Satyam-style): -10% to -20% gap down, potentially sustained
- SEBI order / NSE trading halt: stock halted until investigation, no exit possible

**Mechanism:** Stop-losses are intraday tools. They don't protect against overnight gap risk. A 5% stop on a position with 3% of portfolio weight theoretically limits loss to 0.15% portfolio. But a 20% gap down turns that into 0.6% loss — 4× the expectation.

**Fix:** Size positions accounting for gap risk (not just intraday stop). For names with upcoming earnings, promoter pledges >20%, or regulatory investigations, reduce overnight exposure by 50% regardless of stop placement.

---

### 7. The F&O Ban Period Blow-Up

**What it is:** A strategy holds F&O positions in stocks without monitoring NSE's Market-Wide Position Limit (MWPL). When OI crosses 95% of MWPL, NSE bans new positions. Existing positions can only be closed, not rolled.

**Indian market example:** Vodafone Idea (Vi) and Punjab National Bank (PNB) have repeatedly entered and exited F&O ban status. A strategy that relies on rolling monthly positions in these stocks is forced to close at whatever price the market offers during the ban — often at the worst moment.

**Mechanism:** F&O ban creates one-directional pressure. Longs must exit; no new shorts are allowed to offset. Price dislocates from intrinsic value because the exit queue forms but no new buyers can express their view via options.

**For delivery investors (note):** The condition that causes an F&O ban — extreme one-sided positioning — usually precedes a sharp reversal in the underlying within 1-2 weeks. If a portfolio stock lands on the F&O ban list, flag it as structural reversal risk regardless of cash-market price action. (See `docs/fno_signals.md` §F&O ban as risk flag.)

**Fix for F&O strategies:** Monitor MWPL daily for any held F&O position. Exit before the 95% threshold, not after. The ban is a lagging signal — position exit before 80% MWPL is conservative but avoids the problem entirely.

---

### 8. The Liquidity Mirage

**What it is:** A backtest shows clean entries and exits at mid-price. In reality, the stock has thin liquidity — wide bid-ask spreads, low daily volume, large slippage on any meaningful size.

**Indian market example:** A strategy backtested on stocks with ₹1 cr daily average volume looks great on paper. Executing a ₹5L position means your single order is 50% of the daily volume — the market moves against you on entry and you can't exit cleanly. Backtested at ₹182.50; actual fill ₹184.00 entry, ₹180.75 exit. A 0.5% stated gain becomes a 0.5% actual loss.

**Mechanism:** Historical price data shows mid-price (or last trade). Actual execution costs include: spread, market impact (moving the price with your own order), and slippage. For illiquid stocks, these can easily be 0.5-2% round-trip on top of the cost table.

**Fix:** Liquidity filter: minimum ₹1 crore daily average turnover (in the screener this is `volume × close_price`). Below this threshold, do not execute any strategy that was backtested on historical price data. Also verify that the daily turnover is stable — some stocks have ₹1 cr average but ₹5L on down days (exactly when you need to exit).

---

## Quick Reference — Red Flags That Require Immediate Backtest Rejection

**Critical (stop here — backtest cannot be trusted):**
- Negative expectancy after costs
- <30 total trades
- >5 free parameters
- Slippage/costs not modeled
- Look-ahead bias (used future data in signal construction)
- No out-of-sample test
- Only tested in one market regime
- Universe excludes delisted stocks without survivorship discount

**Warnings (trust with heavy discount):**
- Win rate >80% (suspiciously high — check for data errors)
- CAGR >50% sustained (too good — probably overfit)
- No losing month for 3+ consecutive years (data error suspected)
- MDD <5% on a strategy that holds stocks overnight (unrealistic)
- 30-100 trades (wide confidence interval — edge may not be real)
- <5 years tested
- MDD >30% (psychologically intolerable, will be abandoned)
- Walk-forward efficiency <50% (in-sample much better than out-of-sample)
