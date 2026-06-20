<!-- DO NOT EXTEND THIS FILE WITH F&O TRADING CONTENT. SIGNALS ONLY. -->

> **Sable does not trade F&O — this file is signal-only by design. No Black-Scholes pricing, no Greeks, no strategy construction, no position sizing. F&O trading is structurally out of scope for the TradeCentral system.** The risk profile (leverage, theta decay, gap risk, STT-on-intrinsic trap, physical settlement on stock options, ban-period blow-up) is incompatible with the user's long-term delivery accumulation philosophy.

> **Calibration preamble:** VIX bands tuned to 2020-2025 Indian market regime; PCR thresholds calibrated for current Indian retail-heavy option-writing structure (retail is heavily net option seller via weekly expiry). India VIX calculation methodology is based on Nifty 50 options OI (not physical volatility). Last reviewed: 2026-05-29.

---

# F&O-Derived Signals for Delivery Decisions

When reading F&O-derived signals — open this file first. Do not synthesise VIX interpretation, PCR positioning, or F&O ban implications from training-time knowledge.

The premise: Sable does not enter F&O markets. But F&O markets generate signals that are useful for delivery timing — fear levels (VIX), crowd positioning (PCR), structural vulnerability (F&O ban), and key index levels (OI walls).

---

## 1. India VIX Bands → Delivery Entry Timing

India VIX measures the market's expected 30-day volatility implied by Nifty 50 options prices. It is a fear gauge — higher VIX = more fear = options more expensive = bigger expected moves.

**For delivery investors, VIX is a timing dial, not a strategy selector.**

| VIX Level | Regime label | What Sable should do (delivery) |
|-----------|-------------|----------------------------------|
| <12 | Extreme complacency | Be skeptical of breakouts. Vol expansion is coming — the market is too calm. Hold off on fresh adds near resistance levels. Breakouts at VIX <12 often fail badly when vol returns. |
| 12-15 | Low volatility, trending | Trending market conditions. Pivot breakouts are likely to follow through. Normal position sizing appropriate. |
| 15-20 | Normal | Default state for Indian markets. No special VIX-based adjustment needed. Follow standard alert and stage analysis. |
| 20-25 | Elevated fear | Hedging is active, meaning many participants are already defensively positioned. Often a near-bottom signal. Watch for DII absorption at support zones — if support holds at VIX 20-25, it usually holds well. |
| 25-35 | High fear / capitulation territory | Best zone for contrarian accumulation **IF** Stage analysis confirms basing (Stage 1 or late Stage 4) and DII absorption is occurring. Do not buy falling knives — verify stage first. |
| >35 | Crisis | Cash heavy. Only highest-conviction Core adds at extreme, well-tested support. Wait for VIX to break back below 30 before adding swing-layer exposure. Every breakout attempt will be tested again. |

**VIX and confidence.py:** BUY alerts firing when VIX >25 carry a regime-appropriate confidence modifier — they are contrarian entries that require additional confirmation (DII absorption, stage, promoter activity). The `regime_context.py` "FALLING KNIFE" verdict is specifically keyed to high-VIX + Stage 4 + negative momentum alignment.

---

## 2. Put-Call Ratio (PCR) — Crowd Positioning

The Nifty Put-Call Ratio measures the open interest in put options vs call options. In an Indian market where retail heavily sells weekly calls (covered call / theta farming), the PCR has a slightly bullish structural bias — it tends to run above 1.0 more often than US markets.

**Use at index level only.** Stock-level PCRs are too noisy (dominated by individual events) and should not drive delivery decisions.

| PCR (Nifty) | Market interpretation | Sable implication |
|-------------|----------------------|-------------------|
| >1.3 | Heavy put buying — widespread hedging or outright fear | **Contrarian bullish.** Too many bears. Often precedes short-covering rallies. Pair with VIX 20-25 and DII absorption for confirmation. |
| 1.1-1.3 | Moderately bearish positioning | Mild caution. Not enough fear to be contrarian; not enough complacency to be a top signal. |
| 0.9-1.1 | Neutral / balanced | No signal. |
| 0.7-0.9 | Slightly bullish positioning | Mildly extended; watch for reversal but not actionable alone. |
| <0.7 | Heavy call buying — speculation or complacency | **Contrarian bearish.** Too many bulls. Often precedes sharp pullbacks. Pair with VIX <15 and breadth divergence for confirmation. |

