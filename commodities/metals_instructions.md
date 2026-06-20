# Commodity Framework — Reusable Patterns

A working notebook of insights from building the gold tracker. Use this as the design template when adding silver, copper, oil, or any other commodity to the system.

The patterns below are not theoretical — each one came from a specific decision point during the gold build, often after rejecting a wrong first instinct. The "why" matters as much as the "what".

---

## 1. Frame the asset honestly before writing any code

Different commodities demand different mental models. **Picking the wrong frame poisons every downstream design choice.**

| Asset | Honest frame |
|-------|-------------|
| Gold | Macro-driven portfolio insurance — SIP accumulation, never sell core, throttle pace by regime |
| Silver | Industrial + monetary hybrid — more cyclical than gold, beta to risk-on regimes |
| Copper | Growth-cycle bellwether — buy during industrial slowdowns, sell into capex booms |
| Oil | Geopolitical + supply-shock asset — not a long-term hold, position-trade only |
| Agricultural (wheat, sugar) | Weather + policy-driven — short cycles, retail investors usually shouldn't touch |
| Uranium | Multi-year supply-cycle thesis — accumulation during bear markets, hold through bull |

**The framing question to ask first:** "Is this a long-term hold, a position trade, or a short-term cyclical?" The answer dictates whether you're computing **accumulation regimes** (gold), **rotation triggers** (copper), or **range-bound mean-reversion** (oil).

**For gold we explicitly chose:** "When to accelerate accumulation" — not "when to buy or sell". This single reframe killed half the bad ideas in the initial design (5-state regimes, daily noise, sell signals, RSI overlays).

---

## 2. Two parallel views from one data source

For most commodities the user thinks about the asset two ways: a **tradable** form and a **reference** form. Build both, derive both from the same source, never desync.

| Asset | Tradable view | Reference view | Conversion |
|-------|--------------|----------------|------------|
| Gold | GOLDBEES.NS (ETF) | 24K ₹/gram | NAV × multiplier (factsheet constant, refresh annually) |
| Silver | SILVERBEES.NS (ETF) | ₹/kg 999 fine | NAV × multiplier |
| Copper | COPPER futures contract or copper miner basket | LME spot $/tonne | direct USD → INR via INR=X |
| Oil | CRUDEOIL contract | Brent $/bbl | direct |

**Why this matters:** The user's mental model when buying jewelry or a coin is ₹/gram. The price they actually transact at when adding to a SIP is GOLDBEES NAV. Showing only one of these two leaves a permanent translation tax. Showing both (derived from the same source) costs nothing and respects how the user actually thinks.

**Avoid duty-reconstruction.** Tempting to compute "physical price = international × FX × (1+duty)" but customs policy changes (gold went from 15%→6% in July 2024) silently break the formula. Instead, **derive the reference view from the tradable view via a single multiplier** that already bakes in the duty. Refresh the multiplier annually.

---

## 3. Three-state regime classifier — not five, not seven

**False precision is worse than honest coarseness.** Five states implies you can distinguish "STRONG ACCUMULATE" from "ACCUMULATE" with quantitative confidence. You can't. The data is too noisy and there's no ground truth.

Use **three states** for almost every commodity:

| State | Meaning | User action |
|-------|---------|------------|
| 🟢 GO / ACCUMULATE | 4-5 tailwinds aligned | Lean in this week |
| 🟡 NEUTRAL / Continue SIP | 2-3 tailwinds | Default — quiet, no alert |
| 🔴 WAIT / CAUTION | 0-1 tailwinds + price extended | Hold off lump sums (SIP unchanged) |

**Telegram fires only on regime transitions, not on daily state.** Daily messages train the user to ignore the bot. The whole point of the regime is to cut noise, not generate it.

---

## 4. The 5-factor scorecard discipline

Build a scorecard of **exactly 5 factors**, equal-weighted (count tailwinds, no tuned coefficients). Five is the magic number: enough for diversification, few enough to remember without a cheat sheet, no false-precision in weighting.

**Three hard tests every factor must pass before it goes in:**

