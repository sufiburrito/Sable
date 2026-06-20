> **Calibration preamble:** Source reliability ratings and decay curves calibrated to 2020-2025 Indian market structure. Impact scoring weights mirror SEBI's current cap-tier distinctions (Nifty 50 / Nifty 100 / Nifty 500 / below-500). Decay curve half-lives are empirical medians — individual events can deviate significantly (earnings re-rating, fraud). Last reviewed: 2026-05-29.

---

# News Methodology

When scoring news impact or weighting headline freshness — open this file first. Do not score news impact from training-time knowledge.

---

## 1. The 4-Tier Source Hierarchy

| Tier | Sources | Role | Reliability |
|------|---------|------|-------------|
| **1 — Official** | BSE filings, NSE circulars, SEBI orders, RBI press releases, Ministry of Finance/Defence press releases | Source of truth for regulatory changes, company filings, policy announcements | ★★★★★ |
| **2 — Financial media** | MoneyControl, Economic Times Markets, LiveMint, Business Standard | Speed + corporate context; good for earnings commentary, analyst views | ★★★★ |
| **3 — Data-driven** | Trendlyne, Screener.in, Tijori Finance | Bulk/block deal data, earnings calendars, shareholding changes | ★★★★ |
| **4 — Social** | X/Twitter (@StockMKTNews, institutional handles), Reddit r/IndianStreetBets, Telegram channels | Sentiment pulse, early rumour detection, contrarian indicator | ★★ |

**Source arbitrage:** When Tier 4 (social) breaks a story before Tier 1/2 confirms it, assign the social story a base impact score of ≤4 regardless of the claim's scale. Most breaking-news claims on social turn out to be: (a) delayed filings already public, (b) rumours, or (c) already priced. Wait for Tier 1/2 confirmation before raising impact score above 4.

**Reliability in practice:** A SEBI enforcement action published on SEBI's site is ★★★★★. The same action reported on MoneyControl 4 hours later is ★★★★ (accurate but secondary). A Telegram channel "breaking news" about the same action is ★★ until confirmed.

---

## 2. The 13-Category Event Classification

Every headline gets classified into one of these 13 types. The type determines the `event_type_bonus` in the scoring formula.

| # | Category | Example headlines | Event type bonus |
|---|----------|-------------------|-----------------|
| 1 | **Earnings** | Q4 results, revenue guidance, analyst day | +1 |
| 2 | **Corporate action** | Dividend, stock split, rights issue, buyback | +1 |
| 3 | **M&A** | Acquisition, merger, stake sale, demerger | +2 |
| 4 | **Management change** | CEO resignation, board reconstitution, founder exit | +1 |
| 5 | **Regulatory** | SEBI action, RBI circular, DRDO approval, USFDA order | +2 |
| 6 | **Institutional** | FII/DII bulk deal, mutual fund stake change, block deal | +1 |
| 7 | **Sector** | PLI allocation, government tender, sector policy change | +1 |
| 8 | **Macro** | RBI rate decision, Budget announcements, GDP data | +2 |
| 9 | **Global** | US Fed decision, China GDP, oil cartel decision, EM flows | +2 |
| 10 | **IPO** | IPO listing, allotment, grey market premium | +2 |
| 11 | **Legal** | NCLT filing, debt default, FIR, court order against company | +2 |
| 12 | **Rating** | Credit rating upgrade/downgrade, analyst target change | +1 |
| 13 | **Insider/Promoter** | Promoter buying/selling, pledge creation/release | +1 to +2 |

---

## 3. Impact Scoring Formula (1-10)

```
impact_score = base (3)
             + event_type_bonus       (see table above: +1 or +2)
             + sentiment_bonus        (+1 if clearly bullish or bearish; 0 if neutral)
             + size_bonus             (+1 if Nifty 50 company; +0.5 if Nifty 100)
             + breadth_bonus          (+1 if event affects multiple sectors)
             ± unexpectedness         (+1 if surprising vs consensus; -1 if fully priced in / expected)
             - reliability_penalty    (-1 if Tier 4 source only; 0 otherwise)

final_score = clamp(1, 10)
```

