# Groww API Integration — Architecture Brainstorm

How to evolve TradeCentral from "alerts you act on manually" to "system that can execute trades" — without ever putting capital at risk from a bug, a bad model, or a runaway loop.

---

## The Core Principle: Graduated Autonomy

Never jump from manual to fully automated. Build a ladder of trust levels, where each rung proves itself before you climb to the next. At every rung, the system should be **easier to stop than to start**.

```
Level 0: Alert only (today)        — "CGPOWER hit ₹420 BUY zone"
Level 1: Alert + draft order       — "...shall I place a BUY for 50 shares at ₹418?"
Level 2: One-click execution       — You tap "yes" on Telegram, system executes
Level 3: Auto-execute with limits  — System executes within pre-approved rules
Level 4: Full autonomy             — Model decides, sizes, and executes
```

**You should be at Level 2 for months before considering Level 3. Level 4 may never be appropriate for real capital — and that's fine.** The goal isn't full automation. It's reducing the friction between "I agree with this signal" and "the order is placed."

---

## Level 0: What You Have Today

```
Claude analysis → alert levels in stocks/*.md
                ↓
Bot polls price → crosses level → Telegram alert
                ↓
You read alert → open Groww app → manually place order
```

Latency: minutes to hours. Works fine for long-term investing. The risk is entirely human — you might miss an alert, fat-finger a quantity, or hesitate and miss the price.

---

## Level 1: Alert + Draft Order (Read-Only API)

### What Changes

When an alert fires, the system also computes what the order *would* look like and includes it in the Telegram message:

```
🟢 BUY  CGPOWER at ₹418 — approaching strong support

  Draft order:
  BUY 50 shares @ ₹418 LIMIT (CNC)
  Capital: ₹20,900
  Position after: 150 shares (12% of portfolio)

  Reply ✅ to execute | 👁️ to watch
```

### What's Needed

- **Groww API: read-only calls only** — portfolio positions, holdings, current margin
- **Position sizer module** — given the signal, current holdings, and portfolio rules, compute the right quantity
- **No order placement** — the API key doesn't even need order permissions at this stage

### Safety

- **Zero execution risk** — the system can't place orders
- **You see exactly what it would do** before you act
- **Catches sizing errors** — you'd spot "BUY 500 shares" (wrong) before it matters

### Position Sizing Logic

```python
def compute_order(ticker, signal, price, holdings, portfolio_value, stock_config):
    """Compute a draft order respecting core % and risk rules."""
    core_pct = stock_config.core_pct / 100
    max_allocation = 0.15  # never more than 15% in one stock

    current_shares = holdings.get(ticker, 0)
    current_value = current_shares * price
    current_pct = current_value / portfolio_value

    if signal == 'BUY':
        # Target: halfway between current allocation and max
        target_pct = min(max_allocation, current_pct + 0.03)  # add 3% per buy
        target_value = target_pct * portfolio_value
        buy_value = target_value - current_value
        quantity = max(1, int(buy_value / price))
        return {'action': 'BUY', 'quantity': quantity, 'price': price}

    elif signal == 'SELL':
        # Never sell below core
        core_shares = int((core_pct * portfolio_value) / price)
        sellable = max(0, current_shares - core_shares)
        # Trim 30% of sellable swing layer
        quantity = max(1, int(sellable * 0.30))
        return {'action': 'SELL', 'quantity': quantity, 'price': price}

    return None
```

### Key Rule: Core Protection

The position sizer **hard-codes** the rule that core shares are never sold. `sellable = current_shares - core_shares`. If `sellable <= 0`, the system refuses to generate a sell draft. This is your investment philosophy encoded as a constraint, not a suggestion.

---

## Level 2: One-Click Execution (Controlled Write Access)

### What Changes

When you reply ✅ to a draft order on Telegram, the system places the order via Groww API.

```
You reply ✅ → listener.py receives reaction
            → validates order is still sensible (price hasn't moved >2%)
            → places order via Groww API
            → sends confirmation: "✅ BOUGHT 50 CGPOWER @ ₹418.50 (filled)"
            → logs to data/executions.jsonl
```

### What's Needed

- **Groww API: order placement** — but with *tight* constraints
- **Execution module** (`alert_bot/executor.py`)
- **Staleness check** — if you reply ✅ two hours later, price may have moved. Reject if price has drifted more than X%.
- **Telegram confirmation flow** — double-confirm for orders above a threshold

### Safety Mechanisms

Every one of these is a **hard gate** — the system cannot bypass them:

