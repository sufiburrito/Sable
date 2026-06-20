# `commodities/` — commodity (gold/metals) tracker design

Design notes for the commodity tracker that runs alongside the equity engine — a gold/precious-metals
view with the same conviction-first philosophy. The live tracker code lives in `alert_bot/gold.py`;
these files are the methodology behind it.

| File | Purpose |
|------|---------|
| `gold.md` | Gold tracker design — 5-factor scorecard, 3-state regime (accumulate / hold / trim), lump-sum vs SIP gating, and the ₹/gram view |
| `metals_instructions.md` | Broader metals/commodity-tracker design patterns and the reuse map shared with the gold tracker |
