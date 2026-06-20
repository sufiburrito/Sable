> **Calibration preamble:** Component weights and zone thresholds calibrated to 2020–2025 Indian market structure. India VIX integration assumes ~17-20% FII market-cap share. The A/D stock count is for NSE-listed equities (~2,000 active). Re-baseline if market structure shifts significantly (e.g., large-scale PSU IPOs inflate total count). Last reviewed: 2026-05-29.

---

# Market Breadth Methodology

When working on market breadth — open this file first. Do not score breadth from training-time knowledge.

The key insight: Nifty 50 is market-cap weighted. Top 10 stocks ≈ 45-50% of index weight. **Nifty can rise 1% even if 40 of 50 stocks are flat or down.** Breadth answers a different question: is the market going up, or just a few heavyweight stocks?

---

## 1. The 5-Component Composite Health Score

Maximum score: 100. Computed daily (or at analyst discretion — no bot automation for this).

| # | Component | Weight | Data source |
|---|-----------|--------|-------------|
| 1 | Advance/Decline ratio | 25 pts | NSE A/D data (available in Trendlyne, Screener) |
| 2 | % stocks above 200-DMA | 25 pts | Screener.in sector screens / computed |
| 3 | New 52-week highs vs lows | 20 pts | NSE/Groww / computed |
| 4 | Sector participation | 15 pts | Sector index LTP vs 50-DMA |
| 5 | Nifty divergence (qualitative) | 15 pts | Pattern: Nifty direction vs breadth direction |

---

## 2. Component Formulas

### Component 1 — Advance/Decline Ratio (0-25)

```
A/D ratio = advancing_stocks / declining_stocks
(count across all NSE active equities, ~2,000 stocks)
```

| A/D Ratio | Score |
|-----------|-------|
| ≥3.0 | 25 |
| 2.5-3.0 | 22-25 |
| 2.0-2.5 | 19-22 |
| 1.5-2.0 | 15-19 |
| 1.2-1.5 | 12-15 |
| 1.0-1.2 | 8-12 |
| 0.7-1.0 | 4-8 |
| 0.5-0.7 | 2-4 |
| <0.5 | 0-2 |

**Trend bonus:** ±2 points if the 5-day SMA of A/D is rising (+2) or falling (-2).

**Advanced trackers (compute when data is available):**
- **Cumulative A/D line:** running sum of (advances - declines) each day. If Nifty is at a new high but cumulative A/D line is not, that is a **primary bearish divergence signal**.
- **McClellan Oscillator:** 19-day EMA minus 39-day EMA of (advances - declines). Oscillator >0 = breadth expanding; <0 = breadth deteriorating.

---

### Component 2 — % Stocks Above 200-DMA (0-25)

```
% above = (count of NSE stocks with LTP > 200-DMA) / (total active NSE stocks) × 100
```

| % Above 200-DMA | Score |
|-----------------|-------|
| ≥75% | 25 |
| 65-75% | 21-25 |
| 55-65% | 17-21 |
| 45-55% | 13-17 |
| 35-45% | 9-13 |
| 25-35% | 5-9 |
| <25% | 0-5 |

**Action thresholds (exposure implications):**
- >60%: full equity exposure justified
- 40-60%: selective; focus on relative strength leaders within existing positions
- <40%: reduce, hold Core only, increase cash buffer
- <25%: maximum defense **OR** contrarian accumulation zone (pair with VIX >25 + DII absorption for confirmation)

---

### Component 3 — New 52-Week Highs vs Lows (0-20)

```
ratio = new_highs / (new_highs + new_lows)
```

| Ratio (highs / total) | Score |
|-----------------------|-------|
| ≥0.83 (5:1 or better) | 20 |
| 0.75-0.83 | 16-20 |
| 0.67-0.75 | 12-16 |
| 0.50-0.67 | 8-12 |
| 0.33-0.50 | 4-8 |
| <0.33 | 0-4 |

**NSE reference counts (calibration):**
- Healthy bull market: 80-150 new highs, 10-30 new lows per day
- Transition/choppy: 30-80 highs, 30-60 lows
- Bear market: 5-20 highs, 60-200+ lows
- Capitulation bottom: 0-5 highs, 200-400+ lows — this extreme reading is itself a contrarian buy signal

