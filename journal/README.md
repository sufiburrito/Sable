# `journal/` — local trade journal (Obsidian)

Builds a personal trade journal as a local **Obsidian** vault (`journal/vault/`, gitignored) from your
broker transactions in `portfolio.db`. It is a planning and review aid — **not tax advice**.

Full reference + coaching playbook: **`docs/journal.md`**.

## What it produces

- **Realized P&L** — FIFO closed-lot matching with per-lot profit/loss.
- **Missed Trades** — an advised-but-not-taken scorecard (calls the system made that you didn't act on),
  corroborated against actual OHLC movement (target hit / stopped / soft / pending).
- **Execution Review** — advised-AND-taken: the call vs your real fill (entry slippage + exit-vs-target),
  with verified post-sell excursion so an "early exit" is only flagged if price actually ran higher.
- **Analytics scorecard** — Week / Month / FY / All toggle, P&L calendar, equity curve, radar.
- **Effective P&L** — FY-split Indian capital-gains view: set-off (STCL→STCG+LTCG, LTCL→LTCG), the
  ₹1.25L LTCG exemption, and 8-year loss carry-forward.
- **Tax Planning** — harvest candidates, LTCG-threshold watch, and reminders.

## Modules

```
journal/
  build.py            — orchestrator: 6 guarded stages, rebuilds the whole vault
  realized_pnl.py     — FIFO closed-lot P&L (portfolio.db.transactions → closed_lots)
  missed_trades.py    — advised-but-not-taken scorecard + OHLC corroboration
  execution_review.py — advised-AND-taken: call vs real fill, verified exit quality
  pnl_statement.py    — FY-split effective P&L (charges + CG tax layered on gross)
  tax.py              — Indian capital-gains computation (set-off, exemption, carry-forward)
  tax_reminders.py    — harvest / LTCG-threshold reminders (Discord + CalDAV)
  obsidian.py         — renders the vault: dashboards, DataviewJS tables, charts
```

## How it runs

Rebuilt nightly by `python3 -m journal.build` via **host cron** (right after the forward-test), **not**
the autonomous `/loop` — it is pure Python, no LLM.

**GROSS is the source of truth.** Realized P&L comes straight from raw `portfolio.db.transactions`
(`total_value == qty × price`); the cost and tax views layer on top and never mutate it.