1. **Market-quantifiable.** Must be derivable from yfinance/cached CSVs without scraping. If it requires monthly or quarterly data, it doesn't go in the scorecard — it goes in the narrative file.
2. **Practitioner + academic validation.** Name two real-world investors or research desks that watch this signal. If you can't, you're guessing.
3. **Non-overlapping with another scorecard factor.** If factor A's variance is already inside factor B, you're double-counting — drop one or replace it.

**The most important discipline:** be willing to **kick out** factors mid-design. Gold initially had nominal 10Y rates (kicked: conflates inflation expectations and real growth) and USDINR (kicked: already inside GOLDBEES NAV). Replaced with TIP ETF (real-yield proxy) and INDIAVIX (volatility regime). The replacements are *better* because each one earned its place by passing the three tests.

**Display rejected-but-relevant variables as context, not as scorecard entries.** USDINR didn't make the gold scorecard but is shown in the Telegram message so the user understands *why* GOLDBEES moved when international gold was flat. Context block ≠ scorecard.

---

## 5. The double-counting trap

This is the most common scorecard mistake. Symptoms:

- "USDINR" and "GOLDBEES NAV" both as factors → GOLDBEES NAV already includes the FX move, you've weighted FX twice
- "VIX" and "S&P 500 drawdown" both as factors → drawdown causes VIX, you've weighted equity stress twice
- "DXY" and "USDINR" both as factors for an Indian asset → DXY moves drive INR moves, partial double-count

**The test:** for any candidate factor, ask "is the variance this factor measures already inside one of my other factors?" If yes, drop it or replace it.

For gold I caught this on USDINR — it took two rounds of plan iteration. Be ready to throw out factors that look "obviously important" if they're already baked in elsewhere.

---

## 6. Percentile-of-self for anything that drifts

Some signals have a structural baseline that shifts over time:
- India gold premium drifts when customs duty changes
- USD/EM currency premiums drift with capital control regimes
- Industrial metal "fair value" drifts with green-transition demand

**The trick:** instead of an absolute threshold ("premium > 5% = expensive"), use a **rolling 1-year percentile of itself** ("premium > 75th percentile of trailing year = expensive").

This makes the formula immune to one-off regime breaks. The customs duty change of July 2024 would have permanently broken any absolute-premium rule. The percentile-of-self approach kept working through it.

**When to apply:** any factor where the "normal" range is itself a moving target.

---

## 7. Lump-sum gate ≠ SIP gate

**For a long-term accumulator, "wait for the dip" rules are dangerous.** Gold rallied $900→$1900 in 2009-2011 with almost no pullbacks. Any rule that paused buying because the asset was "extended" would have missed the entire move.

**Solution:** distinguish two kinds of accumulation:

- **SIP (continuous, dollar-cost)** — runs regardless of regime. The whole point is to dollar-cost through extended periods.
- **Lump sums (discretionary, opportunistic)** — gated by the regime. Skip during WAIT, accelerate during ACCUMULATE.

The "price extension" factor in the scorecard is a **lump-sum gate only**. SIP continues regardless. Make this explicit in the Telegram message: "Continue SIP. Hold off on additional lump sums." Otherwise the user reads "WAIT" and pauses everything, defeating the entire dollar-cost philosophy.

---

## 8. Python-first division of labor

This is **load-bearing**. Every commodity tracker should follow it.

**Python does ALL deterministic math.** It writes a single perfectly-prepared structured bundle (`data/{commodity}_analysis_bundle.json`) containing every number, percentile, slope, distance, label, and historical context that narrative reasoning might need. **Zero math, zero lookups, zero "go fetch X" left for the LLM.**

**The LLM (Sable / Claude) reads the bundle and produces only narrative judgment** — cross-referencing the quantitative regime against qualitative context, surfacing portfolio-level implications, composing prose for human-friendly reports.

**Why this division matters:**
- LLMs are bad at arithmetic and great at synthesis. Use each for its strength.
- A perfect bundle means narrative reasoning is reproducible across LLM versions.
- It separates "things we know how to compute" from "things that require judgment" — you can audit each independently.
- When the Python output is a fully-formed bundle with explicit field names and units, the LLM never has to guess what a number means.

**Required bundle fields (minimum) for any commodity:**