| Gate | Rule | What It Prevents |
|------|------|------------------|
| **Approval required** | No order without explicit ✅ reaction | Runaway execution |
| **Staleness window** | Reject if draft is >30 min old OR price moved >2% | Executing at bad prices |
| **Daily capital limit** | Max ₹X deployed per day across all stocks | One bad day wiping out capital |
| **Per-stock limit** | Max ₹X per stock per day | Concentration risk |
| **Core floor** | Never sell below core share count | Violating investment philosophy |
| **Market hours only** | Reject orders outside 9:15-15:15 IST | After-hours mistakes |
| **Kill switch** | `/stop` Telegram command halts all execution immediately | Emergency stop |
| **Duplicate guard** | Same ticker + direction + price within 5 min = reject | Double-execution |
| **Paper trail** | Every order logged with signal source, draft, approval time | Audit and learning |

### The Kill Switch

```python
# In config or state.json
EXECUTION_ENABLED = True  # flipped to False by /stop command

# Checked before EVERY order
def place_order(order):
    if not state.get('execution_enabled', False):
        raise ExecutionHalted("Kill switch active. /start to resume.")
    # ... proceed
```

The `/stop` command should be **instant and unconditional**. No confirmation dialog, no "are you sure." One word stops everything.

### Architecture

```
alert_bot/
  executor.py      — Groww API wrapper with all safety gates
  sizer.py         — Position sizing (respects core %, portfolio limits)
  execution_log.py — Append-only log of all orders (data/executions.jsonl)

data/
  executions.jsonl — Full audit trail: signal → draft → approval → fill
  execution_state.json — Kill switch state, daily counters, per-stock limits
```

### Execution Log Format

```json
{
  "timestamp": "2026-04-07T14:23:01+05:30",
  "ticker": "CGPOWER",
  "signal_source": "alert_level",
  "signal_price": 418.0,
  "draft": {"action": "BUY", "quantity": 50, "price": 418, "order_type": "LIMIT"},
  "approval": {"method": "telegram_reaction", "time": "2026-04-07T14:25:33+05:30"},
  "execution": {"order_id": "GRW123456", "filled_price": 418.50, "filled_qty": 50, "status": "COMPLETE"},
  "gates_passed": ["staleness_ok", "daily_limit_ok", "stock_limit_ok", "core_floor_ok", "market_hours_ok"]
}
```

---

## Level 3: Auto-Execute with Rules (Much Later)

### What Changes

For signals that meet *all* criteria in a strict ruleset, the system executes without waiting for your ✅. You're notified after the fact.

### When to Consider This

- Level 2 has been running for **3+ months** with a clean execution log
- You've reviewed every auto-draft and agreed with >90% of them
- You've backtested the exact signal-to-order pipeline on historical data
- You have a paper trading period of 1+ month with no issues

### Additional Safety for Auto-Execution

Everything from Level 2, plus:

| Gate | Rule |
|------|------|
| **Whitelist only** | Only specific tickers are auto-eligible (opt-in per stock) |
| **Signal confidence threshold** | Only confidence 4-5 signals auto-execute |
| **Max auto-order size** | Much smaller than manual limit (e.g., ₹10,000 per auto-order) |
| **Daily auto-budget** | Separate, smaller cap than manual budget |
| **Cooldown** | Max 1 auto-execution per stock per day |
| **Regime gate** | No auto-execution if HMM says "bear regime" (once built) |
| **Drawdown circuit breaker** | If portfolio is down >5% this week, halt all auto-execution |

### The Drawdown Circuit Breaker

This is the most important safety mechanism at Level 3:

```python
def check_circuit_breaker(portfolio_value, weekly_high):
    """Halt auto-execution if portfolio has drawn down significantly."""
    drawdown = (weekly_high - portfolio_value) / weekly_high
    if drawdown > 0.05:  # 5% weekly drawdown
        disable_auto_execution()
        notify("⚠️ Circuit breaker tripped: portfolio down {:.1%} this week. "
               "Auto-execution halted. Manual orders still available via ✅.")
        return False
    return True
```

Auto-execution stops. Manual approval (Level 2) continues. You stay in control during drawdowns, which is exactly when you most need to be.

---

## Level 4: Full Autonomy (Probably Never for Real Capital)

This is where the model decides entry, exit, sizing, and timing without human approval. **This is appropriate for:**

- Paper trading / simulation accounts
- Very small "experiment" capital (₹10,000 in a separate account)
- Backtesting validation

