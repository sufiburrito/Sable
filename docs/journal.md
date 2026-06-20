# Trade journal — what your trading actually did, and what to do next

A local **Obsidian** vault that turns the raw transaction ledger into a self-coaching
trade journal: closed-lot P&L, a missed-call scorecard, performance analytics, take-home
(after charges + tax), and Indian capital-gains planning. Open this first when working on
anything under `journal/`, or when the user asks how a trade/quarter/FY actually went or
what to harvest before FY-end.

## How it runs

- **Vault:** `journal/vault/` — open it as a folder in Obsidian. Needs the **Dataview**
  plugin (with **JS queries** enabled) for the tables/KPIs and the **Charts** plugin for
  the bars/radar/equity curve.
- **GROSS is the source of truth.** Everything in the vault is computed from the raw
  `portfolio.db.transactions` (`total_value == qty×price`) — pre-brokerage, pre-STT/GST/DP,
  pre-tax. The charge/tax views *layer on top* and **never mutate** the gross numbers.
- **Nightly rebuild:** `python3 -m journal.build` runs 6 guarded stages, in order —
  `charge_model` → `realized_pnl` → `missed_trades` → `execution_review` → `obsidian_vault` →
  `tax_reminders`. A later failure can't undo an earlier success; the build exits non-zero if any
  stage fails.
- **Where it runs:** the **host cron**, right after `forward_test.py` (via
  `run_forward_test.sh`), so the journal reads a fresh forward ledger. Pure Python, no LLM —
  it deliberately is **not** in the autonomous `/loop`. For the forward-ledger / edge math
  it consumes, see `docs/forward_test.md` (not duplicated here).
- **Verified-move primitive:** `forward_lib.excursion(df, from_date, ref_price)` returns the
  realized peak/trough move (%) over the next ~63 trading bars from the OHLC cache, with a
  `complete` flag — so a too-recent event stays **pending** until a full window prints. Both the
  Execution Review and Missed Trades views use it to ground verdicts in *real* price, not forecasts.

**Modules (`journal/`):** `build.py` (orchestrator) · `realized_pnl.py` (FIFO →
`closed_lots`) · `missed_trades.py` (advised-but-not-taken scorecard) · `execution_review.py`
(advised-and-taken: advice vs your real fill) · `pnl_statement.py`
(charge model from the broker xlsx) · `tax.py` (CG planning) · `tax_reminders.py`
(Discord + CalDAV nudges) · `obsidian.py` (writes every vault note).

## The views — and what each is for

Managed notes are **fully overwritten every build** — never hand-edit them.

### Trades DB (`Trades DB.md`)
- **Shows:** a sortable Dataview table of every closed lot — symbol, qty, buy/sell
  date+price, realized ₹ and %, holding days, STCG/LTCG class. Source: `closed_lots`
  (FIFO lot-matched from `transactions` by `realized_pnl.py`).
- **Decision:** which exits actually worked, and which lots are STCG vs LTCG.
- **Sable coaches:** anchors any "how did I do on X" answer to real lots, not memory; flags
  oversells/corporate-action lots that got quarantined out of the math.
- **Pointer:** `journal/realized_pnl.py` → `portfolio.db.closed_lots`.

### Missed Trades DB (`Missed Trades DB.md`)
- **Shows:** KPI cards (missed winners / dodged losers / net) + a sortable table of every
  BUY Sable advised that the user did **not** take (no buy within ±7 days / ±5% of the
  level), classified `missed_winner` / `dodged_loser` / `pending`. Each row now carries a
  **Corroboration** label grounding the verdict in real price — `target_hit` (the advised target
  genuinely printed → a *real* missed winner), `stopped` (the stop really hit → a *real* dodged
  loss), `soft` (time-cap close, the target **never printed** → a *theoretical* outcome),
  `pending` (too few sessions yet) — plus **Actual peak %** / actual trough % (the real high/low
  the stock reached from the alert level, via `forward_lib.excursion`). The gap between advice and
  reality can be huge: a SUVEN call advised +19.5% but the stock *actually peaked +137%*.