```json
{
  "schema_version": "1.0",
  "as_of": "<ISO timestamp with timezone>",
  "prices": {
    "<each tracked instrument>": {
      "value": <float>,
      "today_pct": <signed float>,
      "week_pct": <signed float>,
      "month_pct": <signed float>,
      "ytd_pct": <signed float>,
      "from_52w_high_pct": <signed float>
    }
  },
  "scorecard": {
    "tailwinds_count": <int>,
    "max_count": 5,
    "factors": { "<factor_name>": { "value": ..., "tailwind": true|false, ... } }
  },
  "regime": {
    "current": "<state>",
    "previous": "<state>",
    "transition_today": <bool>,
    "transition_direction": "upgrade|downgrade|none",
    "history_30d": [...]
  },
  "zones": [ ... distance to each user-defined zone, both % and absolute ... ],
  "calendar": { "next_event": { ... }, "all_events_remaining_2026": [...] },
  "correlations_30d": { ... rolling correlations vs each scorecard factor ... },
  "volatility_and_drawdown": { ... },
  "narrative_context_pointer": "data/<commodity>_narrative.json"
}
```

**Pre-format everything.** Dates as ISO + human strings. Percentages with explicit signs and 2-decimal precision. Labels ("rising"/"falling"/"flat") instead of raw slopes. The LLM should be able to type the bundle straight into a Telegram message with zero transformation.

---

## 9. One-writer-per-file discipline (no race conditions)

When you have a deterministic Python pipeline AND an LLM-driven narrative pipeline writing to the same project, **never let two processes write the same file**. You will eventually corrupt state on a race.

**The rule:**

| File | Owner | Other side's access |
|------|-------|---------------------|
| `data/<commodity>_snapshot.json` | Python polling track | Read-only |
| `data/<commodity>_analysis_bundle.json` | Python polling track | Read-only |
| `data/<commodity>_narrative.json` | LLM autonomous loop | Read-only |

**How to integrate them:** the Python tracker may **read** the narrative file to surface qualitative context in the Telegram message — but never writes to it. The LLM may read the snapshot/bundle to inform the convergence report — but never writes to either.

This rule has saved me twice already on other parts of this codebase. Apply it to every new commodity tracker.

---

## 10. Run outside market-hours gate

Commodities are **global, not NSE-bound**. They trade on weekends-adjacent calendars (gold futures trade nearly 24/5 globally, oil similarly).

**More importantly:** holidays are exactly when context matters most. The user is at home, has time to think, sees a global headline, and wants to know what it means. A market-hours gate guarantees the bot is silent precisely when its insight is most valuable.

**Implementation:** in `main.py`, add the daily commodity check **BEFORE** the `is_market_open()` gate, latched on `last_<commodity>_check_date != now.date()`. The polling cadence is daily, not minute-by-minute, so this costs almost nothing.

---

## 11. Reuse, don't rebuild

These primitives in `alert_bot/` are **asset-generic** and should be reused for every commodity:

| Primitive | File:line | Why it's reusable |
|-----------|-----------|-------------------|
| `load_ohlc_cached(ticker, yf_symbol, period)` | `alert_bot/ohlc_cache.py:23` | Accepts any yfinance symbol unchanged. Caches to `analysis/{ticker}_ohlc_cache.csv`. Fetches only the missing tail on subsequent runs. |
| `engine._crosses(level, prev, curr)` | `alert_bot/engine.py:378` | Generic crossing detection: BUY = drop from above, SELL = rise from below, WATCH = either direction. |
| `BotState.level_key(ticker, price_str)` | `alert_bot/state.py:54` | Generic cooldown keys. Use `"GOLD:₹6200-6300"` format. |
| `BotState.level_cooled_down(key, minutes)` | `alert_bot/state.py:61` | Generic cooldown check. |
| `AlertLevel` dataclass | `alert_bot/parser.py:28` | Generic level container. Reuse for accumulation zones. |
| `TelegramNotifier.send(text)` | `alert_bot/notifier.py:23` | Plain text + HTML. |
| `quant_modeling/hmm_regime.py`, `monte_carlo.py` | — | Generic over price series. Apply to gold/silver/copper for Phase 2. |

**The new code per commodity should be a single thin module** (`alert_bot/{commodity}.py`) plus a config file (`commodities/{commodity}.md`) plus a sibling parser dataclass. Everything else reuses existing infrastructure.