---

### Component 4 — Sector Participation (0-15)

Track ~13 NSE sector indices. Count each as "up" if **2 of 3 conditions** are met:
1. Sector index LTP > 50-DMA
2. 1-month return positive
3. 3-month return positive

| Sectors "up" (out of 13) | Score |
|--------------------------|-------|
| 11-13 | 15 |
| 9-10 | 12-14 |
| 7-8 | 9-11 |
| 5-6 | 6-8 |
| 3-4 | 3-5 |
| 0-2 | 0-2 |

**The 13 NSE sector indices to track:** Nifty Bank, IT, FMCG, Auto, Pharma, Realty, Metal, Energy, Infrastructure, PSU Bank, Media, Financial Services, Defense (or Nifty India Defence if not available use portfolio proxy).

---

### Component 5 — Nifty Divergence (0-15, qualitative)

Assess the relationship between Nifty's price direction and overall breadth direction over the past 2-3 weeks.

| Nifty direction | Breadth direction | Score | Label |
|-----------------|-------------------|-------|-------|
| Rising | Improving | 15 | Confirmed uptrend — safest buy environment |
| Flat | Improving | 13 | Stealth accumulation — often precedes breakout |
| Rising | Flat | 10 | Narrow rally — monitor closely |
| Flat | Flat | 8 | Neutral — no edge either way |
| Falling | Improving | 7 | Possible bottom forming — watch for confirmation |
| Flat | Declining | 5 | Stealth distribution — often precedes breakdown |
| Rising | Declining | 3 | **BEARISH DIVERGENCE — highest priority alert** |
| Falling | Declining | 2 | Confirmed downtrend — avoid new longs |

---

## 3. Composite Zone → Equity Exposure Mapping

| Total Score | Zone | Equity Exposure | Strategy |
|-------------|------|-----------------|----------|
| 80-100 | **Strong** | 90-100% | Momentum plays; breakout entries; add to winners |
| 60-79 | **Healthy** | 75-90% | Mix momentum + value; standard alert-based buys |
| 40-59 | **Neutral** | 60-75% | Defensive quality names; sector rotation; no new high-risk |
| 20-39 | **Weakening** | 40-60% | Capital preservation; trim swing layers at resistance |
| 0-19 | **Critical** | 25-40% | Contrarian accumulation OR full defensive; VIX + DII context required |

**Transition rules:**
- **Zone improving:** Stay in the higher zone for 3+ consecutive days before acting on the upgrade. Increase exposure gradually (10% per week max).
- **Zone deteriorating:** Drop immediately when the score crosses into lower zone. Sell weakest-conviction swing layers first.
- **Emergency rule:** Score drops 15+ points in one week OR A/D <0.5 for 3+ consecutive days OR Nifty -5% in a single session → halve swing-layer exposure immediately.

---

## 4. Divergence Detection — The Critical Signal

Breadth divergence is the framework's most actionable output. Bearish divergence is particularly important because it flags fragility **before** the price break.

### Bearish divergence signals (surface when any appear):

1. **Price-breadth:** Nifty within 2% of its 20-day high, BUT % of stocks above 200-DMA has dropped >5 percentage points over the same 20 days.
2. **A/D line:** Nifty making higher highs, BUT the cumulative A/D line is making lower highs.
3. **New highs count:** Nifty at ATH or recent high, BUT daily new-highs count is lower than at the previous Nifty peak.
4. **Sector dropout:** Nifty is up, BUT 3+ sectors that were above their 50-DMA have dropped below in the last 10 trading days.
5. **Cap-tier split:** Nifty 50 making new highs, BUT Nifty Smallcap 100 is below its 50-DMA (or Nifty Midcap 150 is lagging significantly).

### Bullish divergence signals (contrarian accumulation context):

1. Nifty making lower lows, BUT cumulative A/D line is making higher lows.
2. New lows count declining even as Nifty falls.
3. % above 200-DMA stabilising or rising despite falling index.
4. Sector dropout reversing (sectors reclaiming 50-DMA).

### Severity and action scale:

