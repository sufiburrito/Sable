# GOLD — Indian Investor Accumulation Tracker

**Asset class:** Commodity (precious metal) — long-term portfolio diversifier
**Tracked instruments:** GOLDBEES (NSE ETF) + Physical 24K gold (₹/gram view, derived from GOLDBEES NAV)
**Cadence:** Daily (runs OUTSIDE NSE market hours — gold is global, holidays included)
**Target Allocation:** 8% of total portfolio

---

## Investment Philosophy

Gold is **not a stock**. It's a macro-driven, regime-following asset. The right framing for a long-term investor is **"when to accelerate accumulation"**, not binary buy/sell.

- **Default behavior:** Continue SIP regardless of regime (dollar-cost discipline beats market-timing).
- **Regime overlay:** Throttle the pace — accelerate when tailwinds align, hold off lump sums when extended.
- **Never sell core gold position.** Gold is held for portfolio insurance, not return-chasing.

The two views (ETF + physical) are the same underlying asset:
- **Tradable (GOLDBEES.NS)** — what you actually transact on for ETF SIP additions
- **Physical 24K (₹/gram)** — your mental model when buying jewelry, coins, or thinking about household gold

Both move together. ACCUMULATE regime is a green light for ETF tranches AND physical purchases.

---

## Physical 24K ₹/gram — How It's Computed

```
INDIAN_GOLD_CUSTOMS_DUTY_PCT: 6
```

The two views are computed independently from market data, NOT derived from each other:

- **Tradable view (GOLDBEES NAV)** — quoted directly from `GOLDBEES.NS` on the NSE.
- **Physical 24K ₹/gram (Indian retail wholesale)** — computed each run as:
  ```
  (international_USD_per_oz × USDINR / 31.1035) × (1 + customs_duty)
  ```
  This is the duty-paid Indian retail spot. Jeweler making charges (5-25%) and 3% GST on jewelry are vendor-side variables and are NOT included.

**Why no "GOLDBEES × multiplier" derivation?** GOLDBEES NAV moves with international gold × FX (the fund holds physical gold), but it has its own NSE premium/discount and a slowly-drifting fund-accounting offset. Trying to derive ₹/gram from NAV introduces noise that doesn't belong in a retail-spot quote.

**When customs duty changes** (it was 15% pre-July-2024, now 6% — it will move again), update the constant above and the derivation auto-corrects.

---

## Accumulation Zones (₹/gram, 24K)

These are user-defined "lump sum opportunity" zones. Crossing into a zone fires a Telegram alert. SIP runs continuously regardless.

| Signal | Price (₹/gram, 24K) | Type | Alert Message |
|--------|---------------------|------|---------------|
| 🟢 | ₹6200-6300 | BUY | "Gold pulled back to ₹6,200-6,300/g — first accumulation zone. Add ETF tranche + consider physical." |
| 🔵 | ₹5900-6000 | BUY | "Gold deeper pullback to ₹5,900-6,000/g — strong accumulation zone. Lean in." |
| 🔴 | ₹5500-5600 | BUY | "Gold deep pullback to ₹5,500-5,600/g — rare opportunity. Maximum accumulation." |

---

## 5-Factor Regime Scorecard

The Python tracker computes 5 equal-weighted tailwinds daily and collapses to one of 3 regimes:

1. **TIP ETF direction** (real-yield proxy) — TIP rising = real yields falling = tailwind
2. **DXY direction** — falling DXY = tailwind for gold (cheaper for non-USD buyers)
3. **Equity volatility regime** — INDIAVIX > 20 AND rising = risk-off backdrop = tailwind
4. **Price extension** — GOLDBEES within ±2σ of 200DMA = tailwind for lump sums (SIP unaffected)
5. **India premium percentile** — below 50th percentile of trailing 1Y = tailwind (not festival-spiked)

**Regimes:**
- 🟢 **ACCUMULATE** — 4-5 tailwinds aligned → Telegram nudge to add this week
- 🟡 **NEUTRAL — Continue SIP** — 2-3 tailwinds → quiet (default state, no daily noise)
- 🔴 **WAIT** — 0-1 tailwinds AND price >2σ above 200DMA → caution on lump sums

Telegram fires only on **regime transitions** + **zone crossings** + **weekly Sunday digest**.

---

## Thesis Pillars (Why Hold Gold)

- **Currency debasement hedge** — gold has held purchasing power across centuries; rupee has lost 95% of value vs gold over 50 years.
- **Real-yield insurance** — when real interest rates fall (or go negative), gold's "no yield" cost vanishes.
- **Geopolitical tail-risk hedge** — pairs with portfolio in flight-to-safety regimes (Iran/Russia/Taiwan scenarios).
- **Central bank structural bid** — post-2022 sanctions, EM central banks (PBoC, RBI, Turkey, Russia) at record net-buyer pace per World Gold Council.
- **Festival/wedding cultural floor** — India + China account for >50% of physical demand; structural bid never disappears.

## Thesis Breakers

- Gold sustained below $4,000/oz for 6+ months (would also break GOLDLENDER thesis — see decoupled linkage)
- Real yields rising sharply for 12+ months without offsetting safe-haven demand
- Major central banks reversing to net sellers (would signal end of structural bid)

---

## Hardcoded Festival Calendar (2026)

These dates drive physical gold demand spikes. Used by Python to compute `days_until_next_event` and label `premium_risk`.

| Festival | Date | Demand Implication |
|----------|------|--------------------|
| Akshaya Tritiya | 2026-05-08 | wedding_season_pickup |
| Dhanteras | 2026-11-09 | peak_buying |
| Diwali | 2026-11-10 | peak_buying |

## Hardcoded Festival Calendar (2027)

| Festival | Date | Demand Implication |
|----------|------|--------------------|
| Akshaya Tritiya | 2027-04-28 | wedding_season_pickup |
| Dhanteras | 2027-10-29 | peak_buying |
| Diwali | 2027-10-30 | peak_buying |

---

## What This Tracker Does NOT Do

- ❌ Track jeweler making charges (5-25%) or 3% GST on jewelry — vendor-side variables, not visible
- ❌ Track SGB tranche announcements — deferred to Phase 2 (user chose ETF + physical)
- ❌ Fire daily Telegram noise — only on regime transitions, zone crossings, Sunday digests
- ❌ Reach into GOLDLENDER alerts — single-direction dependency (gold → convergence report → stock context)