---

## 12. Decoupled cross-references via convergence (no cross-writes)

Commodities often have indirect implications for stocks already in the portfolio:

- Gold thesis breaks → GOLDLENDER thesis breaks (gold-collateral lender)
- Copper rally → HINDCOPPER tailwind
- Oil spike → input cost pressure on SHARDACROP, paint companies, airlines
- Steel rally → JSW Steel, Tata Steel tailwind

**The wrong way:** let the gold tracker reach into GOLDLENDER's alert engine and modify thresholds.

**The right way:** single-direction dependency chain.
```
commodity tracker → writes snapshot.json → convergence report reads it
                                         → surfaces "GOLDLENDER thesis tailwind active"
                                         → user sees it in convergence
                                         → GOLDLENDER alert config remains untouched
```

**Why decoupled is better:** each system keeps a single responsibility. The commodity tracker doesn't know about specific stocks. Stock alerts don't depend on commodity files existing. The convergence layer is the only place that knows the relationship — and it's a one-way read.

**Implementation:** Python computes a `<stock>_linkage` block in the bundle (mechanical: "gold 19.8% above $4,000/oz thesis-breaker → tailwind active"). The convergence report (LLM-generated) decides whether and how to surface it.

---

## 13. Telegram noise discipline

**Fire on transitions, not on states.** Daily "here's the current regime" messages train the user to ignore the bot. The user already knows the current regime — they read it yesterday.

The signals worth interrupting the user for:

| Trigger | Frequency | Example |
|---------|-----------|---------|
| Regime transition | rare | "GOLD: NEUTRAL → ACCUMULATE" |
| Zone crossing | rare | "Gold pulled back to ₹6,200-6,300/g — first accumulation zone" |
| Weekly Sunday digest | weekly | Full state recap regardless of changes |
| Thesis breaker proximity | rare | "Gold 5% from $4,000/oz thesis breaker — GOLDLENDER at risk" |

Everything else is **ambient information** — it lives in the bundle JSON, in the convergence report, in the Telegram-on-demand commands. Not in unsolicited messages.

---

## 14. Narrative factors live in the loop, not the scorecard

Some factors are real drivers of commodity prices but aren't quantifiable from market data:

- Central bank gold buying (quarterly data, hard to scrape)
- Geopolitical risk events ("Iran ceasefire", "Russia sanctions tightened")
- Inflation expectations from policy commentary ("Fed pivots dovish")
- OPEC production decisions
- Strategic petroleum reserve releases
- Weather forecasts for agricultural commodities

**These do NOT go in the Python scorecard** (they fail the "market-quantifiable" test). They go into a rolling `data/{commodity}_narrative.json` file written by the autonomous loop, extracted from morning digests via regex + LLM judgment.

The Python tracker reads this file as **read-only context** to surface qualitative narrative in the Telegram message body. It does not let the narrative override the scorecard regime classification — quantitative and qualitative are separate signal layers that the user (or convergence report) reconciles.

---

## 15. Hardcode deterministic data, derive everything else

Some things never change and should be **hardcoded in the config markdown**:

- Festival dates (Akshaya Tritiya, Dhanteras, Diwali) — published years in advance
- Harvest cycles for agricultural commodities
- OPEC meeting dates (when announced months ahead)
- Known earnings or macro-print dates

**Why hardcoded > extracted:** extracting from digests adds a failure mode (LLM misreads the date) for zero benefit (the data is deterministic).

**What does NOT get hardcoded:** anything that requires periodic refresh based on price action, macro data, or analyst opinion. Those go in the bundle as computed fields.

---

## 16. Document the "why" in the config markdown

The commodity config file (`commodities/{name}.md`) is read by both Python AND the user AND future-Sable. It should be:

- **Self-explanatory** — a new reader should understand the asset framing in 2 minutes
- **Decision-recording** — document *why* each scorecard factor was chosen and what was rejected
- **Refresh-cued** — flag any constants that drift over time (like the GOLDBEES multiplier) with explicit "refresh annually" notes
- **Honest about exclusions** — explicit "What this tracker does NOT do" section so future-you doesn't waste time re-litigating decisions