- **Decision:** am I acting on Sable's BUY calls — and was skipping them right or costly?
  The roll-up now reports a **confirmed** net (only `target_hit` / `stopped`) *separately* from
  soft/pending, so "following every missed call → net X%" isn't inflated by moves that never
  really printed.
- **Sable coaches:** turns "I keep hesitating" into a number; if dodged losers dominate,
  the hesitation is discipline; if confirmed missed winners dominate, size in faster next time —
  and discounts a `soft` verdict as untested.
- **Note:** each build refreshes the missed notes' frontmatter (managed data) but **preserves
  your reflection body**; a call that later became "taken" (even loosely) is reclassified *out*
  of the Missed view into Execution Review.
- **Pointer:** `journal/missed_trades.py` → `data/journal/missed_trades.jsonl`.

### Analytics (`Analytics.md`) — four sub-sections
- **(a) Scorecard** — Win Rate · Total P&L · Return-on-cost · Profit Factor with a
  **Week / Month / FY / All** timeframe toggle (default **Month**) scoping to lots *closed*
  in the window, a live Sable "read" sentence, and a per-window P&L bar chart.
  *Decision:* is the recent window actually green, on the metric that matters?
- **(b) P&L calendar** — per-day P&L with month navigation. *Decision:* spot clustering —
  are losses bunched on impulsive days?
- **(c) Performance profile** — an **all-time** radar (win rate, profit factor, recovery,
  consistency, plan-adherence). *Decision:* which single trait is dragging the system.
- **(d) Equity curve** — cumulative monthly P&L (all-time); shows the drawdown→recovery arc.
  *Decision:* are you compounding, or round-tripping gains?
- **Sable coaches:** reads the Scorecard window aloud, names the weakest radar axis, and ties
  the equity arc back to behaviour (over-trading, cutting winners early).
- **Pointer:** `journal/obsidian.py` (Dataview-JS + Charts).

### Effective P&L (`Effective P&L.md`)
- **Shows:** take-home **by financial year** — gross → charges → CG tax (after set-off +
  ₹1.25L LTCG exemption), with a second line showing tax after net-loss carry-forward;
  per-FY detail + a live per-FY lot table.
- **Decision:** what you actually kept — the number that matters for real wealth, not the
  gross headline.
- **Sable coaches:** reframes a "big win" by its after-tax reality; shows how much the
  exemption and carry-forward saved.
- **Pointer:** `journal/pnl_statement.py` (`charge_model.json`) + `journal/tax.py`.

### Tax Planning (`Tax Planning.md`)
- **Shows:** key-date countdowns (FY-end harvest ~28 Mar, advance-tax installments, ITR
  31 Jul); this-FY realized gains/losses + estimated tax + LTCG-exemption headroom;
  loss-harvest candidates; LTCG-threshold watch (STCG lots within 45 days of crossing 12
  months, and the tax saved by waiting).
- **Decision:** what to harvest, what to *hold* past the LTCG line, how much exemption is left.
- **Sable coaches:** before FY-end, walks the harvest list and the "hold 20 more days to drop
  20%→12.5%" winners; respects core (never harvest the untouchable core).
- **Pointer:** `journal/tax.py` → also drives `tax_reminders.py` (Discord `#sable-broadcast`
  general dates, deduped via `data/journal/tax_reminders.json`; per-holding LTCG-crossing
  events into a separate `algotrading-tax` CalDAV collection, full-reconciled nightly).
- **Planning aid, NOT tax advice.**

