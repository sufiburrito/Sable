# docs/ — Long-form Reference Files

Methodology references and operational guides. Open the relevant file before working on the topic it covers — do not synthesise from training-time knowledge.

| File | Purpose |
|------|---------|
| `discord_io.md` | Discord I/O — channel map, command dispatch, the one-token in-process/webhook transport split, central HTML→Markdown translation, and the emoji reaction-feedback system |
| `backtest_guide.md` | How Sable's backtest system works — the `backtest_levels.py` pipeline, output schema, and how floor context uses backtest data in alerts |
| `backtest_failure_modes.md` | When NOT to trust a backtest — 8 failure patterns (cost killer, parameter peak, regime specialist, survivorship illusion, circuit limit trap, gap risk destroyer, F&O ban blow-up, liquidity mirage) with India examples and fixes |
| `forward_test.md` | Out-of-sample forward-test rig — TRADE-call track record, realized-R ledger, Bayesian per-class posterior edge + learned backtest-discount δ. Runs nightly via host cron (`forward_test.py`); research read, NOT an alert gate |
| `journal.md` | Local Obsidian trade journal — FIFO realized P&L, missed-call scorecard, execution review, FY-split effective P&L (set-off + carry-forward), and Indian-CG tax planning. Rebuilt nightly via host cron. Planning aid, not tax advice |
| `fii_dii_methodology.md` | FII/DII institutional flow interpretation — 6-regime classifier (Net Buyer/Seller/DII Absorption/Dual Buying/Dual Selling/Transition), DII SIP-floor absorption math (~₹25,000 cr/m), significance thresholds, and four historical case studies |
| `market_breadth_methodology.md` | 5-component market health score (A/D 25%, % above 200-DMA 25%, new highs/lows 20%, sector participation 15%, divergence 15%), zone-to-exposure mapping, and bearish divergence detection rules |
| `fno_signals.md` | F&O-derived signals for delivery decisions only — India VIX bands and entry timing, Nifty PCR crowd positioning, F&O ban as structural risk flag, OI-based index S/R. **No F&O trading content.** |
| `news_methodology.md` | News impact scoring rubric (1-10), 4-tier source hierarchy, 13-event classification, sentiment reaction patterns with half-lives, and decay curves for RBI / FII / earnings / crude events |
| `readme.ZERODHA_API.md` | Zerodha Kite Connect real-time price feed — WebSocket ticks, TOTP auto-login (`kiteconnect` + `pyotp`), yfinance fallback, and poll-interval logic |