| Bearish divergence signals active | Duration | Action |
|----------------------------------|----------|--------|
| 1 signal | <1 week | Monitor — no action yet |
| 2 signals | 1-2 weeks | Tighten stops on swing layer; stop adding new positions |
| 3+ signals | 2-3 weeks | Reduce swing exposure by 20-30%; no new buys |
| All 5 signals | 3+ weeks | Move to defensive exposure (40-60%); prepare to trim further |

---

## 5. Three Historical Case Studies

### COVID 2020

- **Feb 20, 2020 score:** ~65 (Healthy zone — zero breadth warning)
- **Mar 23, 2020 score:** ~5 (Critical zone — full capitulation)
- **Dec 2020 score:** ~75 (Healthy zone restored as FII returned)
- **Lesson:** The framework gave no early warning in February because breadth was genuinely healthy until the WHO pandemic declaration. March breadth collapse (score <10) was itself the contrarian signal — extreme Critical readings mark capitulation, not continued decline. The investor who waited for "breadth improvement" bought at 8,600.

---

### 2021 Bull Top and Bearish Divergence

- **Sep 2021 score:** ~85 (Strong zone, all-time high)
- **Oct 2021:** Nifty made a new high. But score dropped to ~70. Only 7/13 sectors were participating. New highs count declined vs the September peak. **Classic bearish divergence — signal 1, 3, and 4 active simultaneously.**
- **Nov 2021-Jun 2022 outcome:** Nifty fell 15% over 8 months
- **Lesson:** The framework correctly flagged the top 3-4 weeks in advance. A trader watching only Nifty saw "new all-time high." A breadth watcher saw "narrow rally, thinning participation."

---

### 2022 Narrow Rally

- Nifty rose +24% from June to December 2022 — headline-grabbing rally
- But breadth score stayed in the 40-55 range (Neutral, never reaching Healthy)
- Why: rally was concentrated in Adani group stocks, defense PSUs, and a few PSU banks — not broad participation
- **Lesson:** A rising index with neutral breadth is a warning signal, not a confirmation. The portfolio manager who allocated aggressively into the October-December 2022 rally on "Nifty is up 20%" without checking breadth walked into the February-March 2023 correction.

---

## 6. India-Specific Nuances

These distortions affect breadth readings on specific days/periods. Adjust interpretation accordingly.

**F&O expiry (every Thursday):**
- Expiry-day rollover creates artificial A/D distortions (basket selling/buying of index components)
- Use 5-day average A/D rather than single-day reading on expiry Thursday
- Do NOT change regime classification based on a single expiry-day breadth reading

**Budget Day:**
- Budget is a discrete event, not a trend — breadth spikes and crashes on budget day are event-driven noise
- Use the pre-Budget reading (T-1) and the T+3 post-settlement reading as the clean signal
- Do not update composite score on Budget Day itself

**RBI policy announcement days:**
- Bank Nifty (a giant weight in NSE stocks' absolute returns) gets disproportionately moved
- Component 4 (sector participation) can be distorted by banking dominating that day's sector count
- Evaluate Bank Nifty/financial sector separately; wait 2-3 trading days before composite update

**DII SIP floor — the "DII put":**
- India's breadth deteriorates more slowly than US markets during selloffs because the SIP floor (~₹1,500-2,000 cr/day) keeps bid pressure on large-caps
- This means breadth scores of 30-40 in India are more constructive than equivalent readings in US markets
- Don't mechanically copy US breadth playbooks

**Monsoon season (June-September):**
- FMCG, fertilizer, tractor, rural finance breadth expands/contracts with IMD weekly forecast
- A Deficient Monsoon Alert from IMD instantly drags AgriTech, FMCG, fertilizer sector breadth — use as a one-day event-driven discount, not a regime call
- Full monsoon impact takes 2-3 months to flow through to breadth data

**IPO market as leading indicator:**
- Vibrant IPO market (20+ mainboard IPOs/quarter, 2-5× oversubscription) = healthy breadth environment
- SME IPO 100×+ oversubscription for multiple consecutive months = late-stage excess warning
- IPO market freezing up (withdrawals, pricing below issue price) = bear market signal (even if Nifty is resilient)

**SEBI regulatory actions:**
- Category-level changes (F&O inclusion/exclusion, circuit limit tightening) can create one-day A/D spikes that don't reflect market health
- Flag and exclude these from trend analysis