**Score interpretation:**

| Score | Level | Action |
|-------|-------|--------|
| 9-10 | Critical — market-wide | Immediate portfolio review; send Telegram alert if portfolio-relevant |
| 7-8 | High — sector-wide or large-cap | Assess portfolio exposure; update KB ticker/sector if relevant |
| 5-6 | Medium — specific stocks | Watch for price reaction; note in morning digest if applicable |
| 3-4 | Low — FYI | Log; no action unless confirms existing thesis |
| 1-2 | Noise | Discard |

**Scoring examples:**

- *RBI surprise rate cut (unexpected, affects all rate-sensitives):* base 3 + macro +2 + bullish +1 + breadth +1 + surprising +1 = **8 (High)**
- *CGPOWER Q4 earnings beat + guidance raise (unexpected):* base 3 + earnings +1 + bullish +1 + Nifty 100 +0.5 + surprising +1 = **6.5 → 7 (High)**
- *Analyst upgrades STLTECH target to ₹180 (consensus-aligned):* base 3 + rating +1 + bullish +1 + Nifty 500 size 0 + priced-in -1 = **4 (Low)**
- *Promoter of SUVEN buys 50,000 shares open market:* base 3 + insider +2 + bullish +1 + small-cap 0 + somewhat surprising +0 = **6 (Medium)**
- *Telegram channel "rumour: BBOX acquisition":* base 3 + M&A +2 + bullish +1 + Nifty 100 +0.5 + reliability penalty -1 = **5.5 → 6 (but treat as 4 until Tier 1/2 confirms)**

---

## 4. Sentiment Reaction Patterns and Half-Life Tables

Quantified expectations for how markets absorb specific event types. Use these before calling a "delayed reaction" or "still playing out" — most events follow a predictable decay curve.

### RBI Policy Moves

| Event | Immediate reaction | Duration | What holds vs. reverts |
|-------|-------------------|----------|------------------------|
| Rate cut (expected) | Banks +0.5-1%; Realty +1-2% | 1-2 days | Reverts as already priced |
| Rate cut (surprise ≥25bp) | Banks +2-4%; Realty +3-5% | 3-5 days | Partially sustained — genuine re-rating of capex cycle |
| Rate hold when cut expected | Banks -1-2%; INR weak | 1-2 days | Reverts within a week |
| Dovish commentary (without cut) | Rate-sensitives rally | 1-2 weeks | Sustains if followed by a cut within 2-3 meetings |
| Rate hike (expected) | Banks -1-2%; Realty -2-3% | 1-2 days | Reverts as already priced |
| Rate hike (surprise) | Banks -3-5%; Realty -4-6% | 1-2 weeks | Partially sustained — re-prices capex cycle timing |

---

### FII/DII Flows

| Event | Immediate reaction | Duration |
|-------|-------------------|----------|
| FII buying >₹2,000 cr/day | Nifty +0.5-1% | 1 day, minor |
| FII buying >₹5,000 cr/day | Nifty +1-2%; INR strengthens | 2-3 days |
| FII selling >₹3,000 cr/day | Nifty -0.5-1.5% | Can persist days-weeks if structural |
| FII selling >₹7,000 cr/day | Nifty -1.5-3% | Acute; DII response determines sustainability |
| FII sell + DII buy (absorption) | Choppy; range-bound | 1-3 weeks |
| Dual buying >₹4,000 cr combined | Strong +1-2%/day | 7-15 days — most powerful short-duration regime |

---

### Earnings Reports