The markdown is the **source of truth** for the tracker's design philosophy. The Python code implements it; the LLM consumes its outputs; but the markdown is where future-Sable goes to understand *why* things are the way they are.

---

## 17. Verification checklist (every new commodity)

Before declaring a tracker done, run all of these:

1. Drop a sample config in `commodities/{name}.md` with at least 2 zones
2. Run a one-liner that calls `fetch_<commodity>_snapshot()` and dumps to JSON — confirm all yfinance symbols fetch and the snapshot/bundle materialize
3. Confirm all OHLC cache CSVs were created with ≥1y of bars
4. Inspect the bundle JSON — verify scorecard has exactly 5 fields, regime ∈ valid states, history table has ≥30 rows
5. Force a regime transition in code (override scorecard) → restart bot → confirm Telegram delivers the regime-change message
6. Set a synthetic zone within ±2% of current price → confirm zone-crossing alert fires
7. Run the autonomous loop manually on a digest with relevant commodity quotes → confirm `{commodity}_narrative.json` updates
8. Next-day no-transition: scorecard updates, JSON refreshes, **NO** Telegram message (verify silence on quiet days)
9. Verify weekly Sunday summary fires (manual date override)
10. Verify the check runs on a market holiday (override `is_market_open()` to False) — must still fetch and update

If any of these fail, the tracker is not ready. Don't ship it.

---

## 18. Phase 2 ideas (deferred for any commodity)

Things that look tempting but add complexity for marginal value. Defer until Phase 1 is proven and the user requests them:

- HMM regime detection from `quant_modeling/hmm_regime.py` (works on any price series)
- Monte Carlo forward distribution from `monte_carlo.py`
- Prophet forecasting overlay
- Multi-currency views (USD + INR + local) for global commodities
- Cross-commodity correlations (gold/silver ratio, copper/oil ratio)
- Seasonality decomposition (especially for agricultural)
- Options-implied volatility overlay (where listed)

These all have value but **they're not the foundation**. The 5-factor scorecard + 3-state regime + zone crossings is enough to deliver real signal on day one. Layer the rest on once the foundation is proven.

---

## 19. Gold tracker — canonical specification

The gold tracker (`alert_bot/gold.py`) is the reference implementation of every pattern above. Used as the source of truth by CLAUDE.md; the values live here so the design doc stays in one place.

**The 5 factors (equal-weighted, count tailwinds):**

1. **TIP ETF direction** — real-yield proxy. Rising TIP = falling real yields = tailwind for gold.
2. **DXY direction** — falling DXY = tailwind (dollar weakness drives gold up).
3. **Equity volatility regime** — `^INDIAVIX` > 20 AND rising = tailwind (fear bid).
4. **Price extension** — GOLDBEES vs 200DMA in σ. **Lump-sum gate only**; SIP unaffected.
5. **India premium percentile** — rolling 1Y of self; below 50th percentile = tailwind (relative to its own recent history, not an absolute threshold — see section 6).

**3-state regime thresholds:**

| Tailwinds | Regime | User action |
|-----------|--------|-------------|
| 4-5 | 🟢 ACCUMULATE | Lean in this week (lump sums on) |
| 2-3 | 🟡 NEUTRAL | Continue SIP, no extra action |
| 0-1 + extended | 🔴 WAIT | Hold off lump sums; SIP continues |

**Reuse map** — primitives the gold tracker borrows from `alert_bot/`:

| Primitive | File:line |
|-----------|-----------|
| Zone crossing detection | `alert_bot/engine.py` `_crosses()` |
| `AlertLevel` dataclass | `alert_bot/parser.py:28` |
| OHLC cache for 7 yfinance series | `alert_bot/ohlc_cache.py` `load_ohlc_cached()` |
| Zone cooldowns | `alert_bot/state.py` `BotState.level_cooldowns` |

**Tracked instruments:**
- **Tradable:** `GOLDBEES.NS` (NSE gold ETF — what the user transacts on)
- **Reference:** 24K ₹/gram = `GOLDBEES NAV × 81.89` (refresh multiplier annually from fund factsheet)

**GOLDLENDER linkage** — gold tracker writes a `goldlender_linkage` block in `data/gold_analysis_bundle.json`; convergence report reads it. Single-direction dependency; gold tracker never touches GOLDLENDER alert config.