### Execution Review (`Execution Review.md`)
- **Shows:** for every BUY Sable advised that the user actually **took**, the advice vs the
  real fill — the advised entry/target, where the user really bought (entry **slippage %** and
  how many **days late**), and two exit reads: **advice quality** (the old *exit-vs-target %* —
  did the *forecast* target prove rich or conservative, proves nothing alone) and **Left on
  table (verified)** — how much higher the stock *actually* traded after the sell (real OHLC via
  `forward_lib.excursion`), with a verdict: `early` (it really ran ≥3% higher after the sell),
  `good` (sold within ~0.5% of the real high), `ok`, `pending` (window not full yet), `n/a` (no
  OHLC after the sell). Plus a **match tier**: `on_level` (buy within ±7 days and ±5% of the
  advised entry — including a buy placed just *before* the alert) or `loose` (nearest buy within
  45 days after the call, at *any* price — flagged so a fuzzy link can be judged and dropped).
- **Decision:** two execution disciplines — did I enter near Sable's level (slippage), and did
  I hold for the real upside or bail early (Left on table, verified). Honest example: a closed
  trade reads **−22.9% vs target** yet Left on table is only **+2% and pending** — so
  it was *not* an early exit; the target was simply optimistic. Read the verified column, not
  the forecast gap.
- **Sable coaches:** reads the gaps — "you entered 4% cheaper, and the stock only ran 2% past
  your sell — that target was rich, not a discipline miss"; treats a loose link with big
  slippage as possibly a different trade the user can tell Sable to drop. NOT yet fed into any
  numeric model — it's the dataset Sable reads during analysis.
- **Pointer:** `journal/execution_review.py` (`match_call` + `build_execution_review`) →
  `data/journal/execution_review.jsonl`; rendered by `journal/obsidian.py`; the new
  `execution_review` build stage runs after `missed_trades`. It is the **complement of Missed
  Trades DB** — a call the user took *loosely* leaves the "missed" list and appears here instead
  (the two are mutually exclusive).

### Reflection notes — create-if-absent, **never overwritten**
Your space; the build seeds them once and leaves them alone: per-trade notes under `Trades/`
(each with a **Lesson** section), per-missed-call notes under `Missed/`, daily `Reviews/`
(plus a seed template), and `Milestones.md`. This is where the journal becomes *yours* —
the managed views supply the facts, these hold the learning.

## Your cadence with Sable

- **Per trade (on close):** open the `Trades/` note and write the **Lesson** — one line on
  what the setup taught you (entry timing, sizing, holding too long/short). Sable will quote
  it back next time the same pattern appears.
- **Weekly:** Analytics → Scorecard on **Week** (then **Month** for signal); re-read the
  losers in Trades DB and the dodged-vs-missed split in Missed Trades DB. Ask Sable for the
  read on the weakest radar axis.
- **Monthly:** P&L calendar for clustering + Missed Trades DB for opportunity cost — are you
  leaving Sable's winners on the table, or rightly dodging losers? Then **Execution Review** —
  check your entry slippage vs Sable's levels and whether you're holding to targets or bailing early.
- **FY-end (Feb–Mar):** Tax Planning is the playbook — **harvest** losses by ~28 Mar (T+1
  settlement), **hold** LTCG-threshold winners past 12 months to pay 12.5% not 20%, and
  realize gains within the ₹1.25L exemption headroom. **Carry losses forward** only if you
  file ITR by 31 Jul. Effective P&L confirms the after-tax result.

## Caveats

- **GROSS = pre-charge, pre-tax.** The headline P&L in Trades DB / Analytics is before
  brokerage, STT/GST/DP and CG tax. Take-home lives only in the **Effective P&L** view.
- **Charge model is inferred,** not exact: a blended rate (all-in charges ÷ round-trip
  turnover) from one broker statement in `stock portfolio/`; falls back to defaults if none
  is present. Statutory CG rates are editable in `journal/charge_model.json`.
- **Planning aid, not tax advice.** The tax views model the current regime (STCG 20% /
  LTCG 12.5% / ₹1.25L exemption); verify before filing.
- **Per-lot FIFO ≠ per-position.** Each closed lot is one FIFO match; a single
  "position" you think of as one trade may split into several lots with different gain types.
- **Short windows are small-sample.** Week/Month Scorecards can swing on one or two lots —
  widen to FY/All before drawing a conclusion.