**PCR as a confirming signal, not a primary signal.** A PCR >1.3 alone does not trigger a BUY. It adds weight to a BUY setup that already has Stage analysis + support + DII absorption aligned. Similarly, PCR <0.7 adds weight to a trim/sell setup at resistance.

---

## 3. F&O Ban List — Structural Risk Flag

**What it is:** When the aggregate Open Interest in a stock's F&O contracts exceeds 95% of the Market-Wide Position Limit (MWPL) set by NSE, that stock enters F&O ban. New F&O positions cannot be opened; existing positions can only be reduced.

**Recent Indian examples:** Vodafone Idea (Vi), Punjab National Bank (PNB), Adani group names in specific stress periods.

**For delivery investors:** F&O ban itself does not restrict cash market buying or selling. However, the *condition* that causes the ban — extreme one-sided F&O positioning — is the signal.

**What an F&O ban usually means:**
- Extreme one-sided positioning has built up in F&O (almost always directional — a large short or long squeeze building)
- The ban forces one-sided unwinding (existing positions must close; no new positions allowed)
- This creates sharp, often temporary, price dislocations in the underlying
- Historical pattern: within 1-2 weeks of entering ban, the stock typically sees a sharp reversion toward fair value as the one-sided F&O position unwinds

**Portfolio action when a held stock enters F&O ban:**
1. **Flag immediately** as elevated reversal risk for the next 1-2 weeks
2. **Do not add** new delivery position until the ban lifts and OI normalises below 80% MWPL
3. **Tighten the thesis validation window** — if the thesis is intact and support holds during the ban period, that is a strong sign. If support breaks during the ban, exit swing layer.
4. **Treat analogously to promoter pledge >30%** — it is a structural vulnerability flag, not a death sentence, but deserves a step-up in monitoring intensity.

---

## 4. OI-Based Index Support/Resistance

At index level (Nifty 50 weekly expiry), large Open Interest concentrations at specific strikes act as gravitational levels.

**The mechanics:**
- **High call OI at a strike = resistance ceiling.** Option writers defend this level by selling more calls as price approaches (keeping premiums flat, actively selling the underlying to hedge). Nifty has difficulty breaking through high call OI strikes on an expiry week.
- **High put OI at a strike = support floor.** Option writers defend this level by buying underlying to hedge their short puts. Nifty is supported near high put OI strikes.
- **Max pain** = the strike at which the maximum number of contracts (both puts and calls) expire worthless. Nifty weekly expiry tends to gravitate toward max pain ±50-100 points on non-event days.

**Sable's practical use of OI levels:**
- When a digest mentions "Nifty defending 24,200 put OI floor" — understand this as: option writers are buyers near 24,200, providing a support cushion until expiry.
- When a digest mentions "24,800 call wall" — understand this as: option writers are sellers above 24,800, capping the rally near expiry.
- These levels reset every Thursday (Nifty weekly expiry). Do not treat OI-based levels as structural support/resistance — they are expiry-specific.
- For delivery-oriented portfolio decisions, horizontal S/R from price history (in `analysis/{TICKER}_ohlc_cache.csv`) is more durable than weekly OI walls.

---

## 5. The STT-on-Intrinsic Gotcha (Why F&O Is Treacherous)

Not directly relevant to delivery decisions, but cited here as the canonical reason for the categorical no-F&O rule.

**The trap:** Sell a Nifty call option for ₹5 premium. At expiry, the option expires In-The-Money (ITM) with ₹200 of intrinsic value. Securities Transaction Tax (STT) is charged at 0.0625% on the **settlement value** — i.e., the ₹200 intrinsic — not the ₹5 premium received. The STT bill: ₹200 × 0.0625% × lot_size. This can exceed the ₹5 premium entirely, turning a "won" position into a loss.

**Why it matters for Sable:** This is the kind of asymmetric, non-obvious regulatory landmine that makes F&O treacherous for retail participants. The delivery cost stack is transparent and predictable. The F&O cost stack has traps that even experienced traders miss. This fact alone justifies the categorical signal-only boundary — Sable extracts information from F&O markets without being exposed to their structural hazards.