| Event | Immediate reaction | Duration |
|-------|-------------------|----------|
| Beat + raise guidance | Gap up 3-10%; 3-5× volume | **Sustained — re-rating, not mean-reversion** |
| Beat + inline guidance | Gap up 1-3% | Partially reverts day 2-3 |
| Miss + guidance cut | Gap down 5-15%; 5-10× volume | **Sustained — re-rating lower** |
| Miss + maintain guidance | -2-5% | Gradual recovery over 2-3 weeks |
| In-line + in-line | ±1% | Reverts within 2 days |

**Re-rating note:** Earnings beats/misses with guidance changes are the exception to the standard decay curve — they reset the stock's valuation baseline. A -10% gap on miss + cut can sustain for months as analysts downgrade targets.

---

### Crude Oil Shocks

| Crude level | India market reaction | Duration |
|------------|----------------------|----------|
| Rising above $90 sustained | Nifty -1-2%; OMC stocks -5-10% | Sustained while crude stays >$90 |
| Rising above $100 | Broad negative; INR weakens; airlines/paints/plastics -5-15% | Sustained |
| Falling below $70 | Positive for India; OMC stocks +5-10%; INR stable | Sustained while crude stays <$70 |
| Spike then reversal (<2 weeks) | Initial -3-5%, then full reversal | 2-4 weeks total |
| $70-85 "goldilocks" zone | Minimal impact | Structural neutral |

---

## 5. News Impact Decay Curve

The standard decay pattern for most news events:

| Timeframe | % of total move realised | Notes |
|-----------|--------------------------|-------|
| Day 1 | 60-80% | Most of the price response happens in the first session |
| Day 2-3 | 15-25% additional | Continuation or partial reversion |
| Week 1-2 | Narrative builds or fades | Analysts publish models; FII/DII repositioning |
| Month 1 | Structural impact OR mean-reversion complete | Most events fully absorbed |
| Month 1+ | Sustained only for genuine re-ratings | See exceptions below |

**Exceptions — events that do NOT decay back to baseline:**

1. **Earnings re-rating (beat+raise / miss+cut):** These reset the fundamental baseline. Analyst price targets get revised; the stock trades at the new level until the next earnings cycle overrides it.

2. **Fraud/governance failure:** Permanently derating event. Satyam never recovered. DHFL, IL&FS creditors took years to exit. Once institutional trust is broken by fraud, the re-entry barrier is structural.

3. **M&A — deal completion:** Arbitrage spread collapses on announcement but the structural change (new revenue pool, margin profile) is sustained for quarters. The Day 1 gap up is just the start.

4. **Major regulatory approval (USFDA, defence contract win):** Adds a new revenue line. The initial reaction prices the present value, but quarterly execution against the new contract creates sustained interest.

---

## 6. India-Specific Sentiment Indicators

These are shorthand reads that experienced India-market participants use. Sable treats them as confirming signals, not primary ones.

### India VIX context

See `docs/fno_signals.md` for the full VIX band table and delivery entry timing implications. Quick reference:

| VIX | Sentiment label | News impact amplification |
|-----|-----------------|---------------------------|
| <15 | Complacent | Good news barely moves market; bad news ignored |
| 15-20 | Normal | Standard reaction patterns above apply |
| 20-25 | Elevated fear | Bad news amplified 1.5-2× normal; good news dampened |
| >25 | Crisis/capitulation | All bad news amplified; even good news may fail to hold |

### Market Mood Index (MMI) context

MMI is the TradeCentral-specific composite sentiment indicator (Extreme Fear <30, Fear 30-50, Greed 50-70, Extreme Greed >70). Cross-reference with news impact:

- MMI >70 (Extreme Greed): bad news impact amplified; reduce news-driven BUY triggers
- MMI <30 (Extreme Fear): good news impact amplified; news-driven BUY setups more likely to sustain

### Nifty PE context

- Nifty trailing PE >22-23×: news-driven rallies are shorter-lived (already expensive → less upside from re-rating)
- Nifty trailing PE <16-17×: news-driven declines may be overdone; bottom-fishing on 9-10 impact news carries better risk/reward
