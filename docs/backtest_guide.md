# How the Backtest System Works

_Written: 2026-03-17_

---

## What the backtest measures

For each alert level in your stock file, it replays history and asks: *every time price crossed this level in the past N years, what happened next?*

It records:
- **N entries** — how many times this crossing occurred
- **Win rate at 6M** — % of times price was higher 6 months later
- **Median drawdown** — how far below the entry price it typically fell before recovering
- **Median days to green** — how long before it closed back above your entry

---

## The three ways the bot uses this data

### 1. Live alert enrichment

When a BUY alert fires, the bot immediately reads the backtest sidecar and appends a floor estimate derived from two converging signals — ATR buffer and the historical median drawdown. You get:

> 🤖 🔵  BUY  BBOX approaching ₹461-471
> *Watch for better entry near ₹445*

This is not decoration. That ₹445 tells you: *historically, when BBOX has crossed ₹461 to the downside, it has typically dipped to around ₹445 before recovering.* You can use that in two ways — buy a partial position at the alert level and set a limit for ₹445, or wait for ₹445 if you have no position yet.

For SELL alerts:
> 🤖 ⬆️  SELL  BBOX entering trim zone ₹522-534
> *Rally may extend to ₹564 before trimming*

This tells you the resistance may not hold immediately. You can place a limit sell at ₹564 rather than selling at ₹522 and potentially leaving money on the table.

### 2. `/backtest TICKER` — pre-decision reference

Before you act on an alert, or any time you want to review a stock's levels, `/backtest BBOX` gives you the complete picture immediately:

```
📊 BBOX — Floor Context

BUY zones:
🟢 ₹486-497  → watch near ₹469  (10 entries)
🔵 ₹461-471  → watch near ₹445  (4 entries)
🟠 ₹435-446  → watch near ₹416 ⚠ low confidence  (2 entries)

SELL zones:
⬆️ ₹522-534  → may extend to ₹564  (16 entries)
🚀 ₹552-583  → may extend to ₹615  (9 entries)
```

The N entries number is critical. ₹486-497 with 10 entries is a proven zone — the market has repeatedly found support there. ₹435-446 with 2 entries is structurally plausible but not proven. You size your conviction accordingly.

### 3. Input to Claude's analysis (your research tool)

When you run `backtest_levels.py TICKER --period 5y` before an `/analyze`, the resulting markdown report in `analysis/TICKER_backtest.md` tells Claude which levels are historically validated. Claude can then prioritise those levels when suggesting revisions to your alert table, or flag levels that have no history and should be treated as hypothetical until tested.

---

## How it maps to your investment philosophy

Your system has two layers — core (never touched) and swing (trim at resistance, reload at support). The backtest speaks directly to the swing layer decisions:

**Reloading at support:** A BUY level with high win rate and shallow drawdown is a zone where institutional money has historically stepped in. That's exactly where you want to add to your swing position. A BUY level with 0 entries in 5 years is a level drawn from chart analysis — plausible, but untested. The bot will silently omit the floor hint for that level, which is itself a signal.

**Trimming at resistance:** A SELL level where every historical crossing was followed by a decline is validated resistance. Trim there. A SELL level where price kept going up 50% of the time is weak resistance — maybe only trim a small slice, or wait for the rally extension price the bot shows you.

**Days to green:** This is the metric people ignore and shouldn't. If a level historically takes 40+ days to recover, that's capital sitting at a loss for over a month. For your swing layer, that's opportunity cost. A level that historically goes green in 5 days is a clean bounce zone — much better risk profile than one that grinds sideways for 6 weeks.

---

## What it doesn't do

It doesn't know if the current environment is fundamentally different from the historical period. If a stock has changed business model, or the sector has re-rated, the historical floor may not hold. That's why the backtest is one input into your decision — not the decision itself. The `/analyze` flow exists to give you the qualitative overlay that the backtest can't.

---

## The workflow in practice

```
New levels set via /analyze
        ↓
python3 backtest_levels.py TICKER --period 5y
        ↓
Review analysis/TICKER_backtest.md — are these levels proven?
        ↓
Bot runs. Alert fires with floor hint.
        ↓
/backtest TICKER for full context before acting.
        ↓
React to alert (👍 ⏳ ✅ etc.) — feeds back into retrospective analysis.
```

Every piece of that loop is now wired together.