**This is NOT appropriate for** your real investment portfolio. The reason is simple: your investment philosophy (core + swing, conviction-based, long-term accumulation) is fundamentally a human judgment process. Models can *inform* that judgment, but replacing it entirely means the model needs to be right about things it can't know — your risk tolerance today, your cash flow needs, your conviction in a thesis.

The sweet spot for a long-term investor is Level 2-3: models generate signals, you (or tight rules) approve execution, and the system handles the mechanics.

---

## Paper Trading First

Before any real money touches the API, run a **paper trading mode** that:

1. Generates real signals from real market data
2. Computes real draft orders with real position sizing
3. "Executes" by logging to `data/paper_trades.jsonl` instead of calling Groww
4. Tracks paper P&L as if the orders had filled
5. Runs for at least 1 month

```python
class PaperExecutor:
    """Drop-in replacement for GrowwExecutor that logs instead of trading."""
    def place_order(self, order):
        order['status'] = 'PAPER'
        order['filled_price'] = order['price']  # assume perfect fill
        self._log(order)
        return order
```

The executor module should accept either `GrowwExecutor` or `PaperExecutor` via dependency injection, so switching between paper and live is a config change, not a code change.

---

## Auth Architecture

The TOTP flow is better for automation (no daily manual approval), but the secret must be protected:

```
.env:
  GROWW_API_KEY=xxx
  GROWW_TOTP_SECRET=xxx   # NEVER commit this

# In executor.py
import pyotp
from growwapi import GrowwAPI

def _get_client():
    totp = pyotp.TOTP(os.environ['GROWW_TOTP_SECRET']).now()
    token = GrowwAPI.get_access_token(
        api_key=os.environ['GROWW_API_KEY'],
        totp=totp
    )
    return GrowwAPI(token)
```

Token refresh: get a new access token at the start of each trading day (9:10 IST). Don't cache tokens across days.

---

## Integration Points with TradeCentral

```
                    ┌─────────────────┐
                    │  Quant Models   │
                    │  HMM, MC, OU    │
                    └────────┬────────┘
                             │ signals
                    ┌────────▼────────┐
                    │  Alert Engine   │  ← existing bot
                    │  engine.py      │
                    └────────┬────────┘
                             │ alert fires
                    ┌────────▼────────┐
                    │  Position Sizer │  ← new: sizer.py
                    │  core %, limits │
                    └────────┬────────┘
                             │ draft order
                    ┌────────▼────────┐
                    │  Telegram Msg   │  ← existing notifier
                    │  with draft     │
                    └────────┬────────┘
                             │ ✅ reaction (Level 2)
                             │   or auto (Level 3)
                    ┌────────▼────────┐
                    │  Safety Gates   │  ← new: executor.py
                    │  all checks     │
                    └────────┬────────┘
                             │ passes all gates
                    ┌────────▼────────┐
                    │  Groww API      │
                    │  place_order()  │
                    └────────┬────────┘
                             │ fill confirmation
                    ┌────────▼────────┐
                    │  Execution Log  │  ← new: data/executions.jsonl
                    │  + Telegram ack │
                    └─────────────────┘
```

---

## What to Build First

1. **Read-only Groww integration** — fetch portfolio, holdings, margin. Display in web UI.
2. **Position sizer** — compute draft orders for every alert that fires. Log them. Don't show to user yet.
3. **Shadow mode** — run the sizer for 2-4 weeks, review the log. Would you have agreed with these drafts?
4. **Draft orders in Telegram** — show the draft alongside the alert. Still no execution.
5. **Paper executor** — ✅ reaction triggers paper trade logging.
6. **Live executor** — flip the switch. Start with one stock, tiny size, watch closely.

Each step is independently useful and independently safe. You never need to proceed to the next step. The system at step 4 is already a massive improvement over today — you see exactly what to do, the sizing is computed for you, and you just open Groww and place it manually with confidence.

---

## What NOT to Do

- **Don't build execution before the models are proven.** A fast pipe to bad signals just loses money faster.
- **Don't auto-execute during earnings season** without explicit gates. Volatility around results is not normal volatility.
- **Don't trust backtests blindly.** A strategy that returned 40% annually in backtest will not return 40% live. Slippage, timing, and regime changes eat returns.
- **Don't keep the API key in the same process as the models.** If a model bug causes it to signal "BUY" in a loop, the executor's safety gates should catch it — but defense in depth means the model shouldn't even have access to the executor.
- **Don't skip the paper trading phase.** It costs nothing and catches everything.
