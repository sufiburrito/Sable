# [TICKER] — [Company Full Name] | NSE: [TICKER]

## Identity
- **Sector:** [e.g. Optical fiber / Data centers]
- **Reference Price:** ₹[price] (as of [date])
- **52-week range:** ₹[low] — ₹[high]
- **Market cap:** ₹[X] Cr
- **Core Position:** [X]% of invested value — NEVER sell this
<!-- Optional — add ONLY while a genuinely bimodal catalyst (trial readout, regulatory verdict) is unresolved.
     While PENDING, trade_levels.py renders scenario framing instead of numeric R:R. Mark COMPLETED (or
     delete the line) once the outcome is confirmed; the stock then flows back to numeric targets.
- Binary-phase: PENDING | catalyst: [e.g. Phase 2b topline] | expected: [YYYY-MM or YYYY-Qn] | positive: ₹[A-B] | negative-stop: ₹[C] -->

## One-Line Thesis
[Single sentence: what this company does and why you own it long-term]

## Key Thesis Pillars
- [Pillar 1 — e.g. order backlog, promoter buying, pipeline catalyst]
- [Pillar 2]
- [Pillar 3]

## Thesis Breakers (when to reassess)
- [e.g. Daily close below ₹X for 2+ sessions on volume]
- [e.g. Trial failure]
- [e.g. Promoter selling]

## Special Alerts
- [Any time-based or event-based alerts, e.g. earnings date, data readout, lock-in expiry]

---

## Alert Levels

<!-- After filling Price/Signal/Type/reason, run `python3 -m alert_bot.trade_levels [TICKER] --apply`
     to append the derived `TRADE:` clause (entry→target→stop+R:R, or scenario while Binary-phase PENDING)
     to each BUY/SELL message. Re-running is safe/idempotent. Keep the Type cell exactly "BUY"/"SELL"/"WATCH"
     — extra marks like "BUY ★" are silently dropped by the parser. -->

| Signal | Price | Type | Alert Message |
|--------|-------|------|---------------|
| 🔴 | ₹ | BUY | "[TICKER] at ₹X — MAXIMUM BUY. [reason]" |
| 🟠 | ₹ | BUY | "[TICKER] at ₹X — Load up. [reason]" |
| 🔵 | ₹ | BUY | "[TICKER] at ₹X — Add. [reason]" |
| 🟢 | ₹ | BUY | "[TICKER] at ₹X — Add. [reason]" |
| 🟡 | ₹ | BUY | "[TICKER] at ₹X — First tranche. [reason]" |
| 👁️ | ₹ | WATCH | "[TICKER] at ₹X — Watch zone. [reason]" |
| ⬆️ | ₹ | SELL | "[TICKER] at ₹X — Trim [X]%. [reason]" |
| ⬆️⬆️ | ₹ | SELL | "[TICKER] at ₹X — Trim [X]%. [reason]" |
| 🚀 | ₹ | SELL | "[TICKER] at ₹X — Trim [X]%. [reason]" |
| 🚀🚀 | ₹ | SELL | "[TICKER] at ₹X — Trim [X]%. [reason]" |
| 💎 | Always | HOLD | Core [X]% — never trigger a sell alert on this portion |

---

## Belief Level (updated [date])
**[LEVEL]** — [1-paragraph justification: honest verdict on whether this stock will make money,
based on fundamentals + thesis + price action + catalysts. Not a summary of others' views.]

---
## Floor Signals
_Not yet run — trigger with `/analyze [TICKER] retrospective` to populate._

---
## Macro & News Context (updated [date])

### Active Macro Forces
_What sector-level or market-level forces are shaping this stock's environment right now?_

| Force | Direction | Strength | Duration |
|---|---|---|---|
| [e.g. Government capex supercycle] | Tailwind | Strong | Long (years) |
| [e.g. Rising input costs] | Headwind | Moderate | Medium (quarters) |

### Catalyst Stack
_Ranked by immediacy. Only Immediate catalysts affect alert placement this cycle._

| Horizon | Catalyst | Impact | Priced in? | Effect on levels |
|---|---|---|---|---|
| Immediate (0–2w) | [e.g. Q4 results due] | High | No | Widen bands; hold support |
| Near-term (1–3mo) | [e.g. Order pipeline announcement] | Medium | Partial | Raises floor conviction |
| Long-term (3mo+) | [e.g. Sector tailwind multi-year] | High | Yes | Belief level only |

### Zone Confidence Notes
_How does the current news context change confidence in specific alert zones?_
- [e.g. ₹263-270 zone: fundamentally anchored — promoter bought here + Kavach confirms floor]
- [e.g. ₹333-342 resistance: MA wall + no near-term catalyst to break it → trim, don't hold through]

---
## Chart Knowledge (updated [date])
- Key support: ₹[X] — [why]
- Key resistance: ₹[X] — [why]
- Pattern: [e.g. earnings spike rhythm, descending staircase, basing]
- Last chart review: [date]
